# -*- coding: utf-8 -*-
"""2010 起全量补数任务（独立进程运行，防会话中断杀掉）。

用法: .venv/Scripts/python.exe utils/fetch_2010_task.py
进度写 data/backtest/fetch_2010_progress.log（每 200 只一行），完成后写 DONE。
断点安全: get_hist 覆盖判断（首日<=start+20天）天然支持续跑，已补的标的秒过。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import socket
socket.setdefaulttimeout(20)   # 防网络挂死（akshare 无自带 timeout，20s 后抛异常交给重试逻辑）

import sqlite3
from utils.backtest_data import ensure_cached

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "kline_cache.db")
LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "backtest", "fetch_2010_progress.log")


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    c = sqlite3.connect(DB)
    codes = [r[0] for r in c.execute("SELECT DISTINCT symbol FROM klines")]
    c.close()

    # ⚠ 关键修复：跳过「首日晚于 2010-02-01」的票（2010 年后上市的中小板/创业板等）。
    # 这些票的数据已完整（上市首日~2026-09），且 get_hist 的覆盖判断
    # 「首日<=start+20天」对它们永远不满足（上市日>start），会导致每次运行
    # 都重复全量拉取 → 卡死任务。它们对 2010 起的回测本就只贡献上市后的数据。
    c = sqlite3.connect(DB)
    todo, skipped = [], []
    for s in codes:
        r = c.execute("SELECT MIN(date) FROM klines WHERE symbol=?", (s,)).fetchone()
        if r and r[0] and r[0] <= "2010-02-01":
            todo.append(s)
        else:
            skipped.append(s)
    c.close()
    log(f"标的清单: 共 {len(codes)} 只 | 需补2010段: {len(todo)} 只 | "
        f"跳过(晚于2010-02上市, 数据已完整): {len(skipped)} 只")

    log(f"启动: {len(todo)} 只, 起点 2010-01-01, workers=4")
    t0 = time.time()

    # ensure_cached 内部 ThreadPoolExecutor(4)，但无进度输出 → 分批跑以记录进度
    B = 250
    for i in range(0, len(todo), B):
        batch = todo[i:i + B]
        written = ensure_cached(batch, start="2010-01-01", workers=4)
        el = time.time() - t0
        done = min(i + B, len(todo))
        log(f"进度 {done}/{len(todo)}  本批写入 {written} 根  "
            f"已用 {el/60:.1f}m  ETA {el/max(done,1)*(len(todo)-done)/60:.1f}m")

    log(f"DONE 全部完成: {len(todo)} 只处理")


if __name__ == "__main__":
    main()
