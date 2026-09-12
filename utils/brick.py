"""
知行砖型图指标 — 图形买点体系配套副图（玩超短）
通达信源码翻译版：

VAR1A=(HHV(HIGH,4)-CLOSE)/(HHV(HIGH,4)-LLV(LOW,4))*100-90
VAR2A=SMA(VAR1A,4,1)+100
VAR3A=(CLOSE-LLV(LOW,4))/(HHV(HIGH,4)-LLV(LOW,4))*100
VAR4A=SMA(VAR3A,6,1)
VAR5A=SMA(VAR4A,6,1)+100
VAR6A=VAR5A-VAR2A
砖型图=IF(VAR6A>4,VAR6A-4,0)
红柱: REF(砖型图,1)<砖型图 (多头动量增强)
绿柱: REF(砖型图,1)>砖型图 (空头动量增强)

⚠ 重要：砖型图禁止单独使用！必须搭配黄白线(知行趋势线)过滤大趋势：
   白线>黄线(多头环境) 信号才有效；白线<黄线 红砖多为反弹诱多。

核心形态（图形买点体系原版进阶）:
- 绿翻强红: 连续绿柱后第一根红砖 ≥ 前段绿柱最大高度×3/4 → 强买点
- 普通绿翻红: 红砖很短 → 弱反弹，只做极短线
- 短绿接长红: 洗盘极短抛压小，多头主控，可持仓/加仓
- 连续4红砖: 主动减仓30-50%（不是清仓！强势可走5-6砖）
- 红砖缩短: 动能衰竭，准备减仓
- 红翻绿: 短线止盈离场参考
- 锯齿砖: 红绿频繁切换=震荡行情，减少交易

注意: 通达信 SMA(X,N,M) 是加权递推 Y=(M*X+(N-M)*Y')/N，不是简单均线！
     等价于 pandas ewm(alpha=M/N, adjust=False)
"""
import os
import sys

import numpy as np
import pandas as pd

# 支持两种运行方式: 包内 import(utils.brick) 与 直接脚本(python utils/brick.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.strategy_config import CONFIG

# 消除 fillna downcasting FutureWarning
pd.set_option("future.no_silent_downcasting", True)


def tdx_sma(series, n, m):
    """通达信 SMA(X,N,M) = (M*X + (N-M)*Y')/N → ewm(alpha=M/N, adjust=False)"""
    return series.ewm(alpha=m / n, adjust=False).mean()


