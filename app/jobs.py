"""作业进程组管理：启动、暂停(SIGSTOP)、继续(SIGCONT)、超时强杀、进度统计。

关键设计：
- 样例整体以 start_new_session=True 启动，自成一个会话/进程组，pgid == 主进程 pid。
  主进程退出后，残留的子进程仍在这个进程组里，信号按组投递即可全部管住。
- 暂停 = 向进程组发 SIGSTOP，然后轮询 /proc 确认全部存活进程进入 T/t 状态，
  才置为 PAUSED 并冻结剩余时限（停止计时）。
- 继续 = 向进程组发 SIGCONT，并用冻结的剩余时限重设截止时间，原进程接着跑。
- 运行中(RUNNING)与暂停确认中(PAUSING)都按截止时间判超时：到点 SIGKILL 整组。
- 终态（已完成/已超时/失败）下，迟到的暂停/继续请求一律拒绝且不改动任何状态。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid

STATE_RUNNING = "RUNNING"
STATE_PAUSING = "PAUSING"
STATE_PAUSED = "PAUSED"
STATE_COMPLETED = "COMPLETED"
STATE_TIMEOUT = "TIMEOUT"
STATE_FAILED = "FAILED"

TERMINAL_STATES = {STATE_COMPLETED, STATE_TIMEOUT, STATE_FAILED}

STATE_LABELS = {
    STATE_RUNNING: "运行中",
    STATE_PAUSING: "暂停确认中",
    STATE_PAUSED: "已暂停",
    STATE_COMPLETED: "已完成",
    STATE_TIMEOUT: "已超时",
    STATE_FAILED: "失败",
}

_SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.py")


def group_processes(pgid):
    """扫描 /proc，返回进程组 pgid 内的 [(pid, state)]，state 取自 /proc/<pid>/stat。"""
    result = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "r") as fh:
                data = fh.read()
        except OSError:
            continue
        rparen = data.rfind(")")
        if rparen < 0:
            continue
        fields = data[rparen + 2:].split()
        if len(fields) < 3:
            continue
        try:
            pgrp = int(fields[2])
        except ValueError:
            continue
        if pgrp == pgid:
            result.append((int(name), fields[0]))
    return result


def live_processes(pgid):
    """组内存活进程（排除僵尸 Z / 已销毁 X）。"""
    return [(pid, st) for pid, st in group_processes(pgid) if st not in ("Z", "X")]


def count_outputs(out_dir):
    """已完成的缩略图数（样例先写 .tmp 再 rename，只数正式 .png）。"""
    try:
        return sum(1 for f in os.listdir(out_dir) if f.endswith(".png"))
    except OSError:
        return 0


class Job:
    def __init__(self, limit, total, workers, delay, out_root, pause_confirm_delay=0.0):
        self.id = uuid.uuid4().hex[:8]
        self.limit = float(limit)
        self.total = int(total)
        self.workers = int(workers)
        self.delay = float(delay)
        # 测试钩子：模拟"停止确认"耗时，便于复现"暂停确认期间超时"的场景
        self.pause_confirm_delay = max(0.0, float(pause_confirm_delay))
        self.out_dir = os.path.join(out_root, self.id)
        self.state = STATE_RUNNING
        self.message = "作业已创建"
        self.popen = None
        self.pgid = None
        self.created_at = time.time()
        self.ended_at = None
        now = time.monotonic()
        self.deadline = now + self.limit   # PAUSED 时置 None
        self.remaining = None              # PAUSED 时冻结的剩余秒数
        self.pause_confirm_after = None    # pause_confirm_delay 生效的时刻
        self.run_started = now             # 最近一次进入运行态的单调时刻
        self.run_accum = 0.0               # 已累计的运行时长（不含暂停）
        self.pause_count = 0
        self.resume_count = 0

    @property
    def ended(self):
        return self.state in TERMINAL_STATES

    def elapsed_active(self):
        acc = self.run_accum
        if self.run_started is not None:
            acc += time.monotonic() - self.run_started
        return acc


class JobManager:
    def __init__(self, out_root, monitor_interval=0.2):
        self.out_root = os.path.abspath(out_root)
        self.monitor_interval = max(0.05, float(monitor_interval))
        self.jobs = {}
        self.lock = threading.RLock()
        os.makedirs(self.out_root, exist_ok=True)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="job-monitor", daemon=True)
        self._thread.start()

    # ---------------- 对外操作 ----------------
    def start_job(self, limit, total, workers, delay, pause_confirm_delay=0.0):
        with self.lock:
            job = Job(limit, total, workers, delay, self.out_root, pause_confirm_delay)
            self.jobs[job.id] = job
            os.makedirs(job.out_dir, exist_ok=True)
            cmd = [sys.executable, _SAMPLE,
                   "--out", job.out_dir,
                   "--total", str(job.total),
                   "--workers", str(job.workers),
                   "--delay", str(job.delay)]
            try:
                log = open(os.path.join(job.out_dir, "sample.log"), "ab")
                # start_new_session=True：样例整体成为新会话/新进程组，pgid == 主进程 pid
                popen = subprocess.Popen(
                    cmd, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True)
                log.close()
            except OSError as exc:
                job.state = STATE_FAILED
                job.message = "启动失败：%s" % exc
                job.ended_at = time.time()
                job.deadline = None
                job.run_started = None
                return job
            job.popen = popen
            job.pgid = popen.pid
            job.state = STATE_RUNNING
            job.message = "运行中（主进程 pid=%d，进程组 pgid=%d）" % (popen.pid, popen.pid)
            return job

    def pause(self, job_id):
        """请求暂停。仅 RUNNING 受理；终态/其他状态一律不改动结果。"""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None, False, "作业不存在"
            if job.ended:
                return job, False, "作业已结束（%s），迟到的暂停请求已忽略" % STATE_LABELS[job.state]
            if job.state != STATE_RUNNING:
                return job, False, "当前状态为「%s」，暂停请求已忽略" % STATE_LABELS[job.state]
            if time.monotonic() >= job.deadline:
                self._timeout(job)
                return job, False, "暂停请求到达时时限已到，作业按超时处理"
            try:
                os.killpg(job.pgid, signal.SIGSTOP)
            except ProcessLookupError:
                self._finalize_natural(job)
                return job, False, "进程组已退出，按结束处理"
            job.state = STATE_PAUSING
            job.pause_confirm_after = time.monotonic() + job.pause_confirm_delay
            job.message = "已向进程组发送 SIGSTOP，等待全部存活进程停住…"
            return job, True, "暂停请求已受理，等待进程停住"

    def resume(self, job_id):
        """请求继续。仅 PAUSED 受理；用冻结的剩余时限重设截止时间，原进程接着跑。"""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None, False, "作业不存在"
            if job.ended:
                return job, False, "作业已结束（%s），迟到的继续请求已忽略" % STATE_LABELS[job.state]
            if job.state != STATE_PAUSED:
                return job, False, "当前状态为「%s」，继续请求已忽略" % STATE_LABELS[job.state]
            try:
                os.killpg(job.pgid, signal.SIGCONT)
            except ProcessLookupError:
                self._finalize_natural(job)
                return job, False, "进程组已退出，按结束处理"
            now = time.monotonic()
            job.deadline = now + (job.remaining or 0.0)
            job.remaining = None
            job.run_started = now
            job.state = STATE_RUNNING
            job.resume_count += 1
            job.message = "已继续，按剩余时限 %.1fs 接着跑" % (job.deadline - now)
            return job, True, "已继续"

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def all_jobs(self):
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.created_at)

    # ---------------- 监控线程 ----------------
    def _monitor_loop(self):
        while not self._stop.is_set():
            self._reap_zombies()
            with self.lock:
                for job in list(self.jobs.values()):
                    try:
                        self._tick(job)
                    except Exception:
                        traceback.print_exc()
            time.sleep(self.monitor_interval)

    def _reap_zombies(self):
        """回收被 reparent 到本进程的孤儿。

        样例主进程先退出后，它的 worker 子进程会被 reparent 到 PID 1；
        容器里若本进程就是 PID 1，这些孤儿最终要由我们 wait 掉，否则积累僵尸。
        """
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except (ChildProcessError, OSError):
                return
            if pid <= 0:
                return

    def _tick(self, job):
        now = time.monotonic()
        # 运行中或等待确认暂停时，时限一到就结束整组并报超时
        if job.state in (STATE_RUNNING, STATE_PAUSING):
            if job.deadline is not None and now >= job.deadline:
                self._timeout(job)
                return
        if job.state == STATE_RUNNING:
            if not live_processes(job.pgid):
                self._finalize_natural(job)
        elif job.state == STATE_PAUSING:
            live = live_processes(job.pgid)
            if not live:
                # 暂停确认期间进程组自己退出了，按自然结束处理
                self._finalize_natural(job)
                return
            all_stopped = all(st in ("T", "t") for _, st in live)
            if all_stopped and now >= (job.pause_confirm_after or 0):
                # 全部存活进程都停住了：置已暂停并停止计时
                job.remaining = max(0.0, job.deadline - now)
                job.deadline = None
                job.run_accum += now - job.run_started
                job.run_started = None
                job.state = STATE_PAUSED
                job.pause_count += 1
                job.message = "全部存活进程已停住，计时已暂停（剩余 %.1fs）" % job.remaining

    def _timeout(self, job):
        try:
            os.killpg(job.pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self._close(job, STATE_TIMEOUT,
                    "运行时限 %.0fs 已到，已强杀整个进程组" % job.limit)

    def _finalize_natural(self, job):
        done = count_outputs(job.out_dir)
        if done >= job.total:
            self._close(job, STATE_COMPLETED, "全部 %d 张缩略图已生成" % done)
        else:
            self._close(job, STATE_FAILED,
                        "进程组提前退出，仅完成 %d/%d" % (done, job.total))

    def _close(self, job, state, message):
        if job.run_started is not None:
            job.run_accum += time.monotonic() - job.run_started
            job.run_started = None
        job.deadline = None
        job.state = state
        job.message = message
        job.ended_at = time.time()


def snapshot(job):
    """给 API/页面用的作业状态快照。"""
    if job.state in (STATE_RUNNING, STATE_PAUSING) and job.deadline is not None:
        remaining = max(0.0, job.deadline - time.monotonic())
    elif job.state == STATE_PAUSED:
        remaining = job.remaining
    else:
        remaining = None
    # 终态作业不再扫 /proc，避免 pid 复用后误报"存活进程"
    procs = [] if job.ended or not job.pgid else group_processes(job.pgid)
    return {
        "id": job.id,
        "state": job.state,
        "state_label": STATE_LABELS[job.state],
        "ended": job.ended,
        "done": count_outputs(job.out_dir),
        "total": job.total,
        "limit": job.limit,
        "remaining": remaining,
        "elapsed_active": round(job.elapsed_active(), 2),
        "workers": job.workers,
        "delay": job.delay,
        "pgid": job.pgid,
        "processes": [{"pid": p, "state": s} for p, s in sorted(procs)],
        "message": job.message,
        "created_at": job.created_at,
        "ended_at": job.ended_at,
        "pause_count": job.pause_count,
        "resume_count": job.resume_count,
        "out_dir": job.out_dir,
    }
