# 缩略图批作业台

Flask 实现的 Linux 作业台：网页设置运行时限、启动内置缩略图转换样例、查看进度、暂停/继续。
样例中**主进程拉起 worker 子进程后立即退出，子进程留在原进程组里继续生成缩略图**，
作业台按**进程组**管控，因此主进程先退也不影响暂停、续跑和超时强杀。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

本地直跑（开发/排障）：

```bash
pip install -r requirements.txt
JOB_OUT_DIR=/tmp/jobdata python app/app.py
```

端到端实测（需服务已启动，仅依赖标准库）：

```bash
python3 tests/e2e.py --base http://127.0.0.1:8000
```

## 值班操作流程

1. 网页填「运行时限」等参数，启动转换样例；
2. 需要核对时先点「暂停」——作业台向整个进程组发 `SIGSTOP` 停住写盘，
   **等全部存活进程都进入停止态后才显示「已暂停」并冻结剩余时限**；
3. 核对进度（进度条 / `GET /api/jobs/<id>`）；
4. 点「继续」——向进程组发 `SIGCONT`，**原进程（PID 不变）按冻结的剩余时限接着跑**。

## 已实现

- **网页作业台**：设置运行时限（另有总数/工作进程数/单张耗时参数）、启动内置转换样例、
  每秒自动刷新进度（完成数/总数、剩余时限、已运行时长、组内进程列表）、暂停/继续按钮。
- **内置转换样例**（`app/sample.py`）：主进程用 `subprocess` 拉起 K 个 worker 后**立即退出**；
  worker 用 Pillow 生成 512×512 源图并缩成 128×128 PNG，先写 `.tmp` 再 `rename`，
  进度只数完整落盘的 `.png`。
- **进程组级管控**（`app/jobs.py`）：
  - 样例整体 `start_new_session=True` 启动，自成会话/进程组（pgid = 主进程 pid），
    主进程退出后残留子进程仍在组内，信号按组投递；
  - 暂停：`SIGSTOP` 整组 → 轮询 `/proc` 确认全部存活进程（排除僵尸）为 `T/t` →
    才置「已暂停」并冻结剩余时限（停止计时）；
  - 继续：`SIGCONT` 整组，用冻结的剩余时限重设截止时间，原 PID 接着跑；
  - 超时：监控线程在**运行中**和**暂停确认中**都检查截止时间，到点 `SIGKILL` 整组并标记「已超时」；
    已暂停期间计时冻结，不判超时；
  - 终态保护：已完成/已超时/失败后，迟到的暂停/继续返回 `409`，不改动状态与产物；
  - 僵尸回收：监控线程 `waitpid(-1, WNOHANG)` 回收被 reparent 过来的孤儿，
    Compose 侧 `init: true`（tini）双保险；
  - 自然结束判定：组内存活进程清零时按产物数判定「已完成」或「失败（提前退出）」。
- **API**：`POST /api/jobs`、`GET /api/jobs`、`GET /api/jobs/<id>`、
  `POST /api/jobs/<id>/pause`、`POST /api/jobs/<id>/resume`。
- **Docker Compose 一键启动**（`Dockerfile` + `docker-compose.yml`，产物卷 `jobs-data`）。
- **测试钩子**：创建作业时可传 `pause_confirm_delay`（秒，默认 0，页面不展示），
  人为拉长停止确认窗口，用于复现「暂停确认期间超时」。

## 实测结果

环境说明：本开发机无 Docker，`docker compose up` 未能在本机执行；
已用与容器相同的入口命令（`python app/app.py`，Python 3.11 + Flask 3.1.3 + Pillow 13.0.0）实测，
`docker-compose.yml` 通过 YAML 解析校验。`tests/e2e.py` 共 24 项断言全部通过：

```
== A. 主进程先退、子进程继续、自然完成 ==
[PASS] A1 创建作业 | state=RUNNING pgid=269
[PASS] A2 主进程已退出但子进程仍在跑
[PASS] A3 作业自然完成 | state=COMPLETED
[PASS] A4 进度 9/9 | done=9
== B. 暂停→冻结→原进程续跑→按剩余时限超时 ==
[PASS] B0 暂停时主进程已退出、子进程仍在 | pgid=292 pids=[293, 294]
[PASS] B1 运行中请求继续被拒绝且不改动
[PASS] B2 暂停受理，进入暂停确认中
[PASS] B3 全部进程停住后显示已暂停
[PASS] B4 确认时组内进程均为停止态 | procs=[{'pid': 293, 'state': 'T'}, {'pid': 294, 'state': 'T'}]
[PASS] B5 已暂停时重复暂停不改动
[PASS] B6 暂停中计时停止 | remaining 4.81 -> 4.81
[PASS] B7 暂停中写盘停住 | done 6 -> 6
[PASS] B8 继续受理，恢复运行
[PASS] B9 原进程接着跑（PID 不变） | before=[293, 294] after=[293, 294]
[PASS] B10 按剩余时限超时 | state=TIMEOUT
[PASS] B11 超时时刻≈冻结的剩余时限 | resume->timeout 4.89s，冻结剩余 4.81s
[PASS] B12 超时后进程组清空
[PASS] B13 被时限打断，未完成全部 | done=16
== C. 运行中超时强杀整组 ==
[PASS] C1 超时终止 | state=TIMEOUT
[PASS] C2 超时时刻≈3s | 3.02s
[PASS] C3 进程组清空
== D. 暂停确认期间超时 ==
[PASS] D1 暂停受理，进入暂停确认中
[PASS] D2 确认暂停期间时限到，按超时处理 | state=TIMEOUT
[PASS] D3 进程组清空
== E. 迟到操作不改动结果 ==
[PASS] E1 已完成作业拒绝暂停/继续 | pause=409 resume=409
[PASS] E2 已完成结果未被改动 | state=COMPLETED done=9
[PASS] E3 超时作业拒绝暂停/继续
[PASS] E4 超时结果未被改动
==================================================
全部通过
```

佐证（作业 A 产物目录）：`sample.log` 记录 `[main 269] spawned workers [271, 272, 273];
main exits now, workers keep converting`，主进程退出后 3 个 worker 继续产出，
最终 9 张 `thumb-*.png` 齐全，`file` 确认为合法 128×128 PNG；
测试结束后系统僵尸进程数为 0，服务日志无异常。

## 遗留事项

- 本机无 Docker，`docker compose up` 未实际执行过；镜像构建与容器内运行建议在有
  Docker 的环境复核一次（入口命令与本地实测一致，风险低）。
- 暂停确认依赖轮询 `/proc`（默认 0.2s 一档），停止确认存在亚秒级延迟；
  超时判定精度同为监控周期量级。
- 作业表在内存中，服务重启后历史作业状态丢失（产物文件仍在）；仅支持单进程部署，
  多副本/多 worker 部署需要外置状态存储。
- 未做鉴权与操作审计，仅限内网值班使用；暂停/继续/启动接口无幂等键，依赖状态机拒绝非法操作。
- 暂停期间若进程被外部 `kill -9`，继续时会按「进程组已退出」自然收尾（按产物数判完成/失败），
  不会误报为暂停成功。
- 进度按产物文件数统计，未校验图片内容完整性；失败作业的产物目录保留，需人工清理。
