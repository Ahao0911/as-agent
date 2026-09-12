"""
知行单针下20（四线指标）— 图形买点体系配套副图2（找位置）
通达信源码翻译版：

短期:100*(C-LLV(L,N1))/(HHV(C,N1)-LLV(L,N1))      白线 N1=3
中期:100*(C-LLV(L,10))/(HHV(C,10)-LLV(L,10))      黄线 周期10
中长期:100*(C-LLV(L,20))/(HHV(C,20)-LLV(L,20))    紫线 周期20
长期:100*(C-LLV(L,N2))/(HHV(C,N2)-LLV(L,N2))      红线 N2=21

本质：价格相对自身区间位置指标（0~100）
- 数值越低 = 收盘价处于对应周期区间低位（超跌）
- 数值越高 = 收盘价处于周期区间高位（超涨）

四大信号（2026-09-10 对齐知识库《知行深V.txt》原文）:
1. 四线归零买: 短<=6 & 中<=6 & 中长<=6 & 长<=6 → 极端底部（稀缺，分批布局）
2. 白线下20买: 短<=20 & 长>=CONFIG.NEEDLE_LONG_MIN → 大势多头+短期恐慌杀跌=回调买点（核心信号）
   └ 长期线（红线）门槛由 CONFIG.NEEDLE_LONG_MIN 提供。2026-09-11 用户确认起=85
     （入场门槛扫描择优，非知识库原始值）；知识库原文 长>=60 保留于
     CONFIG.NEEDLE_LONG_MIN_KB 作对照/回滚基准。
3. 白穿红线买: CROSS(短,长) & 长<20 → 底部反转（最强）
4. 白穿黄线买: CROSS(短,中) & 中<30 → 底部次级买点（较弱，穿越中期/黄线而非长期/红线）

⚠ 禁止单独使用！优先级：黄白线大趋势 > B1/B2/B3波段 > 单针下20 > 砖型图
"""
import os
import sys

import numpy as np
import pandas as pd

# 支持两种运行方式: 包内 import(utils.needle20) 与 直接脚本(python utils/needle20.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.strategy_config import CONFIG


def needle20_lines(df, n1=3, n2=21):
    """
    计算四线
    df: DataFrame，需含 high/low/close（升序）
    返回: DataFrame，列 = [短期, 中期, 中长期, 长期]
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    def pct_line(close, low, hh_n, ll_n):
        hh = close.rolling(hh_n, min_periods=1).max()   # HHV(C, N)
        ll = low.rolling(ll_n, min_periods=1).min()     # LLV(L, N)
        rng = (hh - ll).replace(0, 1e-9)
        return (close - ll) / rng * 100

    return pd.DataFrame({
        "短期": pct_line(close, low, n1, n1).round(2),      # 白线 3K
        "中期": pct_line(close, low, 10, 10).round(2),      # 黄线 10K
        "中长期": pct_line(close, low, 20, 20).round(2),    # 紫线 20K
        "长期": pct_line(close, low, n2, n2).round(2),      # 红线 21K
    }, index=df.index)


def needle20_signal(df, dual=None):
    """
    单针下20完整信号分析
    df: K线数据
    dual: 可选，analyze_dual_line()结果（黄白线环境判断）
    返回: dict
    """
    lines = needle20_lines(df)
    last = len(lines) - 1
    signals = []

    s = float(lines["短期"].iloc[last])
    m = float(lines["中期"].iloc[last])
    ml = float(lines["中长期"].iloc[last])
    l = float(lines["长期"].iloc[last])

    # 前值（用于 CROSS 判断）
    s_prev = float(lines["短期"].iloc[last - 1]) if last > 0 else s
    m_prev = float(lines["中期"].iloc[last - 1]) if last > 0 else m
    l_prev = float(lines["长期"].iloc[last - 1]) if last > 0 else l

    # 黄白线环境
    bull_env = None
    if dual and isinstance(dual, dict) and "多头区间" in dual:
        bull_env = dual["多头区间"]

    # 1. 四线归零买（极端底部）— 四线阈值均为6（对齐知识库《知行深V.txt》原文）
    if s <= 6 and m <= 6 and ml <= 6 and l <= 6:
        signals.append(f"★ 四线归零买：短{s:.1f}/中{m:.1f}/中长{ml:.1f}/长{l:.1f} 全部极低 → 恐慌杀跌尾声，大级别底部区域（稀缺，分批布局不重仓）")

    # 2. 白线下20买（核心信号）— 长期线门槛取自 CONFIG.NEEDLE_LONG_MIN
    if s <= 20 and l >= CONFIG.NEEDLE_LONG_MIN:
        signals.append(f"★ 白线下20买：白线{s:.1f}≤20 + 红线{l:.1f}≥{CONFIG.NEEDLE_LONG_MIN} → 大势多头短期恐慌砸盘=回调买点")
        if bull_env is False:
            signals.append("  ⚠ 但白线<黄线空头环境 → 只是下跌反弹，只适合超短快进快出，禁止中线布局")

    # 3. 白穿红线买（CROSS(短期,长期) 且 长期<20）
    if s_prev <= l_prev and s > l and l < 20:
        signals.append("★ 白穿红线买：白线自下上穿红线(长期)+红线<20 → 底部反转信号（最强）")

    # 4. 白穿黄线买（CROSS(短期,中期) 且 中期<30）— 对齐知识库《知行深V.txt》原文
    if s_prev <= m_prev and s > m and m < 30:
        signals.append("白穿黄线买：白线自下上穿黄线(中期)+中期<30 → 底部次级买点（弱于白穿红线）")

    # 5. 状态描述
    if s < 20:
        signals.append(f"白线{s:.1f} 在20超跌线下 → 短期超跌区，重点观察潜在买点")
    elif s > CONFIG.NEEDLE_OVERBOUGHT_FORMULA:
        signals.append(f"白线{s:.1f} 在{CONFIG.NEEDLE_OVERBOUGHT_FORMULA}超涨线上 → 短期动能透支，警惕冲高回落")

    if l >= CONFIG.NEEDLE_LONG_MIN:
        signals.append(f"红线{l:.1f} 站稳{CONFIG.NEEDLE_LONG_MIN}上方 → 中期趋势健康（买点前提成立）")
    elif l < 40:
        signals.append(f"红线{l:.1f} 低位 → 中期趋势弱，买点可靠性打折")

    # 6. 优质/弱势区分
    if s <= 20 and l >= CONFIG.NEEDLE_LONG_MIN:
        recent = lines["短期"].iloc[max(0, last - 3):last + 1]
        if recent.iloc[-1] > recent.iloc[0]:
            signals.append("白线快速收回20上方 → 优质买点形态")
        else:
            signals.append("⚠ 白线趴在20下方不起 → 下跌力量持续，回避")

    result = {
        "短期": s, "中期": m, "中长期": ml, "长期": l,
        "四线归零": bool(s <= 6 and m <= 6 and ml <= 6 and l <= 6),
        "白线下20": bool(s <= 20 and l >= CONFIG.NEEDLE_LONG_MIN),
        "白穿红线": bool(s_prev <= l_prev and s > l and l < 20),
        "白穿黄线": bool(s_prev <= m_prev and s > m and m < 30),
        "多头环境": bull_env,
        "信号": signals,
    }
    return result
