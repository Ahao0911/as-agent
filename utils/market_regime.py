# -*- coding: utf-8 -*-
"""市场状态分层（T02-2）
====================
把大盘日线切成 **BULL / SIDEWAYS / BEAR** 三态逐日标签，供回测按市况分层统计与
样本外切分（`BULL+SIDEWAYS → IS`，`BEAR → OOS`）。

【判定依据（口径与理由）】
以 **沪深300 指数**（`sh000300`，本项目回测基准，长历史充足）为唯一状态源：

    MA_fast = MA(close, 20)     # 月线级别短均线
    MA_slow = MA(close, 60)     # 季线级别长均线

    close > MA_slow 且 MA_fast > MA_slow   → BULL      （价在长均线上方 + 短均线上穿长均线，趋势成立）
    close < MA_slow 且 MA_fast < MA_slow   → BEAR      （价在长均线下方 + 短均线下穿长均线，趋势向下）
    其余                                    → SIDEWAYS  （均线纠缠 / 价格反复穿越，方向不明）

**为什么用「均线位置 + 均线方向」双条件而不是 ADX**：
    1. 与 图形买点体系同源 —— 本项目自身的多空判定（白线 vs 黄线）就是「均线关系」，用同构口径
       可避免「指数用一套、个股用另一套」的口径分裂；
    2. 双条件天然抑制震荡市假信号：只用 `close vs MA_slow` 会把每次反抽都判成 BULL，
       叠上 `MA_fast vs MA_slow` 方向后，均线纠缠期自动落入 SIDEWAYS；
    3. ADX 需引入 DM/TR/DI 中间量，可解释性差且需再定阈值，收益不抵复杂度（本项目零新依赖原则）。

【前视偏差防护（硬要求）】
    `classify_series()` 只用 t 及以前的数据（`rolling` 天然右侧对齐）。
    标签是「t 日收盘后可知」的状态；`get_regime(..., lag=1)` 取 t-1 日状态供 t 日决策，
    彻底杜绝「用当天收盘状态判断当天交易」。

用法:
    python utils/market_regime.py                       # 打印各状态占比 + 分段
    python utils/market_regime.py --start 2018-01-01
"""
import os
import sys
import argparse
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

# ── 状态常量（其它模块 import 这两个名字，勿改字符串值）──────────
BULL = "BULL"
SIDEWAYS = "SIDEWAYS"
BEAR = "BEAR"
REGIMES = (BULL, SIDEWAYS, BEAR)

# 沪深300：本项目回测基准，akshare 实测 5990 根（2002 起），长历史充足
INDEX_CODE = "sh000300"
INDEX_LABEL = "沪深300"

# 判定参数默认值（20/60 ≈ 月线/季线，A股中周期趋势的常识口径）
MA_FAST = 20
MA_SLOW = 60

# 数据窗口下限：不足此根数无法计算 MA60 并得到有意义的分布
MIN_BARS = 120


@dataclass
class RegimeSegment:
    """连续同状态的区间段。"""
    start: str
    end: str
    regime: str
    bars: int
    ret_pct: float = 0.0   # 该段指数涨跌幅%（便于人工核对：BULL 段应为正、BEAR 段应为负）


