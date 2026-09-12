# -*- coding: utf-8 -*-
"""
共振评分（F2→F3 · 时间窗计数）
==============================
按 docs/flow_redesign_2026-09-11.md B2 + 用户裁决（2026-09-11 v3）：
  共振池（5 项）: 砖型翻红 / MACD顺周期 / 倍量柱 / MACD底背离 / 关键K
  判定口径: **最近 N 个交易日内曾出现过该指标**（先后出现，非同一天齐发），
            共振数 = 窗口内出现过的不同指标个数（去重计数），N ∈ {3, 5}。

「缩量止跌」已剔除（与 B1 强制缩量定义重叠，保留即自证，污染单调性检验）。

权重设计: **等权计数，不加权**。
依据: b1_rules.md:72-76 原文「各信号独立触发、独立参考，叠加越多只代表胜率倾向更高，
不是必要条件」—— E3 实验本身就是验证计数单调性，加权会污染实验。

前视防护: 锚点为信号日 T（入场在 T+1 开盘）；窗口取 [T-n+1, T]，只用 T 及以前数据。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from utils.brick import brick_chart          # 只读复用冻结内核
from utils.vol_price import heavy_volume_bar, key_k

# 共振池 5 项（v3：剔除「缩量止跌」—— 与 B1 强制缩量定义重叠，保留即自证，
# 会污染「依据越多胜率越高」的单调性检验。用户裁决 2026-09-11）
RESONANCE_ITEMS = ["砖型翻红", "MACD顺周期", "倍量柱", "MACD底背离", "关键K"]


def macd_series(df):
    """MACD 三序列（与 macd_analysis.py 完全同口径: 12/26/9, adjust=False）。

    macd_analysis.py 只输出"最后一日"的文字信号，回测需要逐日序列，
    故此处按其源码公式内嵌重算（零轴 DIF / 黄线 DEA / 红绿柱 2*(DIF-DEA)）。

    Returns:
        DataFrame['dif', 'dea', 'bar']（bar = 2*(dif-dea)，>0 为红柱）
    """
    c = df["close"].astype(float)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    bar = 2 * (dif - dea)
    return pd.DataFrame({"dif": dif, "dea": dea, "bar": bar}, index=df.index)


def resonance_matrix(df):
    """逐日 × 共振项布尔矩阵（当日命中口径）。

    各项来源:
      砖型翻红   = brick_chart(df)["翻红XG"]（绿翻红首次）
      MACD顺周期 = 红柱升高 + 5日价涨（macd_analysis.py:57-58 同口径）
      倍量柱     = vol_price.heavy_volume_bar(df)
      MACD底背离 = 股价创10日新低 而 DIF 不创新低（macd_analysis.py:71-72 同口径）
      关键K      = vol_price.key_k(df)["关键K"]

    注: v3 起不再包含「缩量止跌」（与 B1 定义重叠，见 RESONANCE_ITEMS 注释）。
    时间窗口径由 presence_matrix() 在此基础上二次聚合。

    Returns:
        DataFrame[RESONANCE_ITEMS]（bool，index 与 df 对齐）
    """
    brick = brick_chart(df)
    macd = macd_series(df)
    c = df["close"].astype(float)

    out = pd.DataFrame(index=df.index)
    out["砖型翻红"] = brick["翻红XG"].fillna(False).astype(bool)
    out["MACD顺周期"] = (
        (macd["bar"] > 0)
        & (macd["bar"] > macd["bar"].shift(1))
        & (c > c.shift(5))
    ).fillna(False).astype(bool)
    out["倍量柱"] = heavy_volume_bar(df)
    out["MACD底背离"] = (
        (c < c.shift(10)) & (macd["dif"] > macd["dif"].shift(5))
    ).fillna(False).astype(bool)
    out["关键K"] = key_k(df)["关键K"]
    return out[RESONANCE_ITEMS]


def presence_matrix(matrix, n):
    """N 日时间窗「曾出现」矩阵（v3 共振口径，用户裁决）。

    语义: 第 t 行 = 指标在 **[t-n+1, t] 共 n 根 K 线内是否出现过至少一次**
    （后向窗口，含当日）。用户确认共振指「几天内先后出现」，非同日齐发。

    前视安全: 第 t 行只用 t 及以前数据；调用方以信号日 T 为锚点
    （score_at(..., lag=1)，入场在 T+1 开盘），故 T 日收盘信息可用、无前视。

    Args:
        matrix: resonance_matrix() 输出（bool）。
        n: 窗口长度（交易日根数）。n<=1 时原样返回。

    Returns:
        同形 bool DataFrame。
    """
    n = int(n)
    if n <= 1:
        return matrix
    return matrix.rolling(n, min_periods=1).max().fillna(0).astype(bool)


def score_at(matrix, date, lag=1):
    """取 date 前 lag 个交易日的命中数与命中项列表（防前视）。

    Args:
        matrix: resonance_matrix() 输出。
        date: 目标日期（str / Timestamp），非交易日取 <= date 的最后一个交易日。
        lag: 滞后根数，默认 1（即"信号日 T-1 及以前"口径）。

    Returns:
        (score: int, hits: list[str])；数据不足/越界返回 (0, [])。
    """
    if matrix is None or len(matrix) == 0:
        return 0, []
    try:
        ts = pd.Timestamp(date)
        idx = matrix.index
        if ts in idx:
            pos = idx.get_loc(ts)
            if isinstance(pos, slice):
                pos = pos.start
        else:
            prior = idx[idx <= ts]
            if len(prior) == 0:
                return 0, []
            pos = idx.get_loc(prior[-1])
        pos = int(pos) - int(lag)
        if pos < 0:
            return 0, []
        row = matrix.iloc[pos]
        hits = [str(k) for k in matrix.columns if bool(row[k])]
        return len(hits), hits
    except Exception:
        return 0, []


def bucket(score):
    """命中数 → 分层键："0" / "1" / "2" / "3+"。"""
    s = int(score)
    if s <= 0:
        return "0"
    if s == 1:
        return "1"
    if s == 2:
        return "2"
    return "3+"


if __name__ == "__main__":
    import socket
    socket.setdefaulttimeout(20)
    from utils.backtest_data import get_hist

    df = get_hist("600519", start="2024-01-01")
    d = df.set_index("date")
    m = resonance_matrix(d)
    print(f"600519 {len(d)} 根  {d.index.min().date()} ~ {d.index.max().date()}")
    print("\n共振各项命中数:")
    for col in RESONANCE_ITEMS:
        print(f"  {col:<10} {int(m[col].sum())}")
    sc = m.sum(axis=1)
    print("\n命中数分布:")
    print(sc.value_counts().sort_index().to_string())
    print("\n最后5日 score_at(date, lag=1):")
    for dt in d.index[-5:]:
        s, hits = score_at(m, dt, lag=1)
        print(f"  {dt.date()}  score={s}  bucket={bucket(s)}  hits={hits}")
