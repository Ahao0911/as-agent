# -*- coding: utf-8 -*-
"""
量价指标库（F1 · 图形买点体系词汇表代码化）
==================================
全部为**逐日 Series**（bool 或 float），升序、DatetimeIndex，函数签名统一
``(df: pd.DataFrame, ...) -> pd.Series / pd.DataFrame``。

设计约束（docs/flow_redesign_2026-09-11.md B1）：
  - 每个指标的知识库依据写在 docstring，**不编造没有出处的东西**；
  - 「红肥绿瘦」严格用 b1_rules.md:84 原文口径：近 14 日阳量/阴量 > 1.65；
  - 「四分之三阴量线」「顶部大风车」为用户口述口径（2026-09-11 确认），是**负面逃顶信号**，
    不进 resonance 正向计分（避免污染 E3"依据越多越好"实验），只用于：
      ① 持仓离场层（flow_backtest，--exit-enhanced 开关，默认关）
      ② 选股过滤（候选股命中 → 剔除/降级）

前视防护：所有指标只用 t 及以前数据（rolling/shift 均为右侧对齐），
唯一例外 top_windmill 的「确认」列在**确认日**给出（语义见其 docstring）。
"""
import numpy as np
import pandas as pd

# 知识库口径常量（勿散落硬编码）
RED_FAT_GREEN_THIN_RATIO = 1.65   # b1_rules.md:84「14天阳量/阴量>1.65」
PULLBACK_HEAVY_MULT = 2.5         # b1_rules.md:90「放巨量后缩量回调」巨量倍数
PULLBACK_SHRINK_MULT = 0.8        # 同上：回调缩量上限倍数
VOL_MA_N = 20                     # 量能基准均线（全库统一 20 日）


def _v(df):
    """volume 转 float Series。"""
    return df["volume"].astype(float)


def _vol_ma(df, n=VOL_MA_N):
    """量能基准均线。"""
    return _v(df).rolling(n, min_periods=n).mean()


def _body(df):
    """K 线实体绝对值。"""
    return (df["close"].astype(float) - df["open"].astype(float)).abs()


def _is_yang(df):
    """阳线。"""
    return df["close"].astype(float) > df["open"].astype(float)


def _is_yin(df):
    """阴线。"""
    return df["close"].astype(float) < df["open"].astype(float)


# ============================================================
# 正向量价指标（可进 resonance / 选股）
# ============================================================

def heavy_volume_bar(df, mult=1.8):
    """倍量柱：单/双根量显著放大 + 暴力K。

    口径: V > MA(V,20)*mult 或 V > V_prev*2。
    知识库依据: 「倍量柱：单/双根量显著放大+暴力K」；
    mult 进网格 [1.5, 1.8, 2.0, 2.5]（B2 现行 1.5 偏宽松，倍量柱 ≈2）。
    """
    v = _v(df)
    ma = _vol_ma(df)
    cond_a = v > ma * mult
    cond_b = v > v.shift(1) * 2
    return (cond_a | cond_b).fillna(False).astype(bool)


def long_yin_short_zhu(df):
    """长阴短柱：长阴但量萎缩（恐慌盘被打掉、主力未出）。

    口径: 跌幅 > 3% 且 V < MA(V,20)*0.7。
    知识库依据: 「长阴短柱：长阴但量萎缩（恐慌盘被打掉、主力未出）」。
    """
    close = df["close"].astype(float)
    prev = close.shift(1)
    drop = (prev - close) / prev.replace(0, np.nan)
    cond = (drop > 0.03) & (_v(df) < _vol_ma(df) * 0.7)
    return cond.fillna(False).astype(bool)


def shrink_to_floor(df):
    """缩量到地量：连续缩量下跌 → 地量。

    口径: V < MA(V,20)*0.5 或 V ≤ 近60日10%分位。
    知识库依据: 「连续缩量下跌→地量」。
    """
    v = _v(df)
    q10 = v.rolling(60, min_periods=60).quantile(0.10)
    cond = (v < _vol_ma(df) * 0.5) | (v <= q10)
    return cond.fillna(False).astype(bool)


