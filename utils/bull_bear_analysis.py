# -*- coding: utf-8 -*-
"""
牛熊分段表现分析（2010-2026 穿越两轮牛熊验证）
================================================
输入: flow_optimize_v6 的 json（含逐笔 trades）
分段: 按 A 股公认牛熊边界硬编码（沪深300/上证口径），对每段统计各战法
      「交易数 / 胜率 / 盈亏比 / 平均单笔 / Σ单笔」。

用法:
  python utils/bull_bear_analysis.py --json data/backtest/flow_optimize_v6_xxx.json
"""
import os
import sys
import json
import argparse
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A 股牛熊分段（起点含，终点不含；基于沪深300/上证公认拐点）
SEGMENTS = [
    ("2010-01-01", "2014-06-30", "震荡磨底期(2010-2014)"),
    ("2014-07-01", "2015-06-12", "杠杆大牛市(2014-2015)"),
    ("2015-06-13", "2016-01-31", "股灾三轮暴跌(2015)"),
    ("2016-02-01", "2018-01-31", "白马结构牛(2016-2017)"),
    ("2018-02-01", "2019-01-31", "贸易战熊(2018)"),
    ("2019-02-01", "2021-02-17", "核心资产牛(2019-2021)"),
    ("2021-02-18", "2024-09-23", "长熊阴跌(2021-2024)"),
    ("2024-09-24", "2026-12-31", "924行情与震荡(2024-09起)"),
]


def seg_stats(trades):
    if not trades:
        return {"n": 0, "win": None, "pl": None, "avg": None, "sum": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    aw = float(np.mean(wins)) if wins else 0.0
    al = float(np.mean(losses)) if losses else 0.0
    return {"n": len(pnls),
            "win": round(len(wins) / len(pnls) * 100, 1),
            "pl": round(aw / abs(al), 2) if al else None,
            "avg": round(float(np.mean(pnls)), 3),
            "sum": round(sum(pnls), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="flow_optimize_v6 json 路径")
    a = ap.parse_args()
    payload = json.load(open(a.json, encoding="utf-8"))
    trades = payload.get("trades") or []
    if not trades:
        raise SystemExit("该 json 无逐笔数据（旧版），请重跑 flow_optimize_v6")

    # 变体取 X2（B3 回调加仓版）与 base 对照，其他战法取 base（X 项不影响它们）
    lines = []
    lines.append(f"# 牛熊分段表现分析（{payload.get('start','2010-01-01')} ~ 今）\n")
    lines.append(f"> 数据: {a.json} | 切点(IS/OOS): {payload.get('split_date')}\n")

    opts = payload.get("opts", {})
    strats = ["B1", "B2", "B3", "砖型", "单针"]

    for seg_start, seg_end, seg_name in SEGMENTS:
        seg_tr = [t for t in trades
                  if seg_start <= (t.get("entry") or "") < seg_end]
        if not seg_tr:
            continue
        lines.append(f"\n## {seg_name}（{seg_start} ~ {seg_end}）\n")
        lines.append("| 战法 | 配置 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | Σ单笔% |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        # base 行 + X2 的 B3 行（若存在）
        for strat in strats:
            base_tr = [t for t in seg_tr if t.get("opt") == "base"
                       and t.get("strat") == strat]
            st = seg_stats(base_tr)
            if st["n"] == 0:
                continue
            lines.append(f"| {strat} | base | {st['n']} | {st['win']}% | "
                         f"{st['pl']} | {st['avg']} | {st['sum']} |")
        # B3 X2 专项（回调加仓版是本轮主角）
        x2_tr = [t for t in seg_tr if t.get("opt") == "X2"
                 and t.get("strat") == "B3"]
        if x2_tr:
            st = seg_stats(x2_tr)
            lines.append(f"| B3 | X2回调加仓 | {st['n']} | {st['win']}% | "
                         f"{st['pl']} | {st['avg']} | {st['sum']} |")
        # 组合视角：X2 全战法（B1/B2/B3/砖型/单针 用 X2 配置的整体表现）
        x2_all = [t for t in seg_tr if t.get("opt") == "X2"]
        if x2_all:
            st = seg_stats(x2_all)
            lines.append(f"| **X2组合** | 全战法 | **{st['n']}** | **{st['win']}%** | "
                         f"**{st['pl']}** | **{st['avg']}** | **{st['sum']}** |")

    out = os.path.splitext(a.json)[0] + "_bullbear.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n报告: {out}")


if __name__ == "__main__":
    main()