# ============================================================
# 数据获取
# ============================================================
def load_index_history(start: str = "2018-01-01", end: str = None) -> pd.DataFrame:
    """沪深300 日线（升序，DatetimeIndex）。

    主源: akshare `stock_zh_index_daily`（实测 5990 根，2002 起）。
    兜底: akshare 东财 `index_zh_a_hist`。

    Args:
        start: 起始日期 "YYYY-MM-DD"。
        end: 结束日期，None = 至最新。

    Returns:
        DataFrame[open, high, low, close, volume]，DatetimeIndex 升序。
        两源均失败时返回空 DataFrame（调用方须自行判空，不要静默出假结论）。
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.today()

    # ── 主源: 新浪接口（全历史，无需日期参数，实测最稳）──────────
    try:
        from utils.stock_data import _clear_proxy
        _clear_proxy()   # 项目已知: 代理会随机失效导致 akshare 失败
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol=INDEX_CODE)
        if df is not None and len(df) > MIN_BARS:
            return _normalize_index_df(df, start_ts, end_ts)
    except Exception as e:
        print(f"  ⚠ 新浪指数源失败({type(e).__name__}), 尝试东财兜底...")

    # ── 兜底: 东财接口 ────────────────────────────────────────
    try:
        import akshare as ak
        df = ak.index_zh_a_hist(symbol="000300", period="daily",
                                start_date=start_ts.strftime("%Y%m%d"),
                                end_date=end_ts.strftime("%Y%m%d"))
        if df is not None and len(df) > 0:
            rename = {"日期": "date", "开盘": "open", "最高": "high",
                      "最低": "low", "收盘": "close", "成交量": "volume"}
            df = df.rename(columns=rename)
            return _normalize_index_df(df, start_ts, end_ts)
    except Exception as e:
        print(f"  ⚠ 东财指数源也失败: {e}")

    print(f"  ✗ 沪深300 指数数据获取失败，无法做市况分层")
    return pd.DataFrame()


def _normalize_index_df(df: pd.DataFrame, start_ts, end_ts) -> pd.DataFrame:
    """统一列名/索引/排序/切片，输出标准结构。"""
    df = df.copy()
    # 列名兜底: akshare 不同版本可能给 date 或 日期
    if "date" not in df.columns:
        for cand in ("日期", "trade_date", "Date"):
            if cand in df.columns:
                df = df.rename(columns={cand: "date"})
                break
    need = ["open", "high", "low", "close"]
    for col in need:
        if col not in df.columns:
            raise ValueError(f"指数数据缺少列: {col}, 实际列={list(df.columns)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    for col in need + ["volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    return df[(df.index >= start_ts) & (df.index <= end_ts)]


# ============================================================
# 状态判定
# ============================================================
def classify_series(df_index: pd.DataFrame, fast: int = MA_FAST,
                    slow: int = MA_SLOW) -> pd.Series:
    """逐日状态标签序列。

    规则（★前视安全: 只用 t 及以前数据，rolling 右侧对齐）:
        close_t > MA_slow_t 且 MA_fast_t > MA_slow_t   → BULL
        close_t < MA_slow_t 且 MA_fast_t < MA_slow_t   → BEAR
        其余                                            → SIDEWAYS

    均线未成形的头部区间（< slow 根）无法判定，统一标 SIDEWAYS（保守：不视为牛市）。

    Args:
        df_index: `load_index_history()` 的输出。
        fast: 短均线周期，默认 20。
        slow: 长均线周期，默认 60。

    Returns:
        pd.Series[dtype=object]，index 与 df_index 对齐，取值 ∈ {BULL, SIDEWAYS, BEAR}。
        输入为空时返回空 Series。
    """
    if df_index is None or len(df_index) == 0:
        return pd.Series(dtype=object)

    close = pd.to_numeric(df_index["close"], errors="coerce").astype(float)
    ma_fast = close.rolling(fast, min_periods=fast).mean()
    ma_slow = close.rolling(slow, min_periods=slow).mean()

    above_slow = close > ma_slow
    fast_above_slow = ma_fast > ma_slow
    ma_ready = ma_slow.notna() & ma_fast.notna()

    regime = pd.Series(SIDEWAYS, index=close.index, dtype=object)
    regime[ma_ready & above_slow & fast_above_slow] = BULL
    regime[ma_ready & (~above_slow) & (~fast_above_slow)] = BEAR
    return regime


def get_regime(index_df: pd.DataFrame, fast: int = MA_FAST,
               slow: int = MA_SLOW, lag: int = 0) -> pd.Series:
    """对外主入口: 返回逐日市场状态。

    Args:
        index_df: 沪深300 日线（`load_index_history()` 的输出）。
        fast: 短均线周期。
        slow: 长均线周期。
        lag: 滞后根数。`lag=1` 表示取值 t-1 日状态 —— 回测决策接信号日**前一日**状态时
             必须用 `lag=1`（前视偏差防护）。

    Returns:
        pd.Series[dtype=object]，index 与 index_df 对齐。
    """
    regime = classify_series(index_df, fast=fast, slow=slow)
    if lag and lag > 0 and len(regime) > 0:
        regime = regime.shift(lag).fillna(SIDEWAYS).astype(object)
    return regime


def regime_at(regime: pd.Series, date, lag: int = 1) -> str:
    """取指定日期的状态（默认 lag=1 防前视偏差）。

    Args:
        regime: `get_regime()` 输出的**已滞后**序列，或未滞后序列 + lag 参数。
        date: 目标日期（str / Timestamp）。
        lag: 额外滞后根数。

    Returns:
        str: BULL / SIDEWAYS / BEAR。日期超出范围或数据缺失时返回 SIDEWAYS（保守默认）。
    """
    if regime is None or len(regime) == 0:
        return SIDEWAYS
    try:
        ts = pd.Timestamp(date)
        idx = regime.index
        if ts in idx:
            pos = idx.get_loc(ts)
            if isinstance(pos, slice):
                pos = pos.start
            pos = int(pos) - int(lag)
            if pos < 0:
                return SIDEWAYS
            return str(regime.iloc[pos])
        # 非交易日: 取 <= ts 的最后一个已存在交易日
        prior = idx[idx <= ts]
        if len(prior) == 0:
            return SIDEWAYS
        pos = int(idx.get_loc(prior[-1])) - int(lag)
        if pos < 0:
            return SIDEWAYS
        return str(regime.iloc[pos])
    except Exception:
        return SIDEWAYS


def regime_stats(index_df: pd.DataFrame, fast: int = MA_FAST,
                 slow: int = MA_SLOW, ret_series: bool = False):
    """各状态天数占比统计。

    Args:
        index_df: 沪深300 日线。
        fast: 短均线周期。
        slow: 长均线周期。
        ret_series: True 时额外返回状态 Series（键 "series"）。

    Returns:
        dict: {
            "总天数": int, "BULL": int, "SIDEWAYS": int, "BEAR": int,
            "BULL%": float, "SIDEWAYS%": float, "BEAR%": float,
            "区间起": str, "区间止": str,
            "各状态涨跌幅%": {BULL: float, SIDEWAYS: float, BEAR: float},  # 该状态全段累计
            "分段": list[RegimeSegment],
            "series": pd.Series  (仅 ret_series=True)
        }
        输入为空时返回全零 dict（键完整），调用方据此判断数据失败。
    """
    empty = {"总天数": 0, "BULL": 0, "SIDEWAYS": 0, "BEAR": 0,
             "BULL%": 0.0, "SIDEWAYS%": 0.0, "BEAR%": 0.0,
             "区间起": "", "区间止": "", "各状态涨跌幅%": {}, "分段": []}
    if index_df is None or len(index_df) == 0:
        return empty

    regime = get_regime(index_df, fast=fast, slow=slow)
    total = int(len(regime))
    if total == 0:
        return empty
    counts = {r: int((regime == r).sum()) for r in REGIMES}

    # 各状态累计涨跌幅（把该状态所有交易日的日收益复合）—— 用于人工核对口径合理性
    close = pd.to_numeric(index_df["close"], errors="coerce").astype(float)
    daily = close.pct_change().fillna(0.0)
    seg_ret = {}
    for r in REGIMES:
        mask = (regime == r)
        if int(mask.sum()) > 0:
            seg_ret[r] = round(float((1 + daily[mask]).prod() - 1) * 100, 2)
        else:
            seg_ret[r] = 0.0

    result = {
        "总天数": total,
        "BULL": counts[BULL],
        "SIDEWAYS": counts[SIDEWAYS],
        "BEAR": counts[BEAR],
        "BULL%": round(counts[BULL] / total * 100, 1),
        "SIDEWAYS%": round(counts[SIDEWAYS] / total * 100, 1),
        "BEAR%": round(counts[BEAR] / total * 100, 1),
        "区间起": str(regime.index[0])[:10],
        "区间止": str(regime.index[-1])[:10],
        "各状态涨跌幅%": seg_ret,
        "分段": regime_segments(regime, index_df),
    }
    if ret_series:
        result["series"] = regime
    return result


def regime_segments(regime: pd.Series, index_df: pd.DataFrame = None) -> list:
    """把连续同状态压成区间列表，供报告分段统计。

    Args:
        regime: `get_regime()` / `classify_series()` 输出。
        index_df: 可选，传入则计算每段指数涨跌幅（便于核对 BULL 段应正 / BEAR 段应负）。

    Returns:
        list[RegimeSegment]，按时间升序。
    """
    if regime is None or len(regime) == 0:
        return []

    segs = []
    cur = str(regime.iloc[0])
    start_pos = 0
    close = None
    if index_df is not None and len(index_df) == len(regime):
        close = pd.to_numeric(index_df["close"], errors="coerce").astype(float)

    for i in range(1, len(regime)):
        v = str(regime.iloc[i])
        if v != cur:
            segs.append(_make_segment(regime, start_pos, i - 1, cur, close))
            cur = v
            start_pos = i
    segs.append(_make_segment(regime, start_pos, len(regime) - 1, cur, close))
    return segs


def _make_segment(regime: pd.Series, i0: int, i1: int, name: str,
                  close: pd.Series = None) -> RegimeSegment:
    """构造单个 RegimeSegment（内部工具）。"""
    ret = 0.0
    if close is not None and i1 > i0:
        c0 = float(close.iloc[i0])
        c1 = float(close.iloc[i1])
        if c0 > 0:
            ret = round((c1 / c0 - 1) * 100, 2)
    return RegimeSegment(
        start=str(regime.index[i0])[:10],
        end=str(regime.index[i1])[:10],
        regime=name,
        bars=int(i1 - i0 + 1),
        ret_pct=ret,
    )


# ============================================================
# 样本外切分辅助（供 backtest_grid 使用）
# ============================================================
def split_by_regime(index_df: pd.DataFrame = None, regime: pd.Series = None,
                    start: str = "2018-01-01",
                    is_regimes=(BULL, SIDEWAYS)) -> tuple:
    """按市况切分 IS/OOS 的**日期掩码**（架构师 P1 增强）。

    约定 `BULL+SIDEWAYS → IS`，`BEAR → OOS`：用牛市/震荡市调参，拿熊市做压力测试。
    ⚠️ 返回的是**大盘日期掩码**，标的 df 需按同一批日期切片（全局统一切点，防标的漂移）。

    Args:
        index_df: 沪深300 日线；为 None 时用 `regime` 参数。
        regime: 已算好的状态 Series（与 index_df 二选一）。
        start: 数据窗口起点，仅日志用。
        is_regimes: 归入 IS 的状态集合，默认 (BULL, SIDEWAYS)。

    Returns:
        (is_mask, oos_mask): 两个 bool Series，index 为大盘交易日，互斥且并集为全体。
        数据失败时返回 (None, None)，调用方须硬失败退出。
    """
    if regime is None:
        if index_df is None or len(index_df) == 0:
            return None, None
        regime = get_regime(index_df)
    if regime is None or len(regime) == 0:
        return None, None

    in_is = regime.isin(list(is_regimes))
    is_mask = in_is.astype(bool)
    oos_mask = (~in_is).astype(bool)
    return is_mask, oos_mask


def benchmark_return(index_df: pd.DataFrame, start: str, end: str = None) -> float:
    """区间内沪深300 涨跌幅%（基准对照，起始日跟随 IS/OOS 区间）。

    Returns:
        float 百分比；数据不足返回 0.0。
    """
    if index_df is None or len(index_df) == 0:
        return 0.0
    try:
        sub = index_df[(index_df.index >= pd.Timestamp(start))]
        if end:
            sub = sub[sub.index <= pd.Timestamp(end)]
        if len(sub) < 2:
            return 0.0
        c0 = float(sub["close"].iloc[0])
        c1 = float(sub["close"].iloc[-1])
        if c0 <= 0:
            return 0.0
        return round((c1 / c0 - 1) * 100, 1)
    except Exception:
        return 0.0


# ============================================================
# CLI 自测
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="沪深300 市场状态分层(BULL/SIDEWAYS/BEAR)")
    parser.add_argument("--start", default="2018-01-01", help="起始日期")
    parser.add_argument("--fast", type=int, default=MA_FAST, help="短均线周期(默认20)")
    parser.add_argument("--slow", type=int, default=MA_SLOW, help="长均线周期(默认60)")
    parser.add_argument("--segments", type=int, default=15, help="打印前N个分段")
    args = parser.parse_args()

    print("=" * 74)
    print(f"市场状态分层 · {INDEX_LABEL}({INDEX_CODE}) · MA{args.fast}/MA{args.slow} · 起 {args.start}")
    print("=" * 74)

    df = load_index_history(args.start)
    if len(df) == 0:
        print("✗ 指数数据获取失败，无法分层")
        return

    st = regime_stats(df, fast=args.fast, slow=args.slow)
    print(f"数据: {len(df)} 根  {st['区间起']} ~ {st['区间止']}")
    print("-" * 74)
    print(f"{'状态':<10}{'天数':>7}{'占比':>9}{'该状态累计涨跌幅':>18}")
    print("-" * 74)
    for r in REGIMES:
        print(f"{r:<10}{st[r]:>7}{st[r+'%']:>8.1f}%{st['各状态涨跌幅%'][r]:>17.1f}%")
    print("-" * 74)
    print(f"合计 {st['总天数']} 根")
    print(f"\n状态分段(前{args.segments}段):")
    for s in st["分段"][:args.segments]:
        mark = "✅" if s.regime == BULL else ("❌" if s.regime == BEAR else "➖")
        print(f"  {mark} {s.regime:<9}{s.start} ~ {s.end}  {s.bars:>4}根  指数{s.ret_pct:+.1f}%")
    print("=" * 74)
    print("口径: close>MA_slow 且 MA_fast>MA_slow → BULL; close<MA_slow 且 MA_fast<MA_slow → BEAR; 其余 SIDEWAYS")


if __name__ == "__main__":
    main()
