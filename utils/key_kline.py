"""
图形买点体系 关键K线 战法
核心概念：90%的K线没有意义，只有少数几根关键K线管着整个走势
关键K线 = 放量 + 关键位置（突破/跌破黄线/前高前低），代表主力真实意图
"""
import numpy as np
import pandas as pd


def detect_key_klines(df, lookback=250, top_n=5):
    """
    检测关键K线
    df: DataFrame (open/high/low/close/volume，升序)
    lookback: 回溯K线数
    top_n: 最多返回N根关键K线
    返回: DataFrame 标注关键K线，列 = [is_key, reason, volume_ratio, entity_pct]
    """
    if df is None or len(df) < 20:
        return None

    df = df.copy()
    c = df['close'].astype(float)
    o = df['open'].astype(float)
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    v = df['volume'].astype(float)

    # 基础指标
    ma20 = c.rolling(20, min_periods=5).mean()
    ma60 = c.rolling(60, min_periods=5).mean()
    ma5v = v.rolling(5, min_periods=3).mean()
    avg_v = v.rolling(60, min_periods=10).mean()

    # 量比（当日量/60日均量）
    volume_ratio = v / avg_v.replace(0, np.nan)

    # 实体幅度
    entity = abs(c - o)
    entity_pct = entity / o * 100

    # 上下影
    upper_shadow = h - o.where(c > o, c)
    lower_shadow = o.where(c > o, c) - l

    # ===== 关键K线判定条件 =====
    is_key = pd.Series(False, index=df.index)

    # 条件1: 放量（量比 >= 2.0，放巨量）
    cond_volume = volume_ratio >= 2.0

    # 条件2: 放量突破/跌破关键位置（黄线/20日线/前高前低）
    # 放量突破20日线（阳线）
    cond_break_ma20 = cond_volume & (c > o) & (c.shift(1) <= ma20.shift(1)) & (c > ma20)
    # 放量跌破20日线（阴线）
    cond_breakdown_ma20 = cond_volume & (c < o) & (c.shift(1) >= ma20.shift(1)) & (c < ma20)
    # 放量突破前高（20日高点）
    hh20 = h.rolling(20, min_periods=5).max().shift(1)
    cond_break_high = cond_volume & (c > o) & (c > hh20)
    # 放量跌破前低（20日低点）
    ll20 = l.rolling(20, min_periods=5).min().shift(1)
    cond_breakdown_low = cond_volume & (c < o) & (c < ll20)

    # 条件3: 底部放量长阳（平地惊雷）
    cond_flat_thunder = cond_volume & (c > o) & (entity_pct >= 3) & (c < ma60 * 1.1)

    # 条件4: 高位放量（大阴线/大阳线，出货信号）
    cond_high_volume = cond_volume & (entity_pct >= 3) & (c > ma60 * 1.3)

    # 合并条件
    is_key = (cond_break_ma20 | cond_breakdown_ma20 | cond_break_high |
              cond_breakdown_low | cond_flat_thunder | cond_high_volume)

    df['is_key'] = is_key
    df['volume_ratio'] = volume_ratio.round(2)
    df['entity_pct'] = entity_pct.round(2)
    df['reason'] = ''

    # 标注原因
    for i in df[is_key].index:
        reasons = []
        if cond_break_ma20.loc[i]:
            reasons.append('放量突破MA20')
        if cond_breakdown_ma20.loc[i]:
            reasons.append('放量跌破MA20')
        if cond_break_high.loc[i]:
            reasons.append('放量突破前高')
        if cond_breakdown_low.loc[i]:
            reasons.append('放量跌破前低')
        if cond_flat_thunder.loc[i]:
            reasons.append('平地惊雷(底部放量长阳)')
        if cond_high_volume.loc[i]:
            reasons.append('高位放量')
        df.at[i, 'reason'] = '+'.join(reasons) if reasons else '放量'

    # 只保留最近N根
    key_df = df[df['is_key']].tail(top_n).copy()
    return key_df[['is_key', 'reason', 'volume_ratio', 'entity_pct']]


def analyze_key_klines(df, dual=None):
    """
    关键K线分析
    df: K线DataFrame
    dual: 黄白线分析结果
    返回: dict
    """
    if df is None or len(df) < 20:
        return {"error": "数据不足"}

    keys = detect_key_klines(df, lookback=len(df), top_n=5)
    if keys is None or len(keys) == 0:
        return {"检测到关键K线": 0, "信号": ["未检测到关键K线，可能处于垃圾时间或走势平淡"]}

    c = df['close'].astype(float)
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    c_now = float(c.iloc[-1])
    h_now = float(h.iloc[-1])
    l_now = float(l.iloc[-1])

    signals = []
    key_lines = []

    for idx, row in keys.iterrows():
        pos = df.index.get_loc(idx)
        row_data = df.iloc[pos]
        reason = row['reason']
        vol_ratio = row['volume_ratio']
        entity_pct = row['entity_pct']
        key_high = float(row_data['high'])
        key_low = float(row_data['low'])
        key_close = float(row_data['close'])
        days_ago = len(df) - 1 - pos

        # 关键K线的高低点
        line_info = {
            "日期": str(idx)[:10],
            "天数前": days_ago,
            "原因": reason,
            "高点": round(key_high, 2),
            "低点": round(key_low, 2),
            "收盘": round(key_close, 2),
            "量比": float(vol_ratio),
            "实体%": float(entity_pct),
        }
        key_lines.append(line_info)

        # 当前价格相对于这根关键K线的位置
        if c_now > key_high:
            status = "已突破上方"
            note = f"突破关键K线高点{key_high:.2f}，方向向上"
        elif c_now < key_low:
            status = "已跌破下方"
            note = f"跌破关键K线低点{key_low:.2f}，方向向下"
        else:
            status = "在区间内"
            note = f"仍在关键K线({key_high:.2f}~{key_low:.2f})的管控范围内，垃圾时间"

        signals.append(
            f"★ 关键K线({days_ago}天前, {str(idx)[:10]})：{reason} "
            f"(量比{vol_ratio}x, 实体{entity_pct}%) "
            f"→ 区间[{key_low:.2f}, {key_high:.2f}] → 当前{status}：{note}"
        )

    # 最近一根关键K线
    latest = key_lines[0] if key_lines else None

    # 垃圾时间判断
    in_junk = False
    if latest:
        in_junk = latest["低点"] < c_now < latest["高点"]

    # 综合结论
    conclusion = ""
    if in_junk:
        conclusion = "当前处于关键K线管控的垃圾时间，无交易价值，等待新关键K线出现"
    elif latest and c_now > latest["高点"]:
        conclusion = "已突破关键K线上方，方向向上，等回踩确认或B1跟进"
    elif latest and c_now < latest["低点"]:
        conclusion = "已跌破关键K线下方，方向向下，等新的关键K线或B1"

    return {
        "检测到关键K线": len(key_lines),
        "关键K线列表": key_lines,
        "最近关键K线": latest,
        "当前在垃圾时间": in_junk,
        "信号": signals,
        "结论": conclusion,
    }


def is_junk_time(df, dual=None):
    """
    判断当前是否处于垃圾时间（无交易价值）
    返回: (bool, str)
    """
    r = analyze_key_klines(df, dual)
    if r.get("检测到关键K线", 0) == 0:
        return True, "无关键K线参照，走势不明朗"
    if r.get("当前在垃圾时间", False):
        return True, f"在关键K线区间内震荡，垃圾时间，等待突破"
    return False, "已脱离关键K线管控区间，方向明确"