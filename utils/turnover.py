"""
换手率指标 — 图形买点体系辅助过滤器（最后一关）
核心原则：不记死固定百分比！优先和个股自身历史对比（相对换手），其次参考绝对值。
换手率是辅助过滤器，不能单独作为买卖信号，配合黄白线/单针下20/砖型图使用。

口诀: 上涨锁仓看缩量；良性洗盘看缩量；高位警惕持续暴量。
铁律: 缩量回调=洗盘；放量下跌=出货。

档位（中小盘 50~300亿流通市值通用；大盘权重股整体下调一档，2%+即放量）:
  <3%   低换手冷清（横盘/锁仓拉升）
  3~7%  温和健康（黄金区间，B1启动/B2突破最理想）
  7~15% 高换手分歧加大（连续多天小心）
  >15%  超高换手（低位暴力吸筹 / 高位出货风险区）
"""
import numpy as np
import pandas as pd


def turnover_rate(df, liutong_guben=None):
    """
    计算换手率序列
    df: K线DataFrame（含 volume，单位=手）
    liutong_guben: 流通股本(股)，None时尝试从finance获取失败则返回None
    换手率 = 成交量(股) / 流通股本 × 100%
    """
    vol = df["volume"].astype(float)  # 手
    if liutong_guben and liutong_guben > 0:
        return vol * 100 / liutong_guben * 100  # 手→股(×100)，/流通股本，×100%
    return None


def turnover_signal(df, liutong_guben=None, lookback=20):
    """
    换手率综合分析
    返回: dict {当前换手率, 20日均换手, 相对换手, 档位, 信号列表}
    """
    if df is None or len(df) < 25:
        return {"error": "数据不足25根K线"}

    tr = turnover_rate(df, liutong_guben)
    if tr is None:
        return {"error": "无流通股本，无法计算换手率"}

    c = df["close"].astype(float)
    o = df["open"].astype(float)
    vol = df["volume"].astype(float)

    cur = float(tr.iloc[-1])
    avg20 = float(tr.iloc[-lookback:].mean())
    rel = cur / avg20 if avg20 > 0 else 1.0  # 相对换手（相对自身20日均值）

    signals = []
    # 1. 档位判断
    if cur < 3:
        signals.append(f"低换手({cur:.1f}%)：冷清/锁仓")
    elif cur < 7:
        signals.append(f"温和换手({cur:.1f}%)：黄金区间，B1/B2最理想")
    elif cur < 15:
        signals.append(f"高换手({cur:.1f}%)：分歧加大，连续多天需小心")
    else:
        signals.append(f"超高换手({cur:.1f}%)：低位暴力吸筹/高位出货风险区")

    # 2. 相对换手（对比自身20日均值）
    if rel > 2:
        signals.append(f"★ 相对换手{rel:.1f}倍 → 放量显著")
    elif rel < 0.5:
        signals.append(f"相对换手{rel:.1f}倍 → 明显缩量（洗盘/锁仓特征）")

    # 3. 洗盘VS出货（核心铁律：缩量回调=洗盘，放量下跌=出货）
    last = len(df) - 1
    recent_5 = tr.iloc[-5:]
    rising = tr.iloc[-5:].is_monotonic_increasing
    falling = tr.iloc[-5:].is_monotonic_decreasing
    price_down_5 = float(c.iloc[-1]) < float(c.iloc[-5]) if len(c) > 5 else False

    if price_down_5 and falling:
        signals.append("✓ 回调中换手持续走低（缩量回调）→ 良性洗盘特征，B2观察机会")
    elif price_down_5 and rising:
        signals.append("✗ 回调中换手持续放大（放量下跌）→ 出货风险！规避下跌中继")
    elif not price_down_5 and rising:
        signals.append("⚠ 上涨中换手持续放大 → 关注是否滞涨，接近S1减仓区间")

    # 4. B3锁仓检测（越涨换手越低=锁仓大牛）
    if len(tr) > 30:
        tr_prev10 = float(tr.iloc[-30:-20].mean())
        if float(c.iloc[-1]) > float(c.iloc[-30]) and cur < tr_prev10 * 0.7:
            signals.append("★ 越涨换手越低（锁仓特征）→ 主力高度控盘，B3主升健康")

    return {
        "当前换手率": round(cur, 2),
        f"{lookback}日均换手": round(avg20, 2),
        "相对换手": round(rel, 2),
        "信号": signals,
    }
