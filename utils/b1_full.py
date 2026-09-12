# -*- coding: utf-8 -*-
"""
B1 完整战法 + 对子底 v8.0(对标高手看板)
======================================
B1 三层过滤: T1 周线 WX(14)>YX(57) 趋势多头 / T2 C/WX 98%~102% 粘线回踩 / T3 J<20 超跌
双通道: 标准B1(满足三层过滤) / 牵牛绳(堆量+趋势反转, 无J值限制)
四类型: 完美一 / 完美二 / 标准 / 渣渣
评分: J深度 + 粘线度 + 缩量度 + 趋势(共100分)

对子底: 波段底部对子价 + 不回踩 + 5日波段验证
"""
import pandas as pd
import numpy as np

from utils.indicators import ema, ma, kdj_j, white_line, yellow_line


def is_pair_price(price):
    """对子价: 小数两位相同(5.55/6.66/12.22) 或 整数豹子(555/666)"""
    try:
        s = f"{float(price):.2f}"
    except (ValueError, TypeError):
        return False
    a, b = s.split(".")
    if len(b) >= 2 and b[-2] == b[-1]:
        return True
    if len(a) >= 3 and a[-3] == a[-2] == a[-1]:
        return True
    return False


def weekly_wx_yx(weekly_df):
    """周线 WX(14)=EMA(EMA(周C,14),14), YX(57)=MA(周C,57)(数据不足降级)"""
    c = weekly_df["close"].astype(float)
    wx = ema(ema(c, 14), 14)
    n = len(c)
    if n >= 57:
        yx = ma(c, 57)
    elif n >= 28:
        yx = ma(c, 28)
    else:
        yx = ma(c, max(5, n))
    return float(wx.iloc[-1]), float(yx.iloc[-1])


def _resample_weekly(df):
    w = df.copy()
    w.index = pd.to_datetime(w.index)
    return w.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()


def b1_full_check(df):
    """B1 完整检测: 返回三层过滤/双通道/四类型/评分/各条件明细"""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)

    wx_d = white_line(c)          # 日线白线 EMA(EMA(C,10),10)
    yx_d = yellow_line(c)         # 日线黄线 (MA14+MA28+MA57+MA114)/4
    k, d, j = kdj_j(h, l, c)

    c_now = float(c.iloc[-1])
    wx_now = float(wx_d.iloc[-1])
    yx_now = float(yx_d.iloc[-1])
    j_now = float(j.iloc[-1])
    c_wx = (c_now - wx_now) / wx_now * 100

    # 三层过滤
    try:
        weekly = _resample_weekly(df)
        wx_w, yx_w = weekly_wx_yx(weekly)
        t1_weekly = wx_w > yx_w
    except Exception:
        wx_w = yx_w = None
        t1_weekly = wx_now > yx_now  # 周线失败降级用日线多头
    t2_stick = -2.0 <= c_wx <= 2.0   # C/WX 98%~102%
    t3_j = j_now < 20                # J<20 超跌
    j_optimal = 3.0 <= j_now <= 8.0  # 强势票 J=3~8 最优

    # 双通道
    v20_mean = float(v.iloc[-20:].mean()) if len(v) >= 20 else float(v.iloc[-1])
    v20_high = float(v.iloc[-20:].max()) if len(v) >= 20 else float(v.iloc[-1])
    dui_liang = float(v.iloc[-1]) > v20_mean * 1.5          # 堆量
    shrink = float(v.iloc[-1]) / v20_high * 100 if v20_high else 100  # 缩量度%
    is_standard = t1_weekly and t2_stick and t3_j           # 标准B1
    is_niuniu = t1_weekly and dui_liang and (wx_now > yx_now)  # 牵牛绳(无J限制)

    # 四类型
    if is_standard and j_optimal and shrink <= 40:
        b1_type = "完美一"
    elif is_standard and j_optimal:
        b1_type = "完美二"
    elif is_standard:
        b1_type = "标准"
    elif is_niuniu:
        b1_type = "牵牛绳"
    else:
        b1_type = "渣渣/非B1"

    # 评分(100分: J深度30 + 粘线30 + 缩量20 + 趋势20)
    score = 0
    if 3 <= j_now <= 8:
        score += 30
    elif 8 < j_now < 15:
        score += 20
    elif j_now < 3:
        score += 15
    if abs(c_wx) <= 1.0:
        score += 30
    elif abs(c_wx) <= 2.0:
        score += 20
    if shrink <= 30:
        score += 20
    elif shrink <= 50:
        score += 10
    if t1_weekly and wx_now > yx_now:
        score += 20
    elif t1_weekly:
        score += 10

    return {
        "成立": is_standard or is_niuniu,
        "类型": b1_type,
        "评分": score,
        "三层过滤": {"T1周线多头": t1_weekly, "T2粘线": t2_stick, "T3超跌": t3_j},
        "周线WX": round(wx_w, 2) if wx_w is not None else None,
        "周线YX": round(yx_w, 2) if yx_w is not None else None,
        "J值": round(j_now, 2),
        "J最优区间": j_optimal,
        "C_WX": round(c_wx, 2),
        "缩量度": round(shrink, 1),
        "堆量": bool(dui_liang),
        "通道": "标准B1" if is_standard else ("牵牛绳" if is_niuniu else "无"),
    }


def pair_bottom_check(df, lookback=20):
    """对子底检测(简化版): 波段底部对子价 + 不回踩 + 5日波段验证

    1. 近 N 日找波段低点
    2. 低点价是对子价(尾数相同)
    3. 低点后 5 日不破位(不创新低且不低于对子价 -3%)
    4. 现价回踩到对子价 +3% 区间内
    """
    lows = df["low"].astype(float)
    closes = df["close"].astype(float)

    seg = lows.iloc[-lookback:]
    idx = seg.idxmin()
    low_price = float(lows.loc[idx])

    if not is_pair_price(low_price):
        return {"对子底": False, "原因": "波段低点非对子价", "低点": round(low_price, 2)}

    # 低点后 5 日不破位(±3%)
    try:
        pos = df.index.get_loc(idx)
        after = df.iloc[pos + 1:pos + 6]
    except Exception:
        after = pd.DataFrame()
    if len(after) == 0:
        return {"对子底": False, "原因": "无后续K线验证", "对子价": round(low_price, 2)}
    if float(after["low"].min()) < low_price * 0.97:
        return {"对子底": False, "原因": "已破位(跌破对子价-3%)", "对子价": round(low_price, 2)}

    # 现价回踩到位(对子价 +3% 内)
    cur = float(closes.iloc[-1])
    if cur > low_price * 1.03:
        return {"对子底": False, "原因": "已远离对子价(>+3%)", "对子价": round(low_price, 2)}

    return {"对子底": True, "对子价": round(low_price, 2), "低点日期": str(idx)[:10],
            "现价": round(cur, 2), "回踩幅度": round((cur - low_price) / low_price * 100, 2)}


if __name__ == "__main__":
    # 自测
    print("对子价 5.55:", is_pair_price(5.55))
    print("对子价 12.22:", is_pair_price(12.22))
    print("对子价 555.00:", is_pair_price(555.00))
    print("非对子 12.34:", is_pair_price(12.34))