def red_fat_green_thin_ratio(df, n=14):
    """红肥绿瘦比值（连续值，供人工核对/网格）：近 n 日阳量合计 / 阴量合计。

    知识库依据: b1_rules.md:84 原文「14天阳量/阴量>1.65」。
    阴量为 0 时比值取 +inf（无阴量=极健康）。
    """
    yang = _v(df).where(_is_yang(df), 0.0)
    yin = _v(df).where(_is_yin(df), 0.0)
    yang_sum = yang.rolling(n, min_periods=n).sum()
    yin_sum = yin.rolling(n, min_periods=n).sum()
    ratio = yang_sum / yin_sum.replace(0, np.nan)
    ratio = ratio.where(yin_sum > 0, np.inf)          # 无阴量 → 极健康
    ratio = ratio.where(yang_sum > 0, 0.0)            # 无阳量 → 0
    return ratio


def red_fat_green_thin(df, n=14, threshold=RED_FAT_GREEN_THIN_RATIO):
    """红肥绿瘦（持仓健康度）：近 n 日阳量/阴量 > 1.65。

    知识库依据: b1_rules.md:84 原文口径「14天阳量/阴量>1.65」。
    """
    return (red_fat_green_thin_ratio(df, n=n) > threshold).fillna(False).astype(bool)


def reversal_engulf(df):
    """反包：阳线实体完全覆盖前一日阴线实体（灾后重建）。

    知识库依据: 「反包：阳线完全覆盖前阴实体（灾后重建）」。
    """
    o = df["open"].astype(float)
    c = df["close"].astype(float)
    prev_o = o.shift(1)
    prev_c = c.shift(1)
    prev_yin = prev_c < prev_o
    cond = (
        _is_yang(df)
        & prev_yin
        & (o <= prev_c)      # 今开 ≤ 昨收（阴线实体下沿）
        & (c >= prev_o)      # 今收 ≥ 昨开（阴线实体上沿）
    )
    return cond.fillna(False).astype(bool)


def key_k(df, body_pct=0.04, limit_pct=0.095, hh_n=20):
    """关键K：关键位（突破前高 / 首次站上黄线）+ 大阳或涨停。

    知识库依据: 「关键K：关键位大阳/涨停=大资金入场」；止损 = 跌破该K低点。
    黄线口径: needle20_lines 的「中期」线（10K）。此处为避免循环依赖内嵌同口径计算。

    Returns:
        DataFrame['关键K'(bool), '关键K低点'(float)] —— 低点供离场层第1级用。
    """
    o = df["open"].astype(float)
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    prev_c = c.shift(1)

    big_yang = ((c - o) / o.replace(0, np.nan) > body_pct) & (c > o)
    limit_up = (c / prev_c.replace(0, np.nan) - 1) >= limit_pct

    # 关键位①: 突破前高（前 hh_n 日最高价，不含当日）
    prev_high = h.rolling(hh_n, min_periods=hh_n).max().shift(1)
    break_high = c > prev_high

    # 关键位②: 首次站上黄线（中期 10K，与 needle20_lines 同口径）
    low_ = df["low"].astype(float)
    hh10 = c.rolling(10, min_periods=1).max()
    ll10 = low_.rolling(10, min_periods=1).min()
    rng = (hh10 - ll10).replace(0, 1e-9)
    yellow = ((c - ll10) / rng * 100).round(2)          # 中期线
    above_yellow = c > yellow
    first_above = above_yellow & (~above_yellow.shift(1).fillna(False).astype(bool))

    hit = (break_high | first_above) & (big_yang | limit_up)
    out = pd.DataFrame({
        "关键K": hit.fillna(False).astype(bool),
        "关键K低点": df["low"].astype(float),
    }, index=df.index)
    out.loc[~out["关键K"], "关键K低点"] = np.nan
    return out


def volume_shrink_pullback(df):
    """放量缩量回调（健康）：放巨量后缩量回调 → 可持有。

    口径: 近 10 日有 V > MA(V,20)*2.5 巨量，其后回调日 V < MA(V,20)*0.8 且收阴/收跌。
    知识库依据: b1_rules.md:90「放巨量后缩量回调=健康可持有」。
    """
    v = _v(df)
    ma = _vol_ma(df)
    heavy_day = (v > ma * PULLBACK_HEAVY_MULT).fillna(False).astype(bool)
    heavy_recent = heavy_day.rolling(10, min_periods=1).max().shift(1).fillna(0).astype(bool)
    c = df["close"].astype(float)
    pullback = _is_yin(df) | (c < c.shift(1))
    cond = heavy_recent & (v < ma * PULLBACK_SHRINK_MULT) & pullback
    return cond.fillna(False).astype(bool)


