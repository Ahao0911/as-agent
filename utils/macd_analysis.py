"""
图形买点体系 MACD 战法（2026-04-22版）
核心三层次：零轴多空 → 顺周期建仓波 → 背离 & 金叉空
"""
import pandas as pd
import numpy as np


def macd_analysis(df):
    """
    图形买点体系MACD完整分析
    df: DataFrame (open/high/low/close/volume，升序)
    返回: dict
    """
    if df is None or len(df) < 35:
        return {"error": "数据不足35根K线"}

    c = df['close'].astype(float)
    h = df['high'].astype(float)
    l = df['low'].astype(float)

    # 计算MACD（默认参数12,26,9）
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26          # DIF = 白线
    dea = dif.ewm(span=9, adjust=False).mean()  # DEA = 黄线
    macd_bar = 2 * (dif - dea)   # MACD红绿柱

    signals = []
    last = len(df) - 1

    dif_now = float(dif.iloc[last])
    dif_prev = float(dif.iloc[last-1]) if last > 0 else 0
    dif_5ago = float(dif.iloc[last-5]) if last >= 5 else 0
    dea_now = float(dea.iloc[last])
    bar_now = float(macd_bar.iloc[last])
    bar_prev = float(macd_bar.iloc[last-1]) if last > 0 else 0
    c_now = float(c.iloc[last])
    c_5ago = float(c.iloc[last-5]) if last >= 5 else c_now
    c_10ago = float(c.iloc[last-10]) if last >= 10 else c_now

    # ===== 1. 零轴多空判断 =====
    if dif_now > 0:
        signals.append(f"MACD多头区间(DIF={dif_now:.2f}>0) → 涨的概率>跌的概率，波段拿住不动")
    else:
        signals.append(f"MACD空头区间(DIF={dif_now:.2f}<0) → 跌的概率>涨的概率，手紧")

    # 上穿零轴
    if dif_prev <= 0 and dif_now > 0:
        signals.append("★ DIF上穿零轴 → 进入多头区间，趋势转多")
    # 下穿零轴
    if dif_prev >= 0 and dif_now < 0:
        signals.append("★ DIF下穿零轴 → 进入空头区间，趋势转空")

    # ===== 2. 顺周期 vs 背离 =====
    # 顺周期：MACD红柱变高+股价上涨 = 建仓波（主力真买）
    if bar_now > 0 and bar_now > bar_prev and c_now > c_5ago:
        signals.append("★ 顺周期：MACD红柱升高+股价上涨 → 建仓波，主力真买")
    # 顺周期后大阴线洗盘
    if len(df) > 2:
        r1 = df.iloc[-1]
        r2 = df.iloc[-2]
        if float(r2['close']) < float(r2['open']) and float(r2['close']) < float(r2['open']) * 0.97:
            # 昨天大阴线
            signals.append("⚠ 顺周期后大阴线洗盘 → 建仓波结束，第一次大洗盘，等B1")

    # 顶背离：股价新高但DIF不新高
    if c_now > c_10ago and dif_now < dif_5ago:
        signals.append("⚠ 顶背离：股价新高但DIF不新高 → 上涨动能减弱，警惕见顶")
    # 底背离：股价新低但DIF不新低
    if c_now < c_10ago and dif_now > dif_5ago:
        signals.append("★ 底背离：股价新低但DIF不新低 → 下跌动能衰竭，关注左侧买点")

    # ===== 3. 金叉/死叉 =====
    if dif_prev <= dea.iloc[last-1] if last > 0 else False and dif_now > dea_now:
        signals.append("MACD金叉(白线上穿黄线) → 短线转多信号")
    if dif_prev >= dea.iloc[last-1] if last > 0 else False and dif_now < dea_now:
        signals.append("MACD死叉(白线下穿黄线) → 短线转空信号")

    # ===== 4. 金叉空（MACD金叉后做空的结构） =====
    # 特征：金叉后股价创新高但MACD红柱不跟，然后一把杀下来
    # 这需要结合K线形态判断，这里做一个简化检测
    if len(df) > 20:
        recent_high = h.iloc[-20:].max()
        recent_dif_high = dif.iloc[-20:].max()
        if c_now >= recent_high * 0.98 and dif_now < recent_dif_high * 0.8:
            signals.append("⚠ 金叉空预警：股价接近前高但DIF大幅落后 → 可能金叉空结构，警惕假突破")

    return {
        "DIF": round(dif_now, 2),
        "DEA": round(dea_now, 2),
        "MACD柱": round(bar_now, 2),
        "零轴": "多头" if dif_now > 0 else "空头",
        "信号": signals,
    }