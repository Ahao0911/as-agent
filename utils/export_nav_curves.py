# -*- coding: utf-8 -*-
"""导出逐日净值曲线（供 README / GitHub Pages 演示页作图）

口径与《最终数据_定稿版.md》完全一致：
  · 全市场 4935 只 · 前复权 · 活跃市值 regime 取 T-1（无未来函数）
  · 买入 = 信号日次日开盘 · 成本往返 0.072% · B3 固定持有 10 交易日，其余战法规则驱动
  · 每日净值法：全部信号等权并行、不跳单
另导出【全市场等权基准】：同一股票池、每日等权收益复利 —— 比沪深300 更公平的对照
（同池同偏差，无指数成分股差异）。

输出:
  data/backtest/nav_curves.json （逐日净值原始数据）
  assets/chart_17y.png          （17 年曲线，对数轴）
  assets/chart_2024.png         （这两年曲线）
  assets/chart_old_vs_new.png   （新旧口径对比）
"""
import os
import sys
import json
import sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from utils.backtest import compute_signals, make_exit_rules, _find_exit
from utils.backtest_data import DB_PATH
from utils.flow_env_compare import load_df, universe_from_cache, load_amv_state
from utils.nav_backtest import build_extra

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STRATS = ["B1", "B2", "B3", "砖型", "单针"]
COST = 0.072 / 100.0
B3_HOLD = 10
SEGS = [("y17", "2010-01-01", "2026-09-30", "2010.1–2026.9（17 年）"),
        ("y2", "2024-01-01", "2026-09-30", "2024.1–2026.9")]

# 旧口径（已作废，仅用于对比图说明"为什么不能用"）
OLD_2024 = {"B1": 188.5, "B2": -18.9, "B3": 90.9, "砖型": 48.5, "单针": -35.8}


def main(limit=0):
    codes = universe_from_cache()
    if limit and limit > 0:
        codes = codes[:limit]
    rules = make_exit_rules()
    amv = load_amv_state()

    # strat -> seg -> {date: [rets]}；基准也用同一结构
    bag = {s: {n: defaultdict(list) for n, _, _, _ in SEGS} for s in STRATS}
    bench = {n: defaultdict(list) for n, _, _, _ in SEGS}

    for ci, code in enumerate(codes):
        conn = sqlite3.connect(DB_PATH, timeout=60)
        df = load_df(conn, code)
        conn.close()
        if df is None or len(df) < 120:
            continue
        close = df["close"].astype(float).values
        op = df["open"].astype(float).values
        n = len(df)
        idx = df.index

        # ---- 基准：个股自身日收益，按日汇总等权 ----
        prev = np.roll(close, 1)
        prev[0] = np.nan
        dr = close / prev - 1.0
        for j in range(1, n):
            if not np.isfinite(dr[j]):
                continue
            d = idx[j]
            for nm, s0, s1, _ in SEGS:
                if pd.Timestamp(s0) <= d <= pd.Timestamp(s1):
                    bench[nm][d].append(float(dr[j]))

        # ---- 战法净值 ----
        sig = compute_signals(df)
        extra = build_extra(df)
        v = df["volume"].astype(float).values
        white = np.asarray(extra["white"], dtype=float)
        yellow = np.asarray(extra["yellow"], dtype=float)
        am = amv["regime"].reindex(idx).ffill().shift(1).fillna(False).values

        for s in STRATS:
            sv = sig[s].values
            rule = rules[s]
            for i in range(n - 1):
                if not sv[i] or not am[i]:
                    continue
                b = i + 1
                if b >= n:
                    continue
                if s == "B3":
                    e = min(b + B3_HOLD - 1, n - 1)
                    ep = close[e]
                else:
                    e, ep, _ = _find_exit(b, df, rule, extra)
                    if e < b:
                        continue
                if not (np.isfinite(op[b]) and op[b] > 0):
                    continue
                if not (np.isfinite(ep) and ep > 0):
                    continue
                L = e - b + 1
                if L == 1:
                    rets = [ep / op[b] - 1 - COST]
                else:
                    rets = [close[b] / op[b] - 1 - COST / 2]
                    for t in range(b + 1, e):
                        rets.append(close[t] / close[t - 1] - 1)
                    rets.append(ep / close[e - 1] - 1 - COST / 2)
                if not all(np.isfinite(r) for r in rets):
                    continue
                for k, r in enumerate(rets):
                    d = idx[b + k]
                    for nm, s0, s1, _ in SEGS:
                        if pd.Timestamp(s0) <= d <= pd.Timestamp(s1):
                            bag[s][nm][d].append(float(np.clip(r, -0.99, 10.0)))
        if (ci + 1) % 1000 == 0:
            print(f"  {ci+1}/{len(codes)}", flush=True)

    # ---- 合成净值序列 ----
    out = {"meta": {"generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
                    "universe": len(codes), "cost": "0.072%", "buy": "signal day next open",
                    "segments": {n: d for n, _, _, d in SEGS}},
           "segments": {}}
    for nm, s0, s1, label in SEGS:
        seg = {"label": label, "series": {}}
        for name, series in [(k, bag[k][nm]) for k in STRATS] + [("基准", bench[nm])]:
            days = sorted(series.keys())
            nav, curve = 1.0, []
            for d in days:
                r = float(np.mean(series[d]))
                nav *= (1.0 + r)
                curve.append([d.strftime("%Y-%m-%d"), round(nav, 4)])
            seg["series"][name] = curve
        out["segments"][nm] = seg

    os.makedirs(os.path.join(ROOT, "data", "backtest"), exist_ok=True)
    jp = os.path.join(ROOT, "data", "backtest", "nav_curves.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[JSON] {jp}")

    # ---- 打印汇总 ----
    for nm, s0, s1, label in SEGS:
        print(f"\n【{label}】")
        for name in STRATS + ["基准"]:
            c = out["segments"][nm]["series"][name]
            tot = (c[-1][1] - 1) * 100
            print(f"  {name:<5} 总收益 {tot:>12.1f}%")

    plot(out)
    return out


