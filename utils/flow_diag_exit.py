# -*- coding: utf-8 -*-
"""
F3 诊断 · baseline B1 vs flow 离场口径对照（只测量, 不新增功能）
================================================================
背景: 同一批 B1 信号, `baseline B1 单独` = +278.2%, 而 `E2 纯流程` = +134.5%,
差 2.1 倍；flow 持有中位仅 1 天、91% 由盘中止损离场。本脚本逐项对照定位差异。

测量项:
  A) baseline B1: 离场原因占比 + 持有根数分布 + 「入场日即被止损」占比
  B) flow  (E2_env_off 同源): 离场原因占比 + 持有天数分布 + 减仓腿占比
  C) B1 信号日结构: 止损位(=T日低点) 相对入场价(T+1开盘) 的分布
     —— 若 stop≥entry 则入场日必被扫出（结构性次日止损嫌疑）
  D) baseline B1 独有离场（前高止盈/破前高放飞/破白线）与 flow 缺失项对照

Usage:
    python -u utils/flow_diag_exit.py [--limit N] [--tag TAG]
"""
import os
import sys
import json
import time
import socket
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

socket.setdefaulttimeout(20)

from utils.backtest import compute_signals, make_exit_rules, backtest_stock
from utils.backtest_data import DB_PATH, STD_COLUMNS
from utils.flow_backtest import FlowConfig, _prepare_block, run_flow_backtest_ledger
from utils.flow_experiments import universe_from_cache, load_df

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")


def _pct(a, q):
    return round(float(np.percentile(a, q)), 1) if len(a) else 0.0


