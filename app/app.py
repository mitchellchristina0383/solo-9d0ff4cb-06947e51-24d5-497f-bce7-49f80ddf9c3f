"""Flask 作业台入口：设置时限、启动样例、看进度、暂停/继续。"""
import os

from flask import Flask, jsonify, render_template, request

from jobs import JobManager, snapshot

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
manager = JobManager(
    out_root=os.environ.get("JOB_OUT_DIR", os.path.join(BASE_DIR, "..", "data", "jobs")),
    monitor_interval=float(os.environ.get("JOB_MONITOR_INTERVAL", "0.2")),
)


def _number(data, name, default, lo, hi):
    raw = data.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是数字" % name)
    if not (lo <= value <= hi):
        raise ValueError("%s 必须在 %s ~ %s 之间" % (name, lo, hi))
    return value


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/jobs")
def create_job():
    data = request.get_json(force=True, silent=True) or {}
    try:
        limit = _number(data, "limit", 30, 1, 3600)
        total = int(_number(data, "total", 12, 1, 500))
        workers = int(_number(data, "workers", 3, 1, 8))
        delay = _number(data, "delay", 1.0, 0.0, 30.0)
        # 测试钩子：模拟停止确认耗时（默认 0，页面不展示）
        pause_confirm_delay = _number(data, "pause_confirm_delay", 0.0, 0.0, 60.0)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    job = manager.start_job(limit, total, workers, delay, pause_confirm_delay)
    return jsonify(snapshot(job)), 201


@app.get("/api/jobs")
def list_jobs():
    return jsonify([snapshot(j) for j in manager.all_jobs()])


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = manager.get(job_id)
    if job is None:
        return jsonify({"error": "作业不存在"}), 404
    return jsonify(snapshot(job))


@app.post("/api/jobs/<job_id>/pause")
def pause_job(job_id):
    job, accepted, note = manager.pause(job_id)
    if job is None:
        return jsonify({"error": "作业不存在"}), 404
    body = snapshot(job)
    body.update({"accepted": accepted, "note": note})
    return jsonify(body), (200 if accepted else 409)


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id):
    job, accepted, note = manager.resume(job_id)
    if job is None:
        return jsonify({"error": "作业不存在"}), 404
    body = snapshot(job)
    body.update({"accepted": accepted, "note": note})
    return jsonify(body), (200 if accepted else 409)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)