def shrink_yin_up(df):
    """缩量阴线价升：收盘价上升 & 阴线 & 缩量 = 强势。

    知识库依据: 「缩量阴线+收盘价上升=强势」。
    """
    c = df["close"].astype(float)
    cond = (
        (c > c.shift(1))
        & _is_yin(df)
        & (_v(df) < _vol_ma(df) * 0.85)
        & (_v(df) < _v(df).shift(1))
    )
    return cond.fillna(False).astype(bool)


def top_heavy_volume(df, heavy_mult=2.0, cnt=2, win=10, high_zone=0.97):
    """平量/次高点密集放量（反向指标，供离场层）= 出货嫌疑。

    口径（设计文档 B1 指定，为 volume_shrink_pullback 的反面）:
    近 win 日出现 ≥cnt 次 V>MA(V,20)*heavy_mult，且股价处于近20日高位区
    （close ≥ 20日最高*high_zone）且当日未大涨（滞涨）→ 密集放量滞涨 = 出货。
    """
    v = _v(df)
    ma = _vol_ma(df)
    heavy = (v > ma * heavy_mult).fillna(False).astype(bool)
    dense = heavy.rolling(win, min_periods=1).sum() >= cnt
    c = df["close"].astype(float)
    near_high = c >= c.rolling(20, min_periods=20).max().shift(1) * high_zone
    stalled = (c / c.shift(1).replace(0, np.nan) - 1) < 0.03
    return (dense & near_high & stalled).fillna(False).astype(bool)


# ============================================================
# 负面逃顶指标（不进 resonance 正向计分；进离场层 --exit-enhanced）
# ============================================================

def three_quarter_yin_volume(df, vol_mult=1.5, ratio_lo=0.65, ratio_hi=0.85,
                             ma_n=5):
    """四分之三阴量线（S1 级逃顶预警，负面信号）。

    用户口述（2026-09-11 确认）：「量的图不是k线，放大量红柱第二天是3/4的阴量柱，
    可能是逃顶信号或假突破」。
    知识库佐证: 「标准四分之三阴量线**宣告走坏**」（2026-03-15 原文）；
    与「放巨量后**缩量**回调=健康」互补 —— 缩量阴(≈1/4)健康，3/4 阴量=走坏。

    判定:
      D1: 放量阳线 —— 当日收阳 且 V > MA(V,ma_n).shift(1) * vol_mult
      D2: 次日阴线 且 ratio_lo ≤ V(D2)/V(D1) ≤ ratio_hi（≈3/4）
    命中日在 D2 返回 True。

    Args:
        vol_mult: D1 放量倍数（进网格）。
        ratio_lo/ratio_hi: 阴量/阳量比例带宽（默认 0.65~0.85 ≈ 3/4）。
        ma_n: D1 量能基准均线周期。
    """
    v = _v(df)
    o = df["open"].astype(float)
    c = df["close"].astype(float)
    ma = v.rolling(ma_n, min_periods=ma_n).mean()

    d1 = (c > o) & (v > ma.shift(1) * vol_mult)
    ratio = v / v.shift(1).replace(0, np.nan)
    d2 = _is_yin(df) & d1.shift(1).fillna(False).astype(bool) \
        & ratio.between(ratio_lo, ratio_hi)
    return d2.fillna(False).astype(bool)


