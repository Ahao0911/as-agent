"""
砖型图搬砖战法 — 震荡市/箱体做T战术
大前提: 白线在黄线上方（中期多头），只在多头箱体里搬砖

仓位: 7成底仓(不动) + 3成机动仓(当日买卖，收盘平仓)

接砖(低吸): 连续绿柱调整后绿翻强红 + 单针下20低位 + 回踩箱体下沿
抛砖(高抛): 连续3~4红砖/红砖缩短/红翻绿 + 触及箱体上沿 + 盈利2~5%

纪律: 单次亏2%止损 / 连亏3次停搬 / 突破箱体停搬吃主升
"""
import numpy as np
import pandas as pd

from utils.brick import brick_chart


def box_range(df, n=40):
    """箱体区间: 近n日最高/最低（震荡箱体上下沿）"""
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    top = float(h.iloc[-n:].max())
    bottom = float(l.iloc[-n:].min())
    mid = (top + bottom) / 2
    return {"上沿": top, "下沿": bottom, "中轴": mid}


def carry_signal(df, dual=None, needle=None, box_n=40):
    """
    搬砖战法信号检测
    df: K线DataFrame
    dual: 黄白线结果（判断多头环境）
    needle: 单针下20结果（判断低位）
    返回: dict {接砖: bool, 抛砖: bool, 信号列表}
    """
    if df is None or len(df) < 60:
        return {"error": "数据不足"}

    c = df["close"].astype(float)
    b = brick_chart(df)
    box = box_range(df, box_n)

    last = len(b) - 1
    cur = float(b["砖型图"].iloc[last])
    prev1 = float(b["砖型图"].iloc[last - 1]) if last > 0 else 0
    prev2 = float(b["砖型图"].iloc[-2]) if last > 1 else prev1

    # 黄白线环境
    bull = False
    wl_val = yl_val = 0
    if dual and isinstance(dual, dict):
        bull = dual.get("多头区间", False)
        wl_val = dual.get("白线", 0)
        yl_val = dual.get("黄线", 0)

    signals = []
    today_red = cur > prev1
    yest_green = prev1 < prev2
    red_h = cur - prev1
    green_h = prev2 - prev1
    strong = green_h > 0 and red_h >= green_h * 2 / 3

    # ---- 接砖判断 ----
    catch = False
    if bull:
        # 绿翻强红拐点
        if today_red and yest_green and strong:
            # 单针低位加分
            needle_ok = needle and (needle.get("白线下20") or needle.get("四线归零")) if needle else False
            # 位置: 接近箱体下沿
            near_bottom = float(c.iloc[-1]) <= box["下沿"] * 1.03
            msg = "★ 接砖信号：绿翻强红拐点"
            if needle_ok:
                msg += " + 单针低位（共振加分）"
            if near_bottom:
                msg += " + 回踩箱体下沿"
            msg += f" → 3成机动仓低吸（箱体{box['下沿']:.2f}~{box['上沿']:.2f}）"
            signals.append(msg)
            catch = True
        elif today_red and yest_green and not strong:
            signals.append("⚠ 弱红（红柱高度<绿柱2/3）→ 不接1根小绿后的弱红")
    else:
        signals.append("✗ 白线在黄线下（空头环境）→ 不在空头箱体搬砖，越搬越亏")

    # ---- 抛砖判断 ----
    throw = False
    if bull:
        # 连续红砖计数
        red_count = 0
        j = last
        while j >= 0 and b["红升"].iloc[j]:
            red_count += 1
            j -= 1
        # 红砖缩短检测
        shrinking = False
        if red_count >= 2:
            sizes = []
            j = last
            while j >= 0 and b["红升"].iloc[j] and len(sizes) < 4:
                sizes.append(float(b["砖型图"].iloc[j]))
                j -= 1
            sizes = sizes[::-1]
            if len(sizes) >= 2 and sizes[-1] < sizes[0] * 0.8:
                shrinking = True
        # 红翻绿
        turned_green = (not today_red) and prev1 > prev2
        # 箱体上沿
        near_top = float(c.iloc[-1]) >= box["上沿"] * 0.97

        if (red_count >= 3 and (shrinking or turned_green)) or turned_green or (near_top and red_count >= 2):
            reason = []
            if red_count >= 3:
                reason.append(f"连续{red_count}砖")
            if shrinking:
                reason.append("红砖缩短")
            if turned_green:
                reason.append("红翻绿")
            if near_top:
                reason.append("触及箱体上沿")
            signals.append(f"★ 抛砖信号：{'/'.join(reason)} → 3成机动仓高抛落袋（目标+2~5%）")
            throw = True
    # 突破箱体（停搬吃主升）
    if bull and float(c.iloc[-1]) > box["上沿"] * 1.03:
        signals.append("◆ 放量突破箱体上沿 → 停止搬砖！底仓持有吃B3主升")

    return {
        "接砖": catch,
        "抛砖": throw,
        "箱体": box,
        "白线多头": bull,
        "砖型图": round(cur, 2),
        "连续红砖": red_count if bull else 0,
        "信号": signals,
    }
