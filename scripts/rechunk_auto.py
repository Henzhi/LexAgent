"""重切分自动接力脚本

流程：
1. 等待当前正在运行的重切分进程（如第 1 批 offset=100）全部结束
2. 依次自动执行剩余批次：offset 200/300/.../800，每批 100 个
3. 每批输出到控制台并汇总结果

用法：
    uv run python scripts/rechunk_auto.py
    # 或先手动跑批，再在任何时刻启动本脚本：它会先等当前批结束
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 剩余批次：(offset, limit)
BATCHES = [
    (200, 100),  # 第 2 批 201-300
    (300, 100),  # 第 3 批 301-400
    (400, 100),  # 第 4 批 401-500
    (500, 100),  # 第 5 批 501-600
    (600, 100),  # 第 6 批 601-700
    (700, 100),  # 第 7 批 701-800
    (800, 100),  # 第 8 批 801-898（实际 98 个）
]

WAIT_INTERVAL_SEC = 20


def _query_python_rechunk_pids() -> list[int]:
    """用 PowerShell/CIM 查询正在运行的重切分 python 进程 PID"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'rechunk_lawdata' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", script],
            text=True, errors="ignore",
        )
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        m = re.search(r"\d+", line.strip())
        if m and int(m.group()) != os.getpid():
            pids.append(int(m.group()))
    return pids


def wait_running_batches():
    """等待所有正在运行的重切分进程结束（防止并发冲突）"""
    while True:
        pids = _query_python_rechunk_pids()
        if not pids:
            return
        print(f"[rechunk_auto] 检测到重切分进程仍在运行: {pids}，等待 {WAIT_INTERVAL_SEC}s ...")
        time.sleep(WAIT_INTERVAL_SEC)


def run_batch(offset: int, limit: int) -> bool:
    cmd = [
        "uv", "run", "python",
        os.path.join("scripts", "rechunk_lawdata.py"),
        "--offset", str(offset), "--limit", str(limit),
    ]
    print(f"\n{'=' * 60}")
    print(f"[rechunk_auto] 启动批次: offset={offset} limit={limit}  ({time.strftime('%H:%M:%S')})")
    print(f"{'=' * 60}", flush=True)
    proc = subprocess.run(cmd, cwd=ROOT)
    ok = proc.returncode == 0
    print(f"[rechunk_auto] 批次 offset={offset} {'成功' if ok else '失败(rc=%d)' % proc.returncode} "
          f"({time.strftime('%H:%M:%S')})", flush=True)
    return ok


def main():
    print(f"[rechunk_auto] 启动于 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[rechunk_auto] 剩余批次: {[b[0] for b in BATCHES]}")
    wait_running_batches()
    print("[rechunk_auto] 现有重切分进程已全部结束，开始接力", flush=True)

    results = []
    for offset, limit in BATCHES:
        ok = run_batch(offset, limit)
        results.append((offset, ok))
        # 单批失败不中断后续（force 幂等，可重跑），但记录汇总

    print(f"\n{'=' * 60}")
    print(f"[rechunk_auto] 全部批次执行完毕 {time.strftime('%H:%M:%S')}")
    print(f"[rechunk_auto] 汇总:")
    ok_all = True
    for offset, ok in results:
        print(f"    offset={offset:>4}: {'OK' if ok else 'FAILED'}")
        ok_all = ok_all and ok
    print(f"[rechunk_auto] 总体: {'全部成功' if ok_all else '存在失败批次（可单独重跑）'}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[rechunk_auto] 被用户中断")
        sys.exit(130)