def top_windmill(df, lookback=10, gain_window=5, gain_min=0.15,
                 shadow_body_ratio=2.0, confirm_win=3, confirm_vol_mult=2.0):
    """顶部大风车（连续上涨后的螺旋桨顶部，S1 级预警/确认，负面信号）。

    知识库依据:
      黑话全解密「顶部大风车: 连续上涨后」；
      直播原文「S1形态(顶部大风车形态)」「大风车 + 放巨量阴线 = S1确认」。

    判定:
      前提: 近 lookback 日存在连续上涨 —— 近 gain_window 日累计涨幅 > gain_min；
      形态: 当日为螺旋桨 —— (上影+下影)/实体 ≥ shadow_body_ratio 且上影>0 且下影>0；
      确认: 大风车后 confirm_win 日内出现放巨量阴线（V > MA(V,5)*confirm_vol_mult 且收阴）。

    Returns:
        DataFrame 两列，均 bool：
          '大风车'(预警): 螺旋桨当日 → 减仓预警；
          '大风车确认': 放巨量阴线当日给出 → 清仓离场。
        ★ 确认列在**确认日**（放巨量阴线那天）置 True，判定只用当日及以前数据，
          因此前视安全，可直接作当日离场条件（区别于"回看过去3天"的写法）。
    """
    o = df["open"].astype(float)
    h = df["high"].astype(float)
    c = df["close"].astype(float)
    l = df["low"].astype(float)

    # 前提: 近期连续上涨
    gain = c / c.shift(gain_window).replace(0, np.nan) - 1
    rising = (gain > gain_min).rolling(lookback, min_periods=1).max().fillna(0).astype(bool)

    # 形态: 螺旋桨（长上下影 + 小实体）
    upper = h - pd.concat([o, c], axis=1).max(axis=1)
    lower = pd.concat([o, c], axis=1).min(axis=1) - l
    body = _body(df).clip(lower=1e-9)
    windmill_shape = (
        ((upper + lower) / body >= shadow_body_ratio)
        & (upper > 0) & (lower > 0)
    )
    windmill = (rising & windmill_shape).fillna(False).astype(bool)

    # 确认: 当日放巨量阴线，且此前 confirm_win 日内出现过大风车
    v = _v(df)
    vma5 = v.rolling(5, min_periods=5).mean()
    heavy_yin = _is_yin(df) & (v > vma5 * confirm_vol_mult)
    had_windmill = windmill.rolling(confirm_win, min_periods=1).max().shift(1) \
        .fillna(0).astype(bool)
    confirmed = (heavy_yin & had_windmill).fillna(False).astype(bool)

    return pd.DataFrame({"大风车": windmill, "大风车确认": confirmed}, index=df.index)


if __name__ == "__main__":
    # F1 验收自测: 用 600519 缓存数据人工核对样本日
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import socket
    socket.setdefaulttimeout(20)
    from utils.backtest_data import get_hist

    df = get_hist("600519", start="2024-01-01")
    print(f"600519: {len(df)} 根  {df['date'].min().date()} ~ {df['date'].max().date()}")
    d = df.set_index("date")

    out = pd.DataFrame(index=d.index)
    out["倍量柱"] = heavy_volume_bar(d)
    out["长阴短柱"] = long_yin_short_zhu(d)
    out["地量"] = shrink_to_floor(d)
    out["红肥绿瘦比"] = red_fat_green_thin_ratio(d).round(2)
    out["红肥绿瘦"] = red_fat_green_thin(d)
    out["反包"] = reversal_engulf(d)
    kk = key_k(d)
    out["关键K"] = kk["关键K"]
    out["放量缩量回调"] = volume_shrink_pullback(d)
    out["缩量阴线价升"] = shrink_yin_up(d)
    out["密集放量(出货)"] = top_heavy_volume(d)
    out["四分之三阴量线"] = three_quarter_yin_volume(d)
    tw = top_windmill(d)
    out["大风车"] = tw["大风车"]
    out["大风车确认"] = tw["大风车确认"]

    print("\n各指标命中数（全区间 2024-01~今）:")
    for col in out.columns:
        n = int(out[col].sum()) if out[col].dtype == bool else int((out[col] > 1.65).sum())
        print(f"  {col:<14} {n}")

    print("\n红肥绿瘦复算一例（b1_rules.md:84 口径 14天阳量/阴量>1.65）:")
    r = red_fat_green_thin_ratio(d)
    last = r.dropna().index[-1]
    win = d.loc[:last].tail(14)
    yang_sum = float(win["volume"].where(win["close"] > win["open"], 0).sum())
    yin_sum = float(win["volume"].where(win["close"] < win["open"], 0).sum())
    print(f"  {last.date()}: 阳量合计 {yang_sum:,.0f} / 阴量合计 {yin_sum:,.0f} "
          f"= {yang_sum/yin_sum if yin_sum else float('inf'):.3f} (函数值 {r.loc[last]:.3f})")

    print("\n样本日明细（各指标最近一次命中）:")
    for col in out.columns:
        if out[col].dtype == bool:
            hits = out.index[out[col]]
        else:
            hits = out.index[out[col] > 1.65]
        if len(hits):
            dt = hits[-1]
            row = d.loc[dt]
            print(f"  {col:<14} {dt.date()}  O={row['open']:.2f} H={row['high']:.2f} "
                  f"L={row['low']:.2f} C={row['close']:.2f} V={row['volume']:,.0f}")
