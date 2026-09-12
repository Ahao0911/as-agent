"""
下影线战法（通达信原版公式翻译）
============================
核心逻辑：长下影K线，回踩20日均线，收盘站上20日线
下影足够长，实体很小，属于短线企稳反转信号

⚠ 命名说明（2026-08-16）：本模块原名为 needle20_pattern.py，与四线指标「单针下20」
（utils/needle20.py，找位置）易混淆，实为「下影线回踩20日线」形态战法（找形态），
故重命名为 lower_shadow.py 以彻底区分。两者是不同信号，可并存。

四条件：
1. 下影 >= 2*实体（下影足够长，实体很小）
2. L < MA20 AND C > MA20（盘中跌破20日线，收盘站回）
3. 下影 > 0.02 * REF(C,1)（下影幅度大于2%，排除毛刺）
4. V > MA(V,5)（成交量大于5日均量，要有量能承接）

使用方法：
    from utils.lower_shadow import lower_shadow_signal, scan_lower_shadow_stocks
    result = lower_shadow_signal(df)
    print(result['信号'])
"""

import pandas as pd
import numpy as np


def lower_shadow_lines(df):
    """
    计算下影线回踩20日线的K线要素
    df: DataFrame，需含 open/close/high/low/volume（升序）
    返回: DataFrame，列 = [实体, 下影, 上影, MA20, MA5V, 条件1~4, 单针信号]
    """
    df = df.copy()
    df['实体'] = abs(df['close'] - df['open'])
    df['下影'] = df[['open', 'close']].min(axis=1) - df['low']
    df['上影'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['MA20'] = df['close'].rolling(20, min_periods=20).mean()
    df['MA5V'] = df['volume'].rolling(5, min_periods=5).mean()

    df['条件1'] = df['下影'] >= 2 * df['实体']  # 下影>=2倍实体
    df['条件2'] = (df['low'] < df['MA20']) & (df['close'] > df['MA20'])  # 跌破MA20，收盘站回
    df['条件3'] = df['下影'] > 0.02 * df['close'].shift(1)  # 下影>前收2%
    df['条件4'] = df['volume'] > df['MA5V']  # 量>5日均量
    df['单针信号'] = df['条件1'] & df['条件2'] & df['条件3'] & df['条件4']

    return df


def lower_shadow_signal(df, lookback=10):
    """
    下影线回踩20日线战法完整信号分析
    df: K线DataFrame（升序）
    lookback: 回溯检测天数
    返回: dict
    """
    nd = lower_shadow_lines(df)
    last = len(nd) - 1
    signals = []

    # 最近N天是否有信号
    recent = nd.iloc[-lookback:]
    hit_dates = recent[recent['单针信号'] == True].index.tolist()

    today_hit = bool(nd['单针信号'].iloc[last]) if len(nd) > 20 else False
    yesterday_hit = bool(nd['单针信号'].iloc[last - 1]) if len(nd) > 21 and last >= 1 else False

    if len(hit_dates) > 0:
        count = len(hit_dates)
        # 按时间倒序（最近在先）
        days_ago = []
        for idx in hit_dates[-5:]:
            row = nd.loc[idx]
            ago = len(nd) - 1 - nd.index.get_loc(idx)
            days_ago.append(ago)
            date_str = row.get('date', str(row.name)) if 'date' in nd.columns else str(row.name)
            need = nd.loc[idx]
            signals.append(
                f"★ 单针下20信号（{ago}天前, {date_str}）："
                f"下影{need['下影']:.3f}/实体{need['实体']:.3f}={need['下影']/need['实体']:.1f}x, "
                f"MA20={need['MA20']:.2f}, "
                f"量比{need['volume']/need['MA5V']:.2f}x"
            )

        # 信号后的表现推演
        if len(hit_dates) >= 1 and not today_hit:
            last_sig_idx = hit_dates[-1]
            last_sig_pos = nd.index.get_loc(last_sig_idx)
            if last_sig_pos + 1 < len(nd):
                sig_day = nd.iloc[last_sig_pos]
                today = nd.iloc[-1]
                ret = (today['close'] / sig_day['close'] - 1) * 100
                days_since = len(nd) - 1 - last_sig_pos
                signals.append(f"  └ 信号后{days_since}天: 累计涨幅{ret:+.2f}%（信号日收{sig_day['close']:.2f}→现{today['close']:.2f}）")

        # 当前砖型配合
        if today_hit:
            signals.append(f"  ⚠ 今日出现单针下20信号！关注次日确认：次日不低开+收红更可靠")
        elif yesterday_hit:
            signals.append(f"  ⚠ 昨日出现单针下20信号！今日确认：今日收盘{nd['close'].iloc[last]:.2f}，若收红则信号有效")
    else:
        # 无信号，但看看是否接近（用于预判）
        row = nd.iloc[last]
        near_conditions = []
        if row['下影'] >= row['实体'] * 1.5:
            near_conditions.append(f"下影/实体={row['下影']/row['实体']:.1f}x（接近2倍）")
        if row['low'] < row['MA20']:
            near_conditions.append(f"跌破MA20={row['MA20']:.2f}")
        if row['close'] > row['MA20']:
            near_conditions.append(f"站回MA20={row['MA20']:.2f}")
        if row['volume'] > row['MA5V']:
            near_conditions.append(f"量比{row['volume']/row['MA5V']:.2f}x > 1")
        if near_conditions:
            signals.append("近信号条件: " + " | ".join(near_conditions))

    # 当前K线形态
    row = nd.iloc[last]
    if len(nd) > 20:
        signals.append(
            f"当前K线: 下影{row['下影']:.3f} 实体{row['实体']:.3f} "
            f"MA20={row['MA20']:.2f} "
            f"量比{row['volume']/row['MA5V']:.2f}x"
        )

    return {
        "今日信号": today_hit,
        "昨日信号": yesterday_hit,
        "近N日信号次数": len(hit_dates),
        "最近信号日": str(hit_dates[-1]) if len(hit_dates) > 0 else None,
        "信号": signals,
        # 最近K线要素（供外部调用）
        "当前下影": round(float(row['下影']), 3) if len(nd) > 20 else 0,
        "当前实体": round(float(row['实体']), 3) if len(nd) > 20 else 0,
        "当前MA20": round(float(row['MA20']), 2) if len(nd) > 20 else 0,
        "当前量比": round(float(row['volume'] / row['MA5V']), 2) if len(nd) > 20 and row['MA5V'] > 0 else 0,
    }


def scan_lower_shadow_stocks(stock_list, lookback=60):
    """
    扫描多只股票，检测近期下影线回踩20日线信号
    stock_list: [(code, name), ...]
    lookback: 检测最近N天
    返回: [(code, name, 信号日期, 涨幅), ...]
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils.data_router import get_kline_df

    results = []
    for code, name in stock_list:
        try:
            df, meta = get_kline_df(code, bars=lookback + 30)
            if df is None or len(df) < 30:
                continue
            sig = lower_shadow_signal(df, lookback=lookback)
            if sig['近N日信号次数'] > 0:
                results.append((code, name, sig['最近信号日'], sig['信号']))
        except Exception:
            continue
    return results