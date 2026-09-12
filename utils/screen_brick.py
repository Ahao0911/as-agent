"""
知行砖型图选股 — 超短翻红信号（XG）批量筛选
配合使用：黄白线多头区间 + 砖型图翻红 = 双确认

用法:
    python3 utils/screen_brick.py                    # 内置股票池
    python3 utils/screen_brick.py 600519 000858     # 自定义代码
    python3 utils/screen_brick.py --file 股票池.txt  # 从文件读
"""
import sys
import os
import time
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from utils.screen_bull import DEFAULT_POOL, fetch_kline
from utils.brick import brick_signal
from utils.indicators import analyze_dual_line


def screen(pool):
    """逐只分析砖型图 + 黄白线，返回组合信号"""
    hits, strong_hits, reds, others, errors = [], [], [], [], []
    for code in pool:
        code = str(code).zfill(6)
        df = fetch_kline(code)
        if df is None:
            errors.append((code, "无数据"))
            continue
        try:
            dual = analyze_dual_line(df)
            brick = brick_signal(df, dual)
            item = {
                "代码": code,
                "砖型图": brick["砖型图值"],
                "状态": brick["状态"],
                "连续红砖": brick["连续红砖"],
                "绿翻强红": "★" if brick["绿翻强红"] else "",
                "翻红": "◆" if brick["今日翻红"] else "",
                "近5日翻红": brick["近5日翻红次数"],
                "多头区间": dual.get("多头区间", False) if isinstance(dual, dict) else False,
                "白线": dual.get("白线", 0),
                "黄线": dual.get("黄线", 0),
                "偏离度": round((df["close"].iloc[-1] - dual.get("黄线", df["close"].iloc[-1])) / max(dual.get("黄线", 1), 0.01) * 100, 2),
                "J值": dual.get("J值", 0),
            }
            if brick["绿翻强红"]:
                strong_hits.append(item)
            elif brick["今日翻红"]:
                hits.append(item)
            elif brick["状态"] == "红柱↑":
                reds.append(item)
            else:
                others.append(item)
        except Exception as e:
            errors.append((code, str(e)[:50]))
        time.sleep(random.uniform(0.2, 0.5))

    return strong_hits, hits, reds, others, errors


def main():
    args = sys.argv[1:]
    pool = DEFAULT_POOL
    if args:
        if args[0] == "--file" and len(args) > 1:
            with open(args[1], "r", encoding="utf-8") as f:
                pool = [line.strip() for line in f if line.strip()]
        else:
            pool = args

    print(f"股票池: {len(pool)} 只，砖型图翻红扫描...\n")
    strong_hits, hits, reds, others, errors = screen(pool)

    def show(title, items, extra=""):
        print("=" * 72)
        print(f"{title}: {len(items)} 只 {extra}")
        print("=" * 72)
        if not items:
            print("  (无)")
            return
        print(f"{'代码':<8}{'砖型图':>7}{'状态':>5}{'强红':>4}{'翻红':>4}{'连红':>4}{'白线':>8}{'黄线':>8}{'多头':>4}{'J值':>7}")
        for it in items:
            bull = "✓" if it["多头区间"] else "✗"
            print(f"{it['代码']:<8}{it['砖型图']:>7.2f}{it['状态']:>6}{it['绿翻强红']:>4}{it['翻红']:>4}{it['连续红砖']:>4}{it['白线']:>8.2f}{it['黄线']:>8.2f}{bull:>5}{it['J值']:>7.1f}")
        print()

    show("★★ 绿翻强红（强买点）", strong_hits)
    show("★ 今日翻红（普通）", hits)
    show("红柱延续（上升中）", reds)
    show("绿柱/平（观望）", others)

    # 双确认：绿翻强红 + 多头区间
    dual_hits = [h for h in strong_hits + hits if h["多头区间"]]
    if dual_hits:
        print("═" * 72)
        print(f"◆ 最优组合（翻红/强红 + 黄白线多头区间）: {len(dual_hits)} 只 ← 首选")
        print("═" * 72)
        for it in dual_hits:
            tag = "绿翻强红" if it["绿翻强红"] else "翻红"
            print(f"  {it['代码']}  {tag}  砖型图{it['砖型图']}  白线{it['白线']} > 黄线{it['黄线']}")

    if errors:
        print(f"\n⚠ 失败: {len(errors)} 只")
        print("  " + "  ".join(f"{c}" for c, _ in errors[:10]))


if __name__ == "__main__":
    main()
