"""内置缩略图转换样例。

主进程负责拉起若干 worker 子进程，然后自己立即退出；
worker 子进程留在同一个进程组里继续生成缩略图——
模拟真实批作业中"主进程先退、子进程还在写盘"的场景。

用法：
  主进程: python sample.py --out DIR --total N --workers K --delay S
  worker: python sample.py --worker --out DIR --total N --start W --step K --delay S
"""
import argparse
import os
import subprocess
import sys
import time

from PIL import Image, ImageDraw

THUMB_SIZE = (128, 128)


def make_thumbnail(path, index):
    """生成一张"转换而来"的缩略图：先造 512x512 源图，再缩到 128x128。

    先写 .tmp 再 rename，保证进度里数到的 .png 都是写完整的。
    """
    src = Image.new("RGB", (512, 512))
    draw = ImageDraw.Draw(src)
    for y in range(0, 512, 32):
        color = ((index * 37 + y) % 256,
                 (index * 91 + y * 2) % 256,
                 (index * 53 + y * 3) % 256)
        draw.rectangle([0, y, 512, y + 31], fill=color)
    draw.text((20, 240), "SRC %03d" % index, fill=(255, 255, 255))
    thumb = src.resize(THUMB_SIZE, Image.LANCZOS)
    tmp = path + ".tmp"
    thumb.save(tmp, "PNG")
    os.replace(tmp, path)


def run_worker(args):
    pid = os.getpid()
    print("[worker %d] start, slice %d::%d of %d"
          % (pid, args.start, args.step, args.total), flush=True)
    for i in range(args.start, args.total, args.step):
        make_thumbnail(os.path.join(args.out, "thumb-%03d.png" % i), i)
        print("[worker %d] thumbnail %03d done" % (pid, i), flush=True)
        time.sleep(args.delay)
    print("[worker %d] finished" % pid, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--total", type=int, default=12)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--step", type=int, default=1)
    args = parser.parse_args()

    if args.worker:
        run_worker(args)
        return

    os.makedirs(args.out, exist_ok=True)
    kids = []
    for w in range(args.workers):
        logf = open(os.path.join(args.out, "worker-%d.log" % w), "ab")
        # 不 setsid：worker 留在主进程的进程组里，便于作业台按组管控
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker",
             "--out", args.out, "--total", str(args.total),
             "--start", str(w), "--step", str(args.workers),
             "--delay", str(args.delay)],
            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT)
        logf.close()
        kids.append(proc.pid)
    # 主进程到此就退出，worker 子进程继续在后台生成缩略图
    print("[main %d] spawned workers %s; main exits now, workers keep converting"
          % (os.getpid(), kids), flush=True)


if __name__ == "__main__":
    main()
