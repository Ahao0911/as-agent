"""
通用技术指标库 — 所有专家共用
基于通达信原版公式实现（图形买点体系知行趋势线 黄白线原版）：
- 白线(知行短期趋势线) = EMA(EMA(C,10),10)
- 黄线(知行多空线) = (MA(C,M1)+MA(C,M2)+MA(C,M3)+MA(C,M4))/4
  参数面板预设：M1=14, M2=28, M3=57, M4=114
核心逻辑：白线在黄线之上 = 多头区间（可做）；白线在黄线之下 = 空头区间（不做）
"""
import pandas as pd


def ema(series, n):
    """指数移动平均（通达信 EMA 算法：EMA(X,N)=2/(N+1)*X + (N-1)/(N+1)*EMA'）"""
    return series.ewm(span=n, adjust=False).mean()


def ma(series, n):
    """简单移动平均"""
    return series.rolling(n).mean()


def white_line(close, n=10):
    """短期趋势线（白线）= EMA(EMA(C,10),10)"""
    return ema(ema(close, n), n)


def yellow_line(close, m1=14, m2=28, m3=57, m4=114):
    """知行多空线（黄线）= (MA(C,M1)+MA(C,M2)+MA(C,M3)+MA(C,M4))/4
    原版参数面板预设：M1=14, M2=28, M3=57, M4=114"""
    return (ma(close, m1) + ma(close, m2) + ma(close, m3) + ma(close, m4)) / 4


def bbi(close, m1=14, m2=28, m3=57, m4=114):
    """知行多空线（黄线）别名"""
    return yellow_line(close, m1, m2, m3, m4)


def cross_over(a, b):
    """a 上穿 b（金叉）：前一根 a<=b，当前 a>b"""
    return (a.shift(1) <= b.shift(1)) & (a > b)


def cross_under(a, b):
    """a 下穿 b（死叉）：前一根 a>=b，当前 a<b"""
    return (a.shift(1) >= b.shift(1)) & (a < b)