def plot(out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False
    adir = os.path.join(ROOT, "assets")
    os.makedirs(adir, exist_ok=True)
    colors = {"B1": "#d62728", "B2": "#ff7f0e", "B3": "#9467bd",
              "砖型": "#1f77b4", "单针": "#2ca02c", "基准": "#7f7f7f"}

    for nm, fname, logy, title in [
            ("y17", "chart_17y.png", True, "净值曲线 2010.1–2026.9（17 年，对数轴）"),
            ("y2", "chart_2024.png", False, "净值曲线 2024.1–2026.9")]:
        seg = out["segments"][nm]
        fig, ax = plt.subplots(figsize=(11, 5.6), dpi=140)
        for name, c in seg["series"].items():
            x = [pd.Timestamp(p[0]) for p in c]
            y = [p[1] for p in c]
            lw = 2.4 if name != "基准" else 1.6
            ls = "--" if name == "基准" else "-"
            ax.plot(x, y, label=f"{name}（{(y[-1]-1)*100:+.1f}%）",
                    color=colors.get(name, "#333"), lw=lw, ls=ls)
        if logy:
            ax.set_yscale("log")
        ax.set_title(title, fontsize=13, fontweight="bold")
        ax.set_ylabel("净值（起点=1.0）")
        ax.grid(alpha=.25, ls=":")
        ax.legend(fontsize=9, ncol=3)
        ax.axhline(1.0, color="#999", lw=.8)
        fig.tight_layout()
        p = os.path.join(adir, fname)
        fig.savefig(p)
        plt.close(fig)
        print(f"[图] {p}")

    # 新旧口径对比
    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=140)
    xs = np.arange(len(STRATS))
    newv = [(out["segments"]["y2"]["series"][s][-1][1] - 1) * 100 for s in STRATS]
    oldv = [OLD_2024[s] for s in STRATS]
    ax.bar(xs - 0.2, oldv, 0.4, label="旧口径（算法缺陷，已作废）",
           color="#bbbbbb", edgecolor="#888")
    ax.bar(xs + 0.2, newv, 0.4, label="正确口径（每日净值法）",
           color=["#d62728", "#ff7f0e", "#9467bd", "#1f77b4", "#2ca02c"])
    for i, (o, nw) in enumerate(zip(oldv, newv)):
        ax.text(i - 0.2, o + (6 if o >= 0 else -14), f"{o:+.1f}%", ha="center", fontsize=8.5)
        ax.text(i + 0.2, nw + (6 if nw >= 0 else -14), f"{nw:+.1f}%", ha="center",
                fontsize=8.5, fontweight="bold")
    ax.axhline(0, color="#444", lw=.9)
    ax.set_xticks(xs)
    ax.set_xticklabels(STRATS)
    ax.set_ylabel("2024-01 ~ 2026-09 总收益 %")
    ax.set_title("新旧口径对比：旧算法的收益率全部不可用", fontsize=12.5, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(alpha=.25, ls=":", axis="y")
    fig.tight_layout()
    p = os.path.join(adir, "chart_old_vs_new.png")
    fig.savefig(p)
    plt.close(fig)
    print(f"[图] {p}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    main(a.limit)
