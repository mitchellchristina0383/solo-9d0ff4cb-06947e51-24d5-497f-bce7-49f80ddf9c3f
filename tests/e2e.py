#!/usr/bin/env python3
"""端到端实测脚本：主进程先退后的完成 / 暂停 / 续跑 / 超时 / 迟到操作。

仅依赖标准库。用法：
  python3 tests/e2e.py --base http://127.0.0.1:8000
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

FAILURES = []


def api(base, method, path, payload=None):
    data = headers = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name, cond, detail=""):
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         (" | " + detail) if detail else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(base, jid, pred, timeout_s, interval=0.2):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        st, body = api(base, "GET", "/api/jobs/" + jid)
        assert st == 200, body
        last = body
        if pred(body):
            return body
        time.sleep(interval)
    return last


def test_a(base):
    """主进程先退、子进程继续、自然完成。"""
    print("== A. 主进程先退、子进程继续、自然完成 ==")
    st, j = api(base, "POST", "/api/jobs",
                {"limit": 30, "total": 9, "workers": 3, "delay": 1.0})
    check("A1 创建作业", st == 201, "state=%s pgid=%s" % (j.get("state"), j.get("pgid")))
    jid, pgid = j["id"], j["pgid"]
    saw_main_gone_children_alive = False
    t0 = time.time()
    final = None
    while time.time() - t0 < 25:
        _, b = api(base, "GET", "/api/jobs/" + jid)
        pids = [p["pid"] for p in b["processes"]]
        if pids and pgid not in pids:
            saw_main_gone_children_alive = True
        if b["ended"]:
            final = b
            break
        time.sleep(0.2)
    check("A2 主进程已退出但子进程仍在跑", saw_main_gone_children_alive)
    check("A3 作业自然完成", bool(final) and final["state"] == "COMPLETED",
          "state=%s" % (final and final["state"]))
    check("A4 进度 9/9", bool(final) and final["done"] == 9,
          "done=%s" % (final and final["done"]))
    return jid


def test_b(base):
    """暂停 → 冻结计时 → 原进程续跑 → 按剩余时限超时。"""
    print("== B. 暂停→冻结→原进程续跑→按剩余时限超时 ==")
    st, j = api(base, "POST", "/api/jobs",
                {"limit": 8, "total": 200, "workers": 2, "delay": 1.0})
    jid, pgid = j["id"], j["pgid"]
    time.sleep(3.0)

    _, run = api(base, "GET", "/api/jobs/" + jid)
    pids_now = [p["pid"] for p in run["processes"]]
    check("B0 暂停时主进程已退出、子进程仍在", pgid not in pids_now and len(pids_now) > 0,
          "pgid=%d pids=%s" % (pgid, pids_now))

    st, rj = api(base, "POST", "/api/jobs/%s/resume" % jid)
    check("B1 运行中请求继续被拒绝且不改动", st == 409 and rj["state"] == "RUNNING")

    st, pj = api(base, "POST", "/api/jobs/%s/pause" % jid)
    check("B2 暂停受理，进入暂停确认中", st == 200 and pj["state"] == "PAUSING")

    paused = wait_for(base, jid, lambda b: b["state"] == "PAUSED", 6)
    check("B3 全部进程停住后显示已暂停", paused["state"] == "PAUSED")
    check("B4 确认时组内进程均为停止态",
          len(paused["processes"]) > 0
          and all(p["state"] in ("T", "t") for p in paused["processes"]),
          "procs=%s" % paused["processes"])
    r1, d1 = paused["remaining"], paused["done"]
    pids1 = sorted(p["pid"] for p in paused["processes"])

    st, again = api(base, "POST", "/api/jobs/%s/pause" % jid)
    check("B5 已暂停时重复暂停不改动", st == 409 and again["state"] == "PAUSED")

    time.sleep(2.5)
    _, p2 = api(base, "GET", "/api/jobs/" + jid)
    check("B6 暂停中计时停止", abs(p2["remaining"] - r1) < 0.3,
          "remaining %.2f -> %.2f" % (r1, p2["remaining"]))
    check("B7 暂停中写盘停住", p2["done"] == d1, "done %d -> %d" % (d1, p2["done"]))

    t_resume = time.time()
    st, rj2 = api(base, "POST", "/api/jobs/%s/resume" % jid)
    check("B8 继续受理，恢复运行", st == 200 and rj2["state"] == "RUNNING")
    _, run2 = api(base, "GET", "/api/jobs/" + jid)
    check("B9 原进程接着跑（PID 不变）",
          sorted(p["pid"] for p in run2["processes"]) == pids1,
          "before=%s after=%s" % (pids1, sorted(p["pid"] for p in run2["processes"])))

    final = wait_for(base, jid, lambda b: b["ended"], r1 + 8)
    dt = final["ended_at"] - t_resume
    check("B10 按剩余时限超时", final["state"] == "TIMEOUT", "state=%s" % final["state"])
    check("B11 超时时刻≈冻结的剩余时限", abs(dt - r1) < 1.2,
          "resume->timeout %.2fs，冻结剩余 %.2fs" % (dt, r1))
    check("B12 超时后进程组清空", len(final["processes"]) == 0)
    check("B13 被时限打断，未完成全部", final["done"] < 200, "done=%d" % final["done"])
    return jid


def test_c(base):
    """运行中时限到 → 强杀整组报超时。"""
    print("== C. 运行中超时强杀整组 ==")
    st, j = api(base, "POST", "/api/jobs",
                {"limit": 3, "total": 200, "workers": 2, "delay": 1.0})
    jid = j["id"]
    t0 = time.time()
    final = wait_for(base, jid, lambda b: b["ended"], 10)
    dt = final["ended_at"] - t0
    check("C1 超时终止", final["state"] == "TIMEOUT", "state=%s" % final["state"])
    check("C2 超时时刻≈3s", abs(dt - 3) < 1.0, "%.2fs" % dt)
    check("C3 进程组清空", len(final["processes"]) == 0)
    return jid


def test_d(base):
    """暂停确认期间时限到 → 强杀整组报超时（用 pause_confirm_delay 拉开确认窗口）。"""
    print("== D. 暂停确认期间超时 ==")
    st, j = api(base, "POST", "/api/jobs",
                {"limit": 2, "total": 200, "workers": 2, "delay": 1.0,
                 "pause_confirm_delay": 4})
    jid = j["id"]
    time.sleep(0.3)
    st, pj = api(base, "POST", "/api/jobs/%s/pause" % jid)
    check("D1 暂停受理，进入暂停确认中", st == 200 and pj["state"] == "PAUSING")
    final = wait_for(base, jid, lambda b: b["ended"], 8)
    check("D2 确认暂停期间时限到，按超时处理", final["state"] == "TIMEOUT",
          "state=%s" % final["state"])
    check("D3 进程组清空", len(final["processes"]) == 0)
    return jid


def test_e(base, completed_id, timeout_id):
    """作业结束后，迟到的暂停/继续不能改动结果。"""
    print("== E. 迟到操作不改动结果 ==")
    _, before = api(base, "GET", "/api/jobs/" + completed_id)
    st1, _ = api(base, "POST", "/api/jobs/%s/pause" % completed_id)
    st2, _ = api(base, "POST", "/api/jobs/%s/resume" % completed_id)
    _, after = api(base, "GET", "/api/jobs/" + completed_id)
    check("E1 已完成作业拒绝暂停/继续", st1 == 409 and st2 == 409,
          "pause=%s resume=%s" % (st1, st2))
    check("E2 已完成结果未被改动",
          after["state"] == before["state"] == "COMPLETED"
          and after["done"] == before["done"],
          "state=%s done=%s" % (after["state"], after["done"]))

    st3, _ = api(base, "POST", "/api/jobs/%s/pause" % timeout_id)
    st4, _ = api(base, "POST", "/api/jobs/%s/resume" % timeout_id)
    _, t_after = api(base, "GET", "/api/jobs/" + timeout_id)
    check("E3 超时作业拒绝暂停/继续", st3 == 409 and st4 == 409)
    check("E4 超时结果未被改动",
          t_after["state"] == "TIMEOUT" and len(t_after["processes"]) == 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    completed_id = test_a(base)
    test_b(base)
    timeout_id = test_c(base)
    test_d(base)
    test_e(base, completed_id, timeout_id)

    print("=" * 50)
    if FAILURES:
        print("失败 %d 项：%s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