def kdj_j(high, low, close, n=9, m=3):
    """KDJ 的 J 值（通达信算法）"""
    low_n = low.rolling(n, min_periods=1).min()
    high_n = high.rolling(n, min_periods=1).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, 1e-9) * 100
    k = rsv.ewm(com=m - 1, adjust=False).mean()
    d = k.ewm(com=m - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def analyze_dual_line(df):
    """
    双线战法信号分析
    df: 需包含列 high/low/close/volume（升序，最新行在最后）
    返回 dict：最新状态 + 信号列表
    """
    if df is None or len(df) < 30:
        return {"error": "数据不足30根K线"}

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float) if "volume" in df else pd.Series([0] * len(df), index=df.index)

    wl = white_line(close)       # 白线（知行短期趋势线）
    yl = yellow_line(close)      # 黄线（知行多空线）
    _, _, j = kdj_j(high, low, close)

    last = len(df) - 1
    signals = []

    # 0. 核心：多头区间 / 空头区间（白线在黄线之上=多头，只做多头）
    bull = wl.iloc[last] > yl.iloc[last]
    if bull:
        signals.append("★ 多头区间：白线在黄线之上 → 可做（只做这种票）")
    else:
        signals.append("✗ 空头区间：白线在黄线之下 → 不做（不参与空头区间）")

    # 1. 白黄位置关系
    if bull:
        signals.append(f"右侧交易区有效（白线{wl.iloc[last]:.2f} > 黄线{yl.iloc[last]:.2f}）")
    else:
        signals.append(f"线下无右侧交易价值（白线{wl.iloc[last]:.2f} < 黄线{yl.iloc[last]:.2f}）")

    # 2. 金叉死叉
    if cross_over(wl, yl).iloc[last]:
        signals.append("★ 白线金叉黄线（今日）→ 上涨趋势确立，等回踩B1可入场")
    elif cross_under(wl, yl).iloc[last]:
        signals.append("★ 白线死叉黄线（今日）→ 最后离场时机，走错也要走")

    # 3. 收盘价 vs 白线
    if close.iloc[last] > wl.iloc[last]:
        signals.append(f"收盘价在白线上方（白线{wl.iloc[last]:.2f}），短期趋势未破")
    else:
        signals.append(f"收盘价跌破白线（白线{wl.iloc[last]:.2f}），短期趋势走坏")

    # 4. 缩量回踩黄线（放量金叉后缩量回踩=连续拉升前最后震仓）
    recent_vol_avg = vol.iloc[-5:].mean()
    if vol.iloc[last] < recent_vol_avg * 0.8 and close.iloc[last] > yl.iloc[last] and abs(close.iloc[last] - yl.iloc[last]) / yl.iloc[last] < 0.02:
        signals.append("★ 缩量回踩黄线附近 → 连续拉升前最后震仓，交易价值大")

    # 5. J 值状态
    j_val = j.iloc[last]
    if j_val < -10:
        signals.append(f"J值大负值({j_val:.1f}) → 超卖区，配合N型结构不破=极限买点，只输一根")
    elif j_val > 100:
        signals.append(f"J值超买({j_val:.1f}) → 追高风险大")

    # 6. 要死叉未死叉（N型结构不破 + J大负值 = 极限买点）
    if not cross_under(wl, yl).iloc[last] and wl.iloc[last] > yl.iloc[last] and (wl.iloc[last] - yl.iloc[last]) / yl.iloc[last] < 0.01 and j_val < 20:
        signals.append("★ 白黄要死叉未死叉 + J值低 → 极限买点，只输一根")

    # 7. 白线黄线之间的操作区间（1004新增）
    # 股价在白线和黄线之间 = 区间内可以买，但要排除出货的票
    wl_v = float(wl.iloc[last])
    yl_v = float(yl.iloc[last])
    c_v = float(close.iloc[last])
    if bull and yl_v < c_v < wl_v:
        signals.append(f"★ 股价在白线与黄线之间({yl_v:.2f}<{c_v:.2f}<{wl_v:.2f}) → 建仓区间，配合B1可入场")
        signals.append("  ⚠ 但需排除出货迹象：顶部放量大阴线/大风车/阶梯量/次高点巨量成因")
    elif bull and c_v < yl_v:
        signals.append(f"股价跌破黄线({yl_v:.2f}) → 即使白线还在黄线之上，短期走弱")

    # 8. 连续放量站上黄线 = 大哥入场（1004新增）
    if len(close) > 5:
        recent_c = close.iloc[-5:]
        recent_v = vol.iloc[-5:]
        above_yl = all(recent_c.iloc[i] > yl.iloc[-5+i] if not pd.isna(yl.iloc[-5+i]) else True for i in range(5))
        vol_rising = all(recent_v.iloc[i] > recent_v.iloc[i-1] if i > 0 else True for i in range(1, 5))
        if above_yl and vol_rising:
            signals.append("★ 连续放量站上黄线 → 大哥入场信号，等回调B1跟进")

    # 9. 红肥绿瘦顶部判断（1004新增）
    # 看最近20根K线的红柱vs绿柱面积对比
    if len(close) > 20:
        recent_20 = df.iloc[-20:]
        red_vol = recent_20[recent_20['close'] >= recent_20['open']]['volume'].sum()
        green_vol = recent_20[recent_20['close'] < recent_20['open']]['volume'].sum()
        total_vol = red_vol + green_vol
        if total_vol > 0:
            red_ratio = red_vol / total_vol * 100
            if red_ratio > 60:
                signals.append(f"红肥绿瘦(红柱面积{red_ratio:.0f}%) → 买盘强于卖盘，健康")
            elif red_ratio < 40:
                signals.append(f"⚠ 绿肥红瘦(红柱仅{red_ratio:.0f}%) → 卖盘强于买盘，警惕出货")
            else:
                signals.append(f"红绿面积均衡(红{red_ratio:.0f}%/绿{100-red_ratio:.0f}%) → 多空平衡")

    # 7. 放量阳线（B1/B2确认）
    if close.iloc[last] > close.iloc[last - 1] and vol.iloc[last] > vol.iloc[last - 1] * 1.3:
        signals.append(f"今日放量阳线（量比{vol.iloc[last] / vol.iloc[last - 1]:.1f}）→ 关注是否B2确认")

    result = {
        "日期": str(df.index[last]),
        "收盘": round(close.iloc[last], 2),
        "白线": round(wl.iloc[last], 2),
        "黄线": round(yl.iloc[last], 2),
        "J值": round(j_val, 1),
        "多头区间": bool(bull),   # True=白线在黄线上，可做
        "信号": signals
    }
    return result


def analyze_trend(df):
    """均线趋势辅助：MA60 + 异动检测"""
    if df is None or len(df) < 60:
        return {"error": "数据不足60根K线"}
    close = df["close"].astype(float)
    vol = df["volume"].astype(float) if "volume" in df else None
    ma60 = ma(close, 60)
    last = len(df) - 1
    result = {"MA60": round(ma60.iloc[last], 2)}
    if close.iloc[last] > ma60.iloc[last]:
        result["位置"] = "MA60上方"
    else:
        result["位置"] = "MA60下方"
    # 异动：突然放量价升
    if vol is not None and len(df) > 1:
        if vol.iloc[last] > vol.iloc[-6:-1].mean() * 2 and close.iloc[last] > close.iloc[last - 1]:
            result["异动"] = f"★ 突然放量({vol.iloc[last] / vol.iloc[-6:-1].mean():.1f}倍)+价升 → 异动关注"
    return result