def diag_exit(limit=0, tag=""):
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    conn = sqlite3.connect(DB_PATH, timeout=60)
    rules = make_exit_rules()
    flow_cfg = FlowConfig(env_macd_filter=False, resonance_on=False,
                          needle_patch=False)   # 与 baseline B1 最接近的同源配置

    base_reason = Counter()
    base_hold = []
    base_stop_d1 = 0          # 入场日(=持有1根)即止损
    base_n = 0

    flow_reason = Counter()
    flow_hold = []
    flow_reduce_legs = 0
    flow_n = 0

    gap_list = []             # (entry - stop)/entry
    gap_le0 = 0               # entry <= stop 的次数（入场即在止损下方）
    n_sig = 0

    t0 = time.time()
    for ci, code in enumerate(codes, 1):
        try:
            df = load_df(conn, code)
            if df is None or len(df) < 120:
                continue
            o = df["open"].astype(float).values
            l = df["low"].astype(float).values
            c = df["close"].astype(float).values
            sig = compute_signals(df)
            b1 = sig["B1"].fillna(False).astype(bool).values
            n = len(df)

            # ---- C) 信号日结构: stop=T日低点, entry=T+1开盘 ----
            for i in range(n - 1):
                if not b1[i]:
                    continue
                e = o[i + 1]
                s = l[i]
                if e <= 0:
                    continue
                n_sig += 1
                g = (e - s) / e
                gap_list.append(g)
                if e <= s:
                    gap_le0 += 1

            # ---- A) baseline B1 ----
            res = backtest_stock(sig, df, rules=rules)
            for tr in res.get("B1", []):
                base_n += 1
                base_reason[tr["原因"]] += 1
                base_hold.append(int(tr.get("持有", 0)))
                if tr["原因"] == "止损" and int(tr.get("持有", 0)) <= 1:
                    base_stop_d1 += 1

            # ---- B) flow (同源) ----
            blk = _prepare_block(df, flow_cfg, want_resonance=False)
            trades, _ = run_flow_backtest_ledger(None, code, code, flow_cfg,
                                                 rules, block=blk)
            for t in trades:
                flow_n += 1
                flow_reason[t.exit_reason] += 1
                hd = max((pd.Timestamp(t.exit_date) -
                          pd.Timestamp(t.b1_entry_date)).days, 1)
                flow_hold.append(hd)
                flow_reduce_legs += sum(
                    1 for x in t.tranches
                    if "减仓" in str(x.get("reason", ""))
                    or "预警" in str(x.get("reason", "")))
        except Exception:
            continue
        if ci % 500 == 0:
            print(f"  [exit] {ci}/{len(codes)}  base={base_n} flow={flow_n}  "
                  f"已用 {(time.time()-t0)/60:.1f}m", flush=True)
    conn.close()

    gap = np.array(gap_list) if gap_list else np.array([0.0])
    res = {
        "part": "exit",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_stocks": len(codes),
        "baseline_B1": {
            "n": base_n,
            "reason_share_pct": {k: round(v / max(base_n, 1) * 100, 2)
                                 for k, v in base_reason.most_common()},
            "reason_n": dict(base_reason.most_common()),
            "hold_bars": {"p10": _pct(base_hold, 10), "p25": _pct(base_hold, 25),
                          "median": _pct(base_hold, 50), "p75": _pct(base_hold, 75),
                          "p90": _pct(base_hold, 90), "mean": round(
                              float(np.mean(base_hold)), 1) if base_hold else 0.0},
            "stop_on_entry_day_share_pct": round(base_stop_d1 / max(base_n, 1) * 100, 2),
        },
        "flow_E2_env_off": {
            "n": flow_n,
            "reason_share_pct": {k: round(v / max(flow_n, 1) * 100, 2)
                                 for k, v in flow_reason.most_common()},
            "reason_n": dict(flow_reason.most_common()),
            "hold_days": {"p10": _pct(flow_hold, 10), "p25": _pct(flow_hold, 25),
                          "median": _pct(flow_hold, 50), "p75": _pct(flow_hold, 75),
                          "p90": _pct(flow_hold, 90), "mean": round(
                              float(np.mean(flow_hold)), 1) if flow_hold else 0.0},
            "reduce_leg_share_pct": round(flow_reduce_legs / max(flow_n, 1) * 100, 2),
        },
        "signal_structure": {
            "n_b1_signal": n_sig,
            "gap_entry_minus_stop_pct": {
                "p10": round(_pct(gap * 100, 10), 3), "median": round(_pct(gap * 100, 50), 3),
                "mean": round(float(gap.mean() * 100), 3),
                "p90": round(_pct(gap * 100, 90), 3)},
            "entry_le_stop_share_pct": round(gap_le0 / max(n_sig, 1) * 100, 2),
        },
        "elapsed_sec": round(time.time() - t0, 1),
    }
    jp = os.path.join(OUT_DIR, f"flow_diag_exit_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    b = res["baseline_B1"]; fl = res["flow_E2_env_off"]; sg = res["signal_structure"]
    L = [f"# baseline B1 vs flow 离场口径对照（{res['generated_at']}）", "",
         f"- 股票池 {len(codes)} 只；baseline B1 {b['n']} 笔；flow 同源 {fl['n']} 轮", "",
         "## A/B. 离场原因占比对照", "",
         "| 离场原因 | baseline B1 % | flow % |", "|---|---:|---:|"]
    keys = set(b["reason_share_pct"]) | set(fl["reason_share_pct"])
    for k in sorted(keys, key=lambda x: -(b["reason_share_pct"].get(x, 0)
                                          + fl["reason_share_pct"].get(x, 0))):
        L.append(f"| {k} | {b['reason_share_pct'].get(k,0)} | "
                 f"{fl['reason_share_pct'].get(k,0)} |")
    L += ["", "## 持有长度对照", "",
          "| 口径 | p10 | p25 | 中位 | p75 | p90 | 均值 |", "|---|---:|---:|---:|---:|---:|---:|",
          f"| baseline B1 持有(根) | {b['hold_bars']['p10']} | {b['hold_bars']['p25']} | "
          f"{b['hold_bars']['median']} | {b['hold_bars']['p75']} | {b['hold_bars']['p90']} | "
          f"{b['hold_bars']['mean']} |",
          f"| flow 持有(日) | {fl['hold_days']['p10']} | {fl['hold_days']['p25']} | "
          f"{fl['hold_days']['median']} | {fl['hold_days']['p75']} | {fl['hold_days']['p90']} | "
          f"{fl['hold_days']['mean']} |", "",
          f"- baseline B1「入场日即止损(持有≤1根)」占比: **{b['stop_on_entry_day_share_pct']}%**",
          f"- flow 减仓腿占比: **{fl['reduce_leg_share_pct']}%**", "",
          "## C. 信号日结构（stop=T日低点, entry=T+1开盘）", "",
          f"- (entry − stop)/entry: p10 {sg['gap_entry_minus_stop_pct']['p10']}% / "
          f"中位 {sg['gap_entry_minus_stop_pct']['median']}% / "
          f"均值 {sg['gap_entry_minus_stop_pct']['mean']}% / "
          f"p90 {sg['gap_entry_minus_stop_pct']['p90']}%",
          f"- **entry ≤ stop（入场即处止损下方, 入场日必被扫出）占比: "
          f"{sg['entry_le_stop_share_pct']}%**", ""]
    mp = os.path.join(OUT_DIR, f"flow_diag_exit_{tag}.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[exit] base={b['n']} flow={fl['n']}  报告 {mp}")
    return res


def main():
    import argparse
    ap = argparse.ArgumentParser(description="baseline B1 vs flow 离场口径诊断")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tag", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    diag_exit(limit=args.limit, tag=args.tag)


if __name__ == "__main__":
    main()
