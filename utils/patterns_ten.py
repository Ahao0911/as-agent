# -*- coding: utf-8 -*-
"""
十大完美图形判据（按需触发，非买卖信号）
==========================================
把《图形买点体系 10 张经典图形》的 4 大类图形，代码化为可执行的数值判据。

⚠️ 重要定位
------------
本模块输出的是**形态匹配度**（命中 N/M 条判据），**不是买入信号**。
- 它是「看一眼这只票长得像不像完美图」的辅助工具
- 真实买入决策必须叠加：图形买点体系止损（盘中 -4%）+ 仓位规则（组预算 × 50%）
- 判据阈值来自公开文字版定义 + 架构师定稿，**未经样本外回测验证**，
  因此默认只做「命中率报告」，不做自动下单

📖 判据来源
-----------
公开渠道《图形买点体系 10 张经典图形》文字定义（4 大类 + 11 案例 + 4 判据），
经架构师写入 ARCH 文档（2026-09-10 v1.2）：

① 完美缩量：放量穿黄线 → 缩半量回调 → J≤-12 → 白/黄线上方盘整
② N型回调：沿白线、回调不破白线、缩量
③ 一直拉升回调：一波流 ≥70% + 顶部不放量 + 回调至白线
④ 长期盘整洗盘：顶部大风车量后长盘整 ≥20日、未破黄线

📌 用法
-------
    from utils.patterns_ten import match_all, match_one

    r = match_all("688799")          # 单只票 → 4 类命中情况
    r = match_all(["688799", "600601"])
    df_out = match_to_dataframe(["688799", ...])   # 批量 → DataFrame

命令行：
    python utils/patterns_ten.py 688799 600601 002940

⚠️ 已知限制（2026-09-10 实测）
--------------------------------
**数据窗口只有 1 年（腾讯源硬上限 261 条）**，这对本模块影响很大：

- 案例票（文字版里那 12 只）的**原始形态多发生在 2025 年及更早**，而窗口只到
  2025-08-18。实测 600601 / 600366 / 600184 三只 N 型案例票，在窗口内整体
  **是下跌的**（600184 从 33.55 → 18.99），窗口里根本不存在当年的 N 型结构。
- 因此**不要把「案例票现在跑不出高分」理解成「判据错了」**——大概率是窗口
  没覆盖到案例期。
- 同理，本模块**未做过样本外回测**，阈值（-12 / 70% / 20日 等）属于「按文字版
  定义直译」，未经数据验证。

**结论：本模块目前是「形态参考工具」，不是「择时信号」。** 用于：
  ① 对**当前持仓/候选票**做形态健康度快照（这不需要案例期数据，是看现在）
  ② 作为 T02 回测底座的输入，将来用足够长的历史数据做样本外验证
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd

from utils.indicators import white_line, yellow_line, kdj_j
from utils.data_router import get_daily_bars


# ============================================================ 阈值常量
# 来源：公开文字版《图形买点体系 10 张经典图形》+ 架构师 ARCH v1.2 定稿
TH = {
    # ① 完美缩量
    "缩量比上限": 0.50,        # 回调量 ≤ 前放量段的 50%（缩半量）
    "J大负值": -12.0,          # J ≤ -12 视为进入大负区
    "放量倍数": 1.85,          # 突破日量能 ≥ 前日 × 1.85（同 B1 的 PLRY 口径）
    # ② N型回调
    "N型回调缩量比": 0.80,     # 回调段均量 ≤ 上涨段均量 × 0.8
    # ③ 一直拉升回调
    "一波流涨幅": 0.70,        # 连续拉升 ≥ 70%
    "顶部放量比": 1.30,        # 顶部均量 / 前段均量 < 1.3 视为「顶部不放量」
    # ④ 长期盘整
    "盘整最少日数": 20,        # 横盘 ≥ 20 交易日
    "盘整振幅上限": 0.25,      # 盘整期振幅（高-低）/低 ≤ 25%
}

# 数据需求：黄线含 MA114，至少 120 根；B1 类判据需要更长的量能窗口 260 根
MIN_BARS = 130


# ============================================================ 工具函数
def _prepare(df):
    """补齐指标列（复用 utils.indicators 的官方口径）"""
    df = df.copy()
    c, h, l, v = (df[k].astype(float) for k in ("close", "high", "low", "volume"))
    df["white"] = white_line(c, 10)                 # EMA(EMA(C,10),10)
    df["yellow"] = yellow_line(c)                   # (MA14+MA28+MA57+MA114)/4
    _, _, df["J"] = kdj_j(h, l, c)                  # KDJ(9, 3)
    df["vma5"] = v.rolling(5).mean()
    df["vma20"] = v.rolling(20).mean()
    return df


def _seg_max_vol(v, start, end):
    """区间 [start, end] 内的最大成交量（闭区间，索引为位置序号）"""
    seg = v.iloc[max(0, start):end + 1]
    return float(seg.max()) if len(seg) else 0.0


def _find_recent_high(df, lookback):
    """返回 (最高价位置, 最高价)，在最后 lookback 根内"""
    n = len(df)
    seg = df["high"].iloc[n - lookback:]
    idx = int(seg.values.argmax()) + (n - lookback)
    return idx, float(df["high"].iloc[idx])


def _find_recent_low(df, start, end):
    """返回区间内最低价位置与值"""
    seg = df["low"].iloc[start:end + 1]
    if not len(seg):
        return start, float(df["low"].iloc[start])
    idx = int(seg.values.argmin()) + start
    return idx, float(df["low"].iloc[idx])


# ============================================================ ① 完美缩量
def match_perfect_shrink(df):
    """
    ① 完美缩量图形
    判据（5 条）：
      1. 前期有放量阴/阳线由下至上穿越黄线（放量突破）
      2. 白线 > 黄线（金叉状态，多头）
      3. 回调段成交量 ≤ 前放量段最大量的 50%（缩半量）
      4. 近期 J 值 ≤ -12（进入大负区）
      5. 收盘价在白线或黄线上方（未破线）
    """
    n = len(df)
    hits, detail = [], {}

    c, v = df["close"], df["volume"]
    last = df.iloc[-1]

    # --- 1. 放量突破黄线（近 60 根内找）
    look = min(60, n - 30)
    break_idx, break_ok = None, False
    for i in range(n - look, n - 20):
        if pd.isna(df["yellow"].iloc[i]) or pd.isna(df["yellow"].iloc[i - 1]):
            continue
        crossed = (df["close"].iloc[i] > df["yellow"].iloc[i]
                   and df["close"].iloc[i - 1] <= df["yellow"].iloc[i - 1])
        vol_up = df["volume"].iloc[i] > df["volume"].iloc[i - 1] * TH["放量倍数"]
        if crossed and vol_up:
            break_idx, break_ok = i, True
    hits.append(break_ok)
    detail["放量穿黄线"] = break_ok

    # --- 2. 白线 > 黄线
    dual_ok = bool(not pd.isna(last["white"]) and not pd.isna(last["yellow"])
                   and last["white"] > last["yellow"])
    hits.append(dual_ok)
    detail["白线>黄线(多头)"] = dual_ok

    # --- 3. 缩半量（用突破段与回调段对比）
    shrink_ok, shrink_ratio = False, None
    if break_idx is not None:
        up_max = _seg_max_vol(v, max(0, break_idx - 3), break_idx)   # 放量段
        pull_max = _seg_max_vol(v, break_idx + 1, n - 1)             # 回调段
        if up_max > 0:
            shrink_ratio = pull_max / up_max
            shrink_ok = shrink_ratio <= TH["缩量比上限"]
    detail["缩半量"] = shrink_ok
    hits.append(shrink_ok)

    # --- 4. J ≤ -12（近 15 根内出现过）
    recent_j = df["J"].iloc[max(0, n - 15):]
    j_ok = bool((recent_j <= TH["J大负值"]).any())
    detail[f"J≤{TH['J大负值']:.0f}"] = j_ok
    hits.append(j_ok)

    # --- 5. 价格在线之上
    above_ok = bool((not pd.isna(last["white"]) and last["close"] >= last["white"])
                    or (not pd.isna(last["yellow"]) and last["close"] >= last["yellow"]))
    detail["收盘不破线"] = above_ok
    hits.append(above_ok)

    return {
        "图形": "完美缩量图形",
        "命中": sum(hits),
        "总数": len(hits),
        "明细": detail,
        "缩量比": round(shrink_ratio, 3) if shrink_ratio is not None else None,
    }


# ============================================================ ② N型结构回调
def match_n_structure(df):
    """
    ② N型结构回调买点
    判据（4 条）：
      1. 白线 > 黄线（多头格局）
      2. 存在 N 型结构：股价创近期新高后回调（近 40 根内高点 > 30 根前高点）
      3. 回调低点 > 前一波起涨低点（回踩不创新低）
      4. 回调不破白线（回调低点 ≥ 白线 × 0.98 宽容）
      5. 回调缩量（近 5 日均量 < 上涨段均量 × 0.8）
    """
    n = len(df)
    hits, detail = [], {}
    if n < 60:
        return {"图形": "N型结构回调买点", "命中": 0, "总数": 5,
                "明细": {"数据不足60根": False}, "缩量比": None}

    c = df["close"]
    last = df.iloc[-1]

    # --- 1. 白线 > 黄线
    dual_ok = bool(not pd.isna(last["white"]) and not pd.isna(last["yellow"])
                   and last["white"] > last["yellow"])
    detail["白线>黄线(多头)"] = dual_ok
    hits.append(dual_ok)

    # --- 2. 有N型：近 40 根内高点 > 更早 30 根的高点
    hi_recent_idx, hi_recent = _find_recent_high(df, 40)
    hi_prev = float(df["high"].iloc[max(0, n - 70):max(1, n - 40)].max()) if n > 70 else 0.0
    n_ok = hi_recent > hi_prev if hi_prev > 0 else False
    detail["存在N型上涨"] = n_ok
    hits.append(n_ok)

    # --- 3. 回调低点 > 前一波起涨低点
    low_idx, low_val = _find_recent_low(df, max(0, n - 25), n - 1)
    rise_start_idx, rise_start = _find_recent_low(df, max(0, n - 70), max(1, n - 30))
    higher_low = bool(low_val > rise_start) if rise_start > 0 else False
    detail["回调不创新低"] = higher_low
    hits.append(higher_low)

    # --- 4. 回调不破白线（宽容 2%）
    white_now = float(last["white"]) if not pd.isna(last["white"]) else 0.0
    hold_white = bool(white_now > 0 and low_val >= white_now * 0.98)
    detail["回调不破白线"] = hold_white
    hits.append(hold_white)

    # --- 5. 回调缩量
    pull_v = float(df["volume"].iloc[max(0, n - 5):].mean())
    rise_v = float(df["volume"].iloc[max(0, n - 40):max(1, n - 20)].mean())
    shrink_ratio = (pull_v / rise_v) if rise_v > 0 else None
    shrink_ok = bool(shrink_ratio is not None and shrink_ratio < TH["N型回调缩量比"])
    detail["回调缩量"] = shrink_ok
    hits.append(shrink_ok)

    return {
        "图形": "N型结构回调买点",
        "命中": sum(hits),
        "总数": len(hits),
        "明细": detail,
        "缩量比": round(shrink_ratio, 3) if shrink_ratio is not None else None,
    }


# ============================================================ ③ 一直拉升回调
def match_pullback_after_rally(df):
    """
    ③ 一直拉升的回调买点
    判据（4 条）：
      1. 前一波连续拉升幅度 ≥ 70%
      2. 顶部区域未放巨量（顶部均量 / 前段均量 < 1.3 → 未出货）
      3. 价格已回调至白线附近（距白线 ≤ 5%）
      4. 白线 > 黄线（多头格局）
    """
    n = len(df)
    hits, detail = [], {}
    if n < 130:
        return {"图形": "一直拉升的回调买点", "命中": 0, "总数": 4,
                "明细": {"数据不足130根": False}, "涨幅": None}

    last = df.iloc[-1]

    # --- 1. 一波流涨幅 ≥ 70%（近 120 根内最低 → 最高）
    lo_idx, lo_val = _find_recent_low(df, max(0, n - 120), n - 1)
    # 高点必须在低点之后
    seg_hi = df["high"].iloc[lo_idx:]
    if len(seg_hi) == 0:
        seg_hi = df["high"]
    hi_val = float(seg_hi.max())
    gain = (hi_val / lo_val - 1) if lo_val > 0 else 0.0
    rally_ok = gain >= TH["一波流涨幅"]
    detail[f"拉升≥{TH['一波流涨幅']*100:.0f}%"] = rally_ok
    hits.append(rally_ok)

    # --- 2. 顶部不放量
    hi_all_idx, _ = _find_recent_high(df, min(120, n - 1))
    top_v = float(df["volume"].iloc[max(0, hi_all_idx - 3):hi_all_idx + 1].mean())
    prior_v = float(df["volume"].iloc[max(0, hi_all_idx - 30):max(1, hi_all_idx - 3)].mean())
    top_ratio = (top_v / prior_v) if prior_v > 0 else None
    top_ok = bool(top_ratio is not None and top_ratio < TH["顶部放量比"])
    detail["顶部不放量"] = top_ok
    hits.append(top_ok)

    # --- 3. 回调至白线附近（≤5%）
    white_now = float(last["white"]) if not pd.isna(last["white"]) else 0.0
    near_white = bool(white_now > 0 and abs(last["close"] / white_now - 1) <= 0.05)
    detail["回调至白线附近"] = near_white
    hits.append(near_white)

    # --- 4. 白线 > 黄线
    dual_ok = bool(not pd.isna(last["white"]) and not pd.isna(last["yellow"])
                   and last["white"] > last["yellow"])
    detail["白线>黄线(多头)"] = dual_ok
    hits.append(dual_ok)

    return {
        "图形": "一直拉升的回调买点",
        "命中": sum(hits),
        "总数": len(hits),
        "明细": detail,
        "涨幅": round(gain * 100, 1),
    }


# ============================================================ ④ 长期盘整洗盘
def match_long_consolidation(df):
    """
    ④ 长期盘整消化洗盘
    判据（4 条）：
      1. 历史有较大涨幅（近 120 根内涨幅 ≥ 50%）
      2. 曾出现顶部大风车量（某日量 ≥ 前 20 日均量 × 2）
      3. 近期进入横盘：连续 ≥20 日为盘整且振幅收窄（≤25%）
      4. 盘整期间未有效跌破黄线（收盘 ≥ 黄线 × 0.97）
    """
    n = len(df)
    hits, detail = [], {}
    if n < 150:
        return {"图形": "长期盘整消化洗盘", "命中": 0, "总数": 4,
                "明细": {"数据不足150根": False}, "盘整日数": 0}

    # --- 1. 历史涨幅 ≥ 50%
    lo_idx, lo_val = _find_recent_low(df, max(0, n - 120), n - 1)
    hi_after = float(df["high"].iloc[lo_idx:].max()) if len(df["high"].iloc[lo_idx:]) else lo_val
    hist_gain = (hi_after / lo_val - 1) if lo_val > 0 else 0.0
    gain_ok = hist_gain >= 0.50
    detail["历史涨幅≥50%"] = gain_ok
    hits.append(gain_ok)

    # --- 2. 顶部大风车量（近 120 根内某日量 ≥ 前20日均量×2）
    seg = df.iloc[max(0, n - 120):]
    windmill = False
    for i in range(len(seg)):
        gi = seg.index[i]
        pos = df.index.get_loc(gi)
        if pos >= 20:
            prior = float(df["volume"].iloc[pos - 20:pos].mean())
            if prior > 0 and float(df["volume"].iloc[pos]) >= prior * 2:
                windmill = True
                break
    detail["曾有顶部大风车量"] = windmill
    hits.append(windmill)

    # --- 3. 长期横盘 ≥20 日且振幅收窄
    consol_days = 0
    for k in range(20, min(90, n)):
        win = df.iloc[n - k:]
        hh, ll = float(win["high"].max()), float(win["low"].min())
        if ll > 0 and (hh - ll) / ll <= TH["盘整振幅上限"]:
            consol_days = k
        else:
            break
    consol_ok = consol_days >= TH["盘整最少日数"]
    detail[f"横盘≥{TH['盘整最少日数']}日"] = consol_ok
    hits.append(consol_ok)

    # --- 4. 未有效跌破黄线
    if consol_days > 0:
        win = df.iloc[n - consol_days:]
        y = win["yellow"]
        valid = y.notna()
        hold_yellow = bool(valid.any()
                           and (win["close"][valid] >= y[valid] * 0.97).all())
    else:
        hold_yellow = False
    detail["盘整未破黄线"] = hold_yellow
    hits.append(hold_yellow)

    return {
        "图形": "长期盘整消化洗盘",
        "命中": sum(hits),
        "总数": len(hits),
        "明细": detail,
        "盘整日数": consol_days,
    }


# ============================================================ 汇总入口
ALL_MATCHERS = [
    match_perfect_shrink,
    match_n_structure,
    match_pullback_after_rally,
    match_long_consolidation,
]


def match_one(symbol, count=260, bar_df=None):
    """
    对单只票跑 4 类图形判据
    返回: {"symbol":..., "name":..., "ok":bool, "结果":[...], "最高命中":{...}}
    """
    if bar_df is None:
        resp = get_daily_bars(str(symbol).zfill(6), count=count, adjustment="qfq")
        # 单只返回 {"status":..., "data":[...]}
        if not isinstance(resp, dict) or "data" not in resp:
            return {"symbol": symbol, "ok": False, "msg": f"取数失败: {resp.get('status') if isinstance(resp, dict) else resp}"}
        bars = resp["data"]
    else:
        bars = bar_df

    if not bars or len(bars) < MIN_BARS:
        return {"symbol": symbol, "ok": False,
                "msg": f"数据不足（需≥{MIN_BARS}根，实际{len(bars) if bars else 0}）"}

    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df = _prepare(df)

    results = [m(df) for m in ALL_MATCHERS]
    # 归一化命中率，选最高的
    for r in results:
        r["命中率"] = round(r["命中"] / r["总数"], 3) if r["总数"] else 0.0
    best = max(results, key=lambda x: (x["命中率"], x["命中"]))

    return {
        "symbol": str(symbol).zfill(6),
        "ok": True,
        "数据范围": f"{df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}（{len(df)}根）",
        "结果": results,
        "最高命中": best,
    }


def match_all(symbols, count=260):
    """
    批量跑（symbols 可为 str 或 list）
    返回 list[dict]
    """
    if isinstance(symbols, str):
        return [match_one(symbols, count=count)]
    return [match_one(s, count=count) for s in symbols]


def match_to_dataframe(symbols, count=260, only_hit=False):
    """批量结果转 DataFrame，便于打印/落盘"""
    rows = []
    for r in match_all(symbols, count=count):
        if not r.get("ok"):
            rows.append({"代码": r["symbol"], "状态": r.get("msg", "失败")})
            continue
        for item in r["结果"]:
            if only_hit and item["命中"] == 0:
                continue
            row = {
                "代码": r["symbol"],
                "图形": item["图形"],
                "命中": item["命中"],
                "总数": item["总数"],
                "命中率": f"{item['命中率']*100:.0f}%",
            }
            for k, v in item["明细"].items():
                row[k] = "✓" if v else "✗"
            rows.append(row)
    return pd.DataFrame(rows)


# ============================================================ CLI
def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print("\n用法: python utils/patterns_ten.py <代码1> [代码2 ...]")
        print("示例: python utils/patterns_ten.py 688799 600601 002940")
        return

    print(f"\n{'='*78}")
    print("十大完美图形 · 形态匹配（⚠️ 形态参考，非买入信号）")
    print(f"{'='*78}\n")

    for code in args:
        r = match_one(code)
        if not r.get("ok"):
            print(f"[{code}] ✗ {r.get('msg')}\n")
            continue
        print(f"【{r['symbol']}】{r['数据范围']}")
        print(f"{'-'*78}")
        for item in r["结果"]:
            mark = "★" if item is r["最高命中"] else " "
            print(f" {mark} {item['图形']:<20} {item['命中']}/{item['总数']}  ({item['命中率']*100:.0f}%)")
            for k, v in item["明细"].items():
                print(f"       {'✓' if v else '✗'} {k}")
            extra = {k: v for k, v in item.items()
                     if k in ("缩量比", "涨幅", "盘整日数")}
            if extra:
                print(f"       · {extra}")
        best = r["最高命中"]
        print(f"  → 最匹配：{best['图形']}（{best['命中']}/{best['总数']}）")
        print()


if __name__ == "__main__":
    main()