def brick_chart(df):
    """
    计算知行砖型图
    df: DataFrame，需含 high/low/close（升序，最新行最后）
    返回: DataFrame，列 = [砖型图, 红升, 绿降, 翻红XG, 翻绿XD]
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    hhv4 = high.rolling(4, min_periods=1).max()   # HHV(HIGH,4)
    llv4 = low.rolling(4, min_periods=1).min()    # LLV(LOW,4)
    rng = (hhv4 - llv4).replace(0, 1e-9)

    var1a = (hhv4 - close) / rng * 100 - 90
    var2a = tdx_sma(var1a, 4, 1) + 100
    var3a = (close - llv4) / rng * 100
    var4a = tdx_sma(var3a, 6, 1)
    var5a = tdx_sma(var4a, 6, 1) + 100
    var6a = var5a - var2a

    brick = np.where(var6a > 4, var6a - 4, 0.0)
    brick = pd.Series(brick, index=df.index)

    prev = brick.shift(1)
    red_up = prev < brick            # 红柱（动量增强）
    green_dn = prev > brick          # 绿柱（动量衰减）
    # XG = 前一根非上升 且 当前上升 → 绿转红首次翻红
    prev_red = red_up.shift(1).fillna(False).astype(bool)
    xg = (~prev_red) & red_up
    # XD = 前一根非下降 且 当前下降 → 红转绿首次翻绿
    prev_green = green_dn.shift(1).fillna(False).astype(bool)
    xd = (~prev_green) & green_dn

    return pd.DataFrame({
        "砖型图": brick.round(2),
        "红升": red_up,
        "绿降": green_dn,
        "翻红XG": xg,
        "翻绿XD": xd,
    }, index=df.index)


def _green_peak_before(b, i):
    """
    找第i根之前最近一段连续绿柱的最大高度
    （连续绿柱的起点 = 上一次红柱之后的第一个绿柱）
    """
    if i <= 0:
        return 0.0
    peak = 0.0
    j = i - 1
    while j >= 0 and b["绿降"].iloc[j]:
        peak = max(peak, float(b["砖型图"].iloc[j]))
        j -= 1
    return peak


def _brick_ratio_desc():
    """强红阈值的中文分数描述（从 CONFIG.BRICK_RATIO 反推，避免硬编码 '2/3'）"""
    from fractions import Fraction
    fr = Fraction(CONFIG.BRICK_RATIO).limit_denominator(10)
    return f"{fr.numerator}/{fr.denominator}"


def analyze_brick_patterns(df, dual=None):
    """
    砖型图完整形态分析（图形买点体系进阶版）
    df: K线数据
    dual: 可选，analyze_dual_line()的结果（黄白线多头/空头判断）
    返回: dict 完整信号
    """
    b = brick_chart(df)
    last = len(b) - 1
    signals = []
    details = {}

    # 0. 大趋势过滤（黄白线）
    bull_env = None
    if dual and isinstance(dual, dict) and "多头区间" in dual:
        bull_env = dual["多头区间"]

    # 1. 今日是否绿翻强红（连续绿柱后第一根红砖 ≥ 前段绿峰×3/4）
    cur = float(b["砖型图"].iloc[last])
    prev_val = float(b["砖型图"].iloc[last - 1]) if last > 0 else 0

    if b["翻红XG"].iloc[last]:
        green_peak = _green_peak_before(b, last)
        details["绿峰"] = round(green_peak, 2)
        details["红砖"] = round(cur, 2)
        # 绿转红一半不干（红砖<绿柱一半=弱反弹）
        if green_peak > 0 and cur < green_peak * 0.5:
            signals.append(f"⚠ 绿转红但红砖{cur:.1f} < 绿柱{green_peak:.1f}的一半 → 弱反弹，不干")
        elif green_peak > 0 and cur >= green_peak:
            signals.append(f"★ 绿转红盖过绿柱(红砖{cur:.1f} ≥ 绿峰{green_peak:.1f}) → 强信号，可干")
            if bull_env:
                signals.append("  ✓ 多头环境+绿转红盖过 → 确定性高，可重仓")
        elif green_peak > 0 and cur >= green_peak * CONFIG.BRICK_RATIO:
            signals.append(f"★ 绿翻强红：红砖{cur:.1f} ≥ 绿峰{green_peak:.1f}×{_brick_ratio_desc()} → 强买点形态")
            if bull_env:
                signals.append("  ✓ 白线>黄线多头环境，信号有效 → 高性价比试多")
            elif bull_env is False:
                signals.append("  ⚠ 白线<黄线空头环境 → 大概率反弹诱多，禁止重仓，快进快出")
        else:
            signals.append(f"⚠ 普通绿翻红：红砖{cur:.1f} < 绿峰{green_peak:.1f}×{_brick_ratio_desc()} → 弱反弹，只做极短线不重仓")
    elif b["翻绿XD"].iloc[last]:
        signals.append("红翻绿：动量由多转空 → 短线减仓/离场参考")

    # 2. 连续红砖计数（4砖减仓纪律）
    red_count = 0
    j = last
    while j >= 0 and b["红升"].iloc[j]:
        red_count += 1
        j -= 1
    details["连续红砖"] = red_count
    if red_count >= 4:
        signals.append(f"⚠ 连续{red_count}根红砖 → 主动减仓30-50%锁定利润（非清仓！强势主升可走5-6砖）")
    elif red_count == 3:
        signals.append(f"连续{red_count}根红砖 → 接近4砖纪律位，注意减仓节奏")

    # 3. 红砖缩短检测（动能衰竭）
    if red_count >= 2:
        sizes = []
        j = last
        while j >= 0 and b["红升"].iloc[j] and len(sizes) < 4:
            sizes.append(float(b["砖型图"].iloc[j]))
            j -= 1
        sizes = sizes[::-1]  # 时间正序
        if len(sizes) >= 2 and sizes[-1] < sizes[0] * 0.8:
            signals.append("⚠ 红砖逐级缩短 → 多头动能衰竭，随时红翻绿，提前准备减仓")
        elif len(sizes) >= 2 and sizes[-1] > sizes[0]:
            signals.append("红砖逐级走高 → 健康动量结构，安心持有")

    # 4. 锯齿砖检测（震荡行情）
    lookback = min(10, last)
    window = b.iloc[last - lookback:last + 1]
    flips = ((window["红升"] != window["红升"].shift(1)) & window["红升"].notna()).sum()
    if flips >= 5:
        signals.append("⚠ 红绿频繁切换（锯齿砖）→ 典型震荡行情，减少交易，容易反复止损")

    # 5. 大阳要配大红（大阳线但红砖很短=动能不足，字幕知识点）
    # 大阳线(实体≥3%)但砖型图值很低(<5) = 动能不足
    if len(df) > 1:
        last_row = df.iloc[-1]
        last_o = float(last_row['open'])
        last_c = float(last_row['close'])
        last_h = float(last_row['high'])
        last_l = float(last_row['low'])
        last_entity = abs(last_c - last_o) / last_o * 100
        upper_shadow = last_h - max(last_c, last_o)
        lower_shadow = min(last_c, last_o) - last_l
        shadow_entity_ratio = max(upper_shadow, lower_shadow) / max(abs(last_c - last_o), 0.01)

        # 大阳配大红检测
        if last_c > last_o and last_entity >= 3 and cur < 5:
            signals.append("⚠ 大阳线(实体{:.1f}%)但红砖仅{:.1f} → 大阳要配大红，动能不足，警惕冲高回落".format(last_entity, cur))
        elif last_c > last_o and last_entity >= 3 and cur >= 10:
            signals.append("✅ 大阳配大红(实体{:.1f}%+红砖{:.1f}) → 量价配合良好，健康动量".format(last_entity, cur))

        # 下影线（锤子）检测：下影线>实体2倍 → 爆发力被透支，不做
        if lower_shadow > abs(last_c - last_o) * 2 and shadow_entity_ratio > 2:
            direction = "红锤" if last_c >= last_o else "绿锤"
            signals.append(f"⚠ {direction}(下影{lower_shadow:.3f}≥实体×2) → 下影线锤子不做，爆发力被透支")
        # 上影线不买：上影线>实体2倍
        if upper_shadow > abs(last_c - last_o) * 2 and shadow_entity_ratio > 2:
            signals.append("⚠ 上影线过长(≥实体×2) → 上影线不买，冲高回落压力大")

    # 6. 四块砖循环检测（4天是一个转折循环点）
    if red_count >= 4:
        signals.append("  📐 四块砖循环到位 → 减仓或清仓，等下一个循环")
    # 检测最近有没有连续4块绿砖（可能见底）
    green_count = 0
    j = last
    while j >= 0 and b["绿降"].iloc[j]:
        green_count += 1
        j -= 1
    if green_count >= 4:
        signals.append(f"📐 连续{green_count}块绿砖 → 四天循环，可能见底转折，关注绿转红")

    # 7. 衰竭点检测（跌破黄线后的反弹，红砖缩短=衰竭点，必须卖）
    # 只有在黄线之下（空头趋势中）的反弹衰竭点才有意义
    bull_env_bool = bull_env if bull_env is not None else False
    if not bull_env_bool and red_count >= 2:
        # 检查最近几根红砖是否在缩短
        sizes = []
        j = last
        while j >= 0 and b["红升"].iloc[j] and len(sizes) < 5:
            sizes.append(float(b["砖型图"].iloc[j]))
            j -= 1
        sizes = sizes[::-1]
        if len(sizes) >= 3 and sizes[-1] < sizes[0] * 0.6:
            signals.append("⚠ 衰竭点：黄线之下反弹红砖缩短(末砖{:.1f}<首砖{:.1f}×60%) → 反弹衰竭，必须卖".format(sizes[-1], sizes[0]))
        elif len(sizes) >= 2 and sizes[-1] < sizes[0] * 0.5:
            signals.append("⚠ 衰竭点：反弹红砖大幅缩短 → 动能衰竭，拍掉不纠结")

    # 8. 白线之上的绿砖无意义（多头趋势中的回调不用怕）
    if bull_env_bool and b["绿降"].iloc[last]:
        signals.append("白线之上绿砖 → 多头趋势中的正常回调，不是卖出信号")
    if b["红升"].iloc[last]:
        state = "红柱↑"
    elif b["绿降"].iloc[last]:
        state = "绿柱↓"
    else:
        state = "平"

    result = {
        "砖型图值": round(cur, 2),
        "状态": state,
        "连续红砖": red_count,
        "今日翻红": bool(b["翻红XG"].iloc[last]),
        "今日翻绿": bool(b["翻绿XD"].iloc[last]),
        "绿翻强红": bool(b["翻红XG"].iloc[last] and details.get("绿峰", 0) > 0 and cur >= details["绿峰"] * CONFIG.BRICK_RATIO),
        "近5日翻红次数": int(b["翻红XG"].iloc[-5:].sum()),
        "信号": signals,
    }
    if details:
        result["细节"] = details
    return result


def brick_signal(df, dual=None):
    """兼容旧接口：完整形态分析"""
    return analyze_brick_patterns(df, dual)
