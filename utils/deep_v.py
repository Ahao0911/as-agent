"""
图形买点体系 补票战法（深V策略）
当B1没进去时，在上涨趋势中找V形洗盘补票上车
核心：胜率<40%但盈亏比好，玩的是纪律不是胜率
积小胜为大胜，有B1还是优先B1
"""
import numpy as np
import pandas as pd


def detect_deep_v(df, lookback=30):
    """
    检测深V形态（补票战法）
    df: DataFrame (open/high/low/close/volume，升序)
    lookback: 回溯检测天数
    返回: dict {信号: bool, 详情: str}
    """
    if df is None or len(df) < 10:
        return {"信号": False, "详情": "数据不足"}

    c = df['close'].astype(float)
    o = df['open'].astype(float)
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    v = df['volume'].astype(float)

    # 最近3根K线
    r1 = df.iloc[-1]  # 今日
    r2 = df.iloc[-2]  # 昨日
    r3 = df.iloc[-3]  # 前日

    c1 = float(r1['close'])
    o1 = float(r1['open'])
    h1 = float(r1['high'])
    l1 = float(r1['low'])
    v1 = float(r1['volume'])

    c2 = float(r2['close'])
    o2 = float(r2['open'])
    l2 = float(r2['low'])
    v2 = float(r2['volume'])

    c3 = float(r3['close'])
    o3 = float(r3['open'])
    l3 = float(r3['low'])

    # 均线
    ma20 = float(c.iloc[-20:].mean()) if len(c) >= 20 else 0
    ma5v = float(v.iloc[-5:].mean()) if len(v) >= 5 else 0

    signals = []
    score = 0

    # ===== 条件1: 短期V形反转 =====
    # 昨日是阴线（下跌），今日是阳线（上涨），形成V
    yesterday_yin = c2 < o2
    today_yang = c1 > o1
    if yesterday_yin and today_yang:
        # V的幅度：昨日最低到今日收盘
        v_depth = (c1 - l2) / l2 * 100
        if v_depth >= 1.5:  # V的深度至少1.5%
            signals.append(f"V形反转(深度{v_depth:.1f}%)")
            score += 2
        else:
            signals.append(f"微V(深度{v_depth:.1f}%)")
            score += 1

    # ===== 条件2: 今日阳线吃掉昨日阴线（V的右脚高过左脚） =====
    if today_yang and c1 > o2:  # 今日收盘 > 昨日开盘
        signals.append(f"阳线反包(收盘{c1:.2f}>昨开{o2:.2f})")
        score += 2
    elif today_yang and c1 > c2:
        signals.append("阳线(未反包)")
        score += 1

    # ===== 条件3: 上涨趋势中（在MA20之上） =====
    if ma20 > 0 and c1 > ma20 * 0.98:
        signals.append(f"在MA20({ma20:.2f})附近或之上")
        score += 2
    else:
        signals.append("在MA20之下(趋势偏弱)")
        score -= 1

    # ===== 条件4: 量能配合（洗盘缩量后放量） =====
    # 昨日下跌缩量，今日上涨放量
    vol_ratio_today = v1 / v2 if v2 > 0 else 0
    if yesterday_yin and v2 < ma5v * 0.8 and today_yang and vol_ratio_today >= 1.2:
        signals.append(f"缩量洗盘+放量反弹(量比{vol_ratio_today:.1f}x)")
        score += 2
    elif today_yang and vol_ratio_today >= 1.5:
        signals.append(f"放量反弹(量比{vol_ratio_today:.1f}x)")
        score += 1

    # ===== 条件5: 下影线（洗盘特征） =====
    entity1 = abs(c1 - o1)
    lower_shadow1 = min(o1, c1) - l1
    if today_yang and lower_shadow1 >= entity1 * 0.5:
        signals.append("有下影线(洗盘特征)")
        score += 1

    # 综合判断
    is_signal = score >= 5
    detail = " | ".join(signals) if signals else "无V形特征"

    return {
        "信号": is_signal,
        "评分": score,
        "详情": detail,
        "V深度": round(v_depth, 1) if 'v_depth' in dir() else 0,
        "反包": bool(today_yang and c1 > o2) if 'today_yang' in dir() and 'c1' in dir() and 'o2' in dir() else False,
        "今日阳线": bool(today_yang) if 'today_yang' in dir() else False,
        "昨日阴线": bool(yesterday_yin) if 'yesterday_yin' in dir() else False,
    }


def analyze_deep_v(df):
    """
    补票战法完整分析
    """
    if df is None or len(df) < 20:
        return {"信号": False, "结论": "数据不足"}

    # 先检测V
    v = detect_deep_v(df)
    signals = []
    c = df['close'].astype(float)

    if not v.get("信号"):
        return {"信号": False, "结论": "未检测到深V补票信号", "信号列表": []}

    signals.append(f"★ 补票信号：{v['详情']} (评分{v['评分']}/10)")

    # 补充判断
    c_now = float(c.iloc[-1])
    c_5ago = float(c.iloc[-6]) if len(c) > 6 else c_now
    trend_up = c_now > c_5ago  # 5日趋势向上

    if trend_up:
        signals.append("  5日趋势向上 ✓")
    else:
        signals.append("  5日趋势偏弱，注意仅做超短线")

    # 判断后续策略
    strategy = ""
    if v.get("信号"):
        strategy = "补票信号出现：次日有利润就走（超短线），积小胜为大胜；若次日低开破今日低点则止损"
        if v.get("反包"):
            strategy += " | 阳线反包较强，可多拿一天等卤煮信号"

    return {
        "信号": True,
        "评分": v.get("评分", 0),
        "详情": v.get("详情", ""),
        "信号列表": signals,
        "策略": strategy,
        "胜率提醒": "补票战法胜率<40%，玩的是纪律和盈亏比，不是胜率",
    }


def scan_deep_v_stocks(stock_list, lookback=60):
    """
    批量扫描深V补票信号
    stock_list: [(code, name), ...]
    """
    import sys
    sys.path.insert(0, '/home/ahao/ahao-stock-agent')
    from utils.data_router import get_kline_df

    results = []
    for code, name in stock_list:
        try:
            df, meta = get_kline_df(code, bars=lookback)
            if df is None or len(df) < 20:
                continue
            r = analyze_deep_v(df)
            if r.get("信号"):
                c = float(df['close'].iloc[-1])
                results.append({
                    "代码": code, "名称": name,
                    "收盘": round(c, 2),
                    "评分": r.get("评分", 0),
                    "详情": r.get("详情", ""),
                    "策略": r.get("策略", ""),
                })
        except Exception:
            continue
    return results