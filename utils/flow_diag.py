# -*- coding: utf-8 -*-
"""
F3 诊断脚本（问题2 共振池指标诊断 / 问题3 持有段与离场诊断）
================================================================
背景: flow_exp_2026-09-11 跑批暴露两处异常——
  问题2: E3 桶 0(26157) ≫ 1(18346) ≫ 2(1461) > 3+(1)，命中数极度偏斜；
  问题3: B2 加仓 / 单针补票几乎 0 触发，E4 与 E2 同值，持有段疑似被过早打断。

本脚本只做**只读诊断**（零网络，复用 kline_cache.db + 冻结内核），产出:
  --part resonance  问题2: 六项共振在 B1 信号日的单独命中率/两两共现/桶分布,
                    并给出「数学互斥」证据（B1 强制缩量 → 倍量柱不可能命中）。
  --part hold       问题3: B1→离场的持有天数分布 + 离场原因占比 + 状态到达分布,
                    定位离场第 3 级（红翻绿/红砖缩短）是否过于敏感。

Usage:
    python -u utils/flow_diag.py --part resonance [--limit N] [--tag TAG]
    python -u utils/flow_diag.py --part hold      [--limit N] [--tag TAG]
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

from utils.backtest import make_exit_rules
from utils.backtest_data import DB_PATH
from utils.flow_backtest import FlowConfig, _prepare_block, run_flow_backtest_ledger
from utils.resonance import RESONANCE_ITEMS
from utils.vol_price import (red_fat_green_thin, volume_shrink_pullback,
                             shrink_yin_up, reversal_engulf, long_yin_short_zhu,
                             shrink_to_floor)
from utils.flow_experiments import universe_from_cache, load_df

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")

# 与 B1 语境冲突的共振项（B1 = 缩量 v<v20 & 回调涨幅[-2,1.8]%）
CONFLICT_ITEMS = ["倍量柱", "关键K", "MACD顺周期"]      # 需放量 / 价涨
CORE_ITEMS = ["砖型翻红", "MACD底背离", "缩量止跌"]      # 与回调/缩量相容


def _bucket(s):
    return "0" if s <= 0 else ("1" if s == 1 else ("2" if s == 2 else "3+"))


# ============================================================
# 问题2: 共振池指标诊断
# ============================================================

def diag_resonance(limit=0, tag=""):
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    conn = sqlite3.connect(DB_PATH, timeout=60)
    cfg = FlowConfig(env_macd_filter=True, resonance_on=True, needle_patch=False)

    item_hits_all = Counter()      # 指标在【全区间任意日】命中次数（无条件基准）
    item_hits_b1 = Counter()       # 指标在【B1 信号日】命中次数
    pair_b1 = Counter()            # B1 日的两两共现
    bucket_b1 = Counter()          # B1 日的命中数分桶
    core_bucket_b1 = Counter()     # 仅用 CORE_ITEMS 的分桶
    n_b1 = 0
    viol_shrink = 0                # B1 信号日竟然 v>=v20 的次数（理论应为 0）
    viol_heavy = 0                 # B1 信号日竟然命中「倍量柱」的次数（理论应为 0）
    t0 = time.time()

    for c_i, code in enumerate(codes, 1):
        try:
            df = load_df(conn, code)
            if df is None or len(df) < 120:
                continue
            blk = _prepare_block(df, cfg, want_resonance=True)
            m = blk["res_matrix"]
            if m is None:
                continue
            cols = {c: m[c].values for c in RESONANCE_ITEMS}
            # legacy: v3 已从共振池剔除「缩量止跌」，诊断中仍单独核算（供对比证据）
            cols["缩量止跌"] = (long_yin_short_zhu(df) | shrink_to_floor(df)).values
            all_items = RESONANCE_ITEMS + ["缩量止跌"]
            for c in all_items:
                item_hits_all[c] += int(np.nansum(cols[c]))
            v = blk["v"].values
            v20 = blk["v20"].values
            b1 = blk["b1_sig"].values
            env = blk["env_ok"].values
            for i in range(len(b1)):
                if not (b1[i] and env[i]):
                    continue
                n_b1 += 1
                if not np.isnan(v20[i]) and v[i] >= v20[i]:
                    viol_shrink += 1
                row = {c: bool(cols[c][i]) for c in all_items}
                hits = [c for c in all_items if row[c]]
                if row["倍量柱"]:
                    viol_heavy += 1
                for c in hits:
                    item_hits_b1[c] += 1
                for a in range(len(hits)):
                    for b in range(a + 1, len(hits)):
                        pair_b1[tuple(sorted((hits[a], hits[b])))] += 1
                bucket_b1[_bucket(len(hits))] += 1
                core_s = sum(1 for c in CORE_ITEMS if row[c])
                core_bucket_b1[_bucket(core_s)] += 1
        except Exception:
            continue
        if c_i % 500 == 0:
            print(f"  [resonance] {c_i}/{len(codes)}  B1信号 {n_b1}  "
                  f"已用 {(time.time()-t0)/60:.1f}m", flush=True)
    conn.close()

    item_rate_b1 = {c: round(item_hits_b1[c] / n_b1 * 100, 3) if n_b1 else 0.0
                    for c in all_items}

    res = {
        "part": "resonance",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_stocks": len(codes), "n_b1_signals": n_b1,
        "item_hits_b1": {c: item_hits_b1[c] for c in all_items},
        "item_rate_b1_pct": item_rate_b1,
        "item_hits_all_days": {c: item_hits_all[c] for c in all_items},
        "bucket_b1": dict(bucket_b1),
        "core_bucket_b1": dict(core_bucket_b1),
        "pair_b1_top": {" + ".join(k): v for k, v in pair_b1.most_common(15)},
        "proof": {
            "conflict_items": CONFLICT_ITEMS,
            "core_items": CORE_ITEMS,
            "b1_violate_shrink_n": viol_shrink,
            "b1_hit_heavyvol_n": viol_heavy,
            "note": "B1 定义强制 v<v20；倍量柱需 v>v20*1.8/前量*2 → 在 B1 信号日"
                    "数学互斥（viol_heavy 应为 0）。关键K需大阳/涨停 vs B1涨幅≤1.8% "
                    "亦近乎互斥；MACD顺周期需 c>c5 价涨 vs B1 回调，结构性冲突。",
        },
        "elapsed_sec": round(time.time() - t0, 1),
    }
    jp = os.path.join(OUT_DIR, f"flow_diag_resonance_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    L = [f"# 问题2 · 共振池指标诊断（{res['generated_at']}）", "",
         f"- 股票池: {res['n_stocks']} 只；B1 信号日（多头环境）样本 **{n_b1}** 条", "",
         "## 1. 六项共振在 B1 信号日的单独命中率", "",
         "| 共振项 | B1 信号日命中次数 | B1 日命中率% | 全区间命中次数 | 冲突? |",
         "|---|---:|---:|---:|---|"]
    for c in all_items:
        flag = "⚠️ 与B1冲突" if c in CONFLICT_ITEMS else "兼容"
        L.append(f"| {c} | {item_hits_b1[c]} | {item_rate_b1[c]} | "
                 f"{item_hits_all[c]} | {flag} |")
    L += ["", "## 2. 命中数分桶分布（当前 6 项池）", "",
          "| 桶(命中数) | 轮数 | 占比 |", "|---|---:|---:|"]
    tot = max(n_b1, 1)
    for b in ("0", "1", "2", "3+"):
        L.append(f"| {b} | {bucket_b1.get(b,0)} | {bucket_b1.get(b,0)/tot*100:.2f}% |")
    L += ["", "## 3. 仅用「兼容项」子集的桶分布（砖型翻红/MACD底背离/缩量止跌）", "",
          "| 桶(命中数) | 轮数 | 占比 |", "|---|---:|---:|"]
    for b in ("0", "1", "2", "3+"):
        L.append(f"| {b} | {core_bucket_b1.get(b,0)} | {core_bucket_b1.get(b,0)/tot*100:.2f}% |")
    L += ["", "## 4. 两两共现 Top", ""]
    for k, v in res["pair_b1_top"].items():
        L.append(f"- {k}: {v}")
    L += ["", "## 5. 数学互斥证据", "",
          f"- B1 信号日违反「缩量」定义 (v≥v20) 次数: **{viol_shrink}**（应恒为 0）",
          f"- B1 信号日命中「倍量柱」次数: **{viol_heavy}**（应恒为 0，因 B1 强制缩量）",
          f"- 冲突项: {', '.join(CONFLICT_ITEMS)}（需放量/价涨，与 B1 回调语境相反）",
          f"- 兼容项: {', '.join(CORE_ITEMS)}（与回调/缩量相容）", ""]
    mp = os.path.join(OUT_DIR, f"flow_diag_resonance_{tag}.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[resonance] B1信号 {n_b1}  报告 {mp}")
    return res


# ============================================================
# 问题2（续）: 备选共振项在 B1 信号日的可用性评估
# ============================================================

# B1 语境相容的备选项候选（缩量/回调/承接类，理论不与 B1 冲突）
CANDIDATE_ITEMS = ["缩量止跌", "砖型翻红", "MACD底背离",
                   "红肥绿瘦", "放量缩量回调", "缩量阴线价升", "反包", "长阴短柱"]


def diag_candidates(limit=0, tag=""):
    """统计备选共振项在 B1 信号日的命中率 + 建议池的分桶分布。

    目的: 现有 6 项池剔除冲突项后仍偏斜（桶 3+ ≈0），需确认是否存在
    "既兼容 B1、又足够高频、可稳定共现" 的替代项组合。
    """
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    conn = sqlite3.connect(DB_PATH, timeout=60)
    cfg = FlowConfig(env_macd_filter=True, resonance_on=True, needle_patch=False)

    hits_b1 = Counter()
    pair_b1 = Counter()
    pool_bucket = Counter()
    n_b1 = 0
    # 建议池（先用兼容且高频的 4 项，跑完按实测可微调）
    POOL = ["缩量止跌", "砖型翻红", "红肥绿瘦", "放量缩量回调"]
    t0 = time.time()

    for c_i, code in enumerate(codes, 1):
        try:
            df = load_df(conn, code)
            if df is None or len(df) < 120:
                continue
            blk = _prepare_block(df, cfg, want_resonance=True)
            m = blk["res_matrix"]
            if m is None:
                continue
            # 备选项序列
            extra = {
                "缩量止跌": (long_yin_short_zhu(df) | shrink_to_floor(df)).values,
                "红肥绿瘦": red_fat_green_thin(df).values,
                "放量缩量回调": volume_shrink_pullback(df).values,
                "缩量阴线价升": shrink_yin_up(df).values,
                "反包": reversal_engulf(df).values,
                "长阴短柱": long_yin_short_zhu(df).values,
            }
            all_items = {c: m[c].values for c in RESONANCE_ITEMS}
            all_items.update(extra)
            b1 = blk["b1_sig"].values
            env = blk["env_ok"].values
            for i in range(len(b1)):
                if not (b1[i] and env[i]):
                    continue
                n_b1 += 1
                row = {c: bool(all_items[c][i]) for c in CANDIDATE_ITEMS}
                for c in CANDIDATE_ITEMS:
                    if row[c]:
                        hits_b1[c] += 1
                ph = [c for c in POOL if row[c]]
                for a in range(len(ph)):
                    for b in range(a + 1, len(ph)):
                        pair_b1[tuple(sorted((ph[a], ph[b])))] += 1
                pool_bucket[_bucket(len(ph))] += 1
        except Exception:
            continue
        if c_i % 500 == 0:
            print(f"  [candidates] {c_i}/{len(codes)}  B1 {n_b1}  "
                  f"已用 {(time.time()-t0)/60:.1f}m", flush=True)
    conn.close()

    rate = {c: round(hits_b1[c] / n_b1 * 100, 2) if n_b1 else 0.0
            for c in CANDIDATE_ITEMS}
    tot = max(n_b1, 1)
    res = {
        "part": "candidates",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_stocks": len(codes), "n_b1_signals": n_b1,
        "candidate_items": CANDIDATE_ITEMS,
        "candidate_rate_b1_pct": rate,
        "proposed_pool": POOL,
        "proposed_pool_bucket_b1": dict(pool_bucket),
        "proposed_pair_b1": {" + ".join(k): v for k, v in pair_b1.most_common(10)},
        "elapsed_sec": round(time.time() - t0, 1),
    }
    jp = os.path.join(OUT_DIR, f"flow_diag_candidates_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    L = [f"# 问题2（续） · 备选共振项评估（{res['generated_at']}）", "",
         f"- B1 信号日样本 {n_b1} 条（{len(codes)} 只）", "",
         "## 1. 备选项在 B1 信号日的命中率", "",
         "| 备选项 | 命中次数 | 命中率% |", "|---|---:|---:|"]
    for c in sorted(CANDIDATE_ITEMS, key=lambda x: -rate[x]):
        L.append(f"| {c} | {hits_b1[c]} | {rate[c]} |")
    L += ["", f"## 2. 建议池 {POOL} 的分桶分布", "",
          "| 桶(命中数) | 轮数 | 占比 |", "|---|---:|---:|"]
    for b in ("0", "1", "2", "3+"):
        L.append(f"| {b} | {pool_bucket.get(b,0)} | {pool_bucket.get(b,0)/tot*100:.2f}% |")
    L += ["", "## 3. 建议池两两共现", ""]
    for k, v in res["proposed_pair_b1"].items():
        L.append(f"- {k}: {v}")
    L.append("")
    mp = os.path.join(OUT_DIR, f"flow_diag_candidates_{tag}.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[candidates] B1 {n_b1}  报告 {mp}")
    return res


# ============================================================
# 问题3: 持有段与离场诊断
# ============================================================

def diag_hold(limit=0, tag=""):
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    conn = sqlite3.connect(DB_PATH, timeout=60)
    cfg = FlowConfig(env_macd_filter=True, resonance_on=True, needle_patch=True,
                     needle_stop_pct=0.03, tranche={"B1": 0.5, "B2": 0.3, "needle": 0.2})
    rules = make_exit_rules()

    reason = Counter()
    holds_days = []
    state_reached = Counter()
    b2_rounds = needle_rounds = b3_rounds = 0
    reason_hold = defaultdict(list)          # 离场原因 → 持有天数样本
    t0 = time.time()

    for c_i, code in enumerate(codes, 1):
        try:
            df = load_df(conn, code)
            if df is None or len(df) < 120:
                continue
            blk = _prepare_block(df, cfg, want_resonance=True)
            trades, led = run_flow_backtest_ledger(None, code, code, cfg, rules, block=blk)
            for t in trades:
                reason[t.exit_reason] += 1
                hd = max((pd.Timestamp(t.exit_date) - pd.Timestamp(t.b1_entry_date)).days, 1)
                holds_days.append(hd)
                reason_hold[t.exit_reason].append(hd)
                if t.b3_date:
                    state_reached["HOLD_B3"] += 1
                    b3_rounds += 1
                elif t.b2_entry_date:
                    state_reached["HOLD_B2"] += 1
                else:
                    state_reached["HOLD_B1"] += 1
                if t.b2_entry_date:
                    b2_rounds += 1
                if t.needle_entry_date:
                    needle_rounds += 1
        except Exception:
            continue
        if c_i % 500 == 0:
            print(f"  [hold] {c_i}/{len(codes)}  轮数 {len(holds_days)}  "
                  f"已用 {(time.time()-t0)/60:.1f}m", flush=True)
    conn.close()

    n = len(holds_days)
    h = np.array(holds_days) if holds_days else np.array([0.0])
    def _p(q):
        return round(float(np.percentile(h, q)), 1)
    total = max(n, 1)
    reason_tbl = {k: {"n": v, "share_pct": round(v / total * 100, 2),
                      "median_hold_d": round(float(np.median(reason_hold[k])), 1)}
                  for k, v in reason.most_common()}

    res = {
        "part": "hold",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_stocks": len(codes), "n_rounds": n,
        "hold_days": {"min": _p(0), "p10": _p(10), "p25": _p(25), "median": _p(50),
                      "p75": _p(75), "p90": _p(90), "max": _p(100),
                      "mean": round(float(h.mean()), 1)},
        "state_reached": dict(state_reached),
        "b2_add_rounds": b2_rounds, "needle_patch_rounds": needle_rounds,
        "b3_rounds": b3_rounds,
        "exit_reasons": reason_tbl,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    jp = os.path.join(OUT_DIR, f"flow_diag_hold_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    L = [f"# 问题3 · 持有段与离场诊断（{res['generated_at']}）", "",
         f"- 股票池 {len(codes)} 只；轮数 **{n}**", "",
         "## 1. B1 入场→离场 持有天数分布", "",
         f"- min {_p(0)} / p10 {_p(10)} / p25 {_p(25)} / **median {_p(50)}** / "
         f"p75 {_p(75)} / p90 {_p(90)} / max {_p(100)}；mean {round(float(h.mean()),1)} 天", "",
         "## 2. 状态到达分布", "",
         "| 状态 | 轮数 | 占比 |", "|---|---:|---:|"]
    for k in ("HOLD_B1", "HOLD_B2", "HOLD_B3"):
        v = state_reached.get(k, 0)
        L.append(f"| {k} | {v} | {v/total*100:.2f}% |")
    L += ["", f"- B2 加仓轮数: **{b2_rounds}**；单针补票轮数: **{needle_rounds}**", "",
          "## 3. 离场原因占比（按频次）", "",
          "| 离场原因 | 轮数 | 占比% | 中位持有天数 |", "|---|---:|---:|---:|"]
    for k, v in reason_tbl.items():
        L.append(f"| {k} | {v['n']} | {v['share_pct']} | {v['median_hold_d']} |")
    L.append("")
    mp = os.path.join(OUT_DIR, f"flow_diag_hold_{tag}.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[hold] 轮数 {n}  报告 {mp}")
    return res


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    ap = argparse.ArgumentParser(description="F3 诊断（问题2 共振 / 问题3 持有）")
    ap.add_argument("--part", choices=["resonance", "candidates", "hold"], required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tag", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    if args.part == "resonance":
        diag_resonance(limit=args.limit, tag=args.tag)
    elif args.part == "candidates":
        diag_candidates(limit=args.limit, tag=args.tag)
    else:
        diag_hold(limit=args.limit, tag=args.tag)


if __name__ == "__main__":
    main()
