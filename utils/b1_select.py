"""
B1启动选股公式 — 图形买点体系海选池（找标的）
与砖型图/单针下20（择时）互补：B1选股池 → 等B2调整 → 单针+砖图共振入场

核心逻辑（九层过滤，由弱到强）:
1. K线分类: 只统计真阳线/真阴线（过滤假阴假阳干扰量能统计）
2. J<=13: KDJ低位，前期回调沉淀
3. 流通市值>=40亿: 剔除小盘妖股，保证流动性
4. 量能碾压: 28日多空量比>1.65 或 14日>2.25（多头资金持续吸筹）
5. GOOD28: 28天内无【开盘高位+放量大跌】
6. PLRY: 放量真阳线(VOL>1.85*前日 且 >40日均量)，14日≥2根 或 28日≥3根
7. 洗盘结构: 拉升+缩量洗盘交替（28日≥3次）
8. MAX28_OK: 28日最大成交量当天不是真阴线（无天量出货）
9. QL成本线: C > 0.99*QL (QL=0.4*MA20+0.3*MA60+0.2*MA120+0.1*MA250)

⚠ 强制过滤: B1信号后必须叠加黄白线二次筛选，白线<黄线的B1大多是下跌反弹
"""
import numpy as np
import pandas as pd

from utils.indicators import kdj_j


def _kline_classify(df):
    """K线分类: 真阳/真阴/假阳/假阴"""
    c = df["close"].astype(float)
    o = df["open"].astype(float)
    prev_c = c.shift(1)

    fake_yang = (c > o) & (c < prev_c)              # 假阳：阳线但收盘低于昨日
    fake_yin = (c < o) & (c > prev_c)               # 假阴：阴线但收盘高于昨日
    real_yang = (c > o) & ~(c < prev_c)             # 真阳：阳线且收盘创新高
    real_yin = (c < o) & ~(c > prev_c)              # 真阴：阴线且收盘走低
    return c, o, real_yang.fillna(False), real_yin.fillna(False)


def b1_signal(df, liutong_guben=None, mv_min_yi=40.0):
    """
    B1启动选股信号
    df: K线DataFrame (open/high/low/close/volume，升序)
    liutong_guben: 流通股本(股)，None时尝试从finance获取或忽略市值过滤
    mv_min_yi: 流通市值下限（亿），默认40亿
    返回: dict {B1: bool, 各层检查结果, 信号说明}
    """
    if df is None or len(df) < 260:
        return {"B1": False, "错误": "数据不足260根K线"}

    c, o, real_yang, real_yin = _kline_classify(df)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float)
    checks = {}
    reasons = []

    # 1. KDJ低位 J<=13
    _, _, j = kdj_j(high, low, c)
    j_val = float(j.iloc[-1])
    j_ok = j_val <= 13
    checks["KDJ低位"] = j_ok
    if not j_ok:
        reasons.append(f"J值{j_val:.1f}>13，未在低位")

    # 2. 流通市值过滤（>=40亿）
    mv_ok = True
    if liutong_guben:
        mv = float(liutong_guben) * float(c.iloc[-1]) / 1e8
        mv_ok = mv >= mv_min_yi
        checks[f"流通市值≥{mv_min_yi}亿"] = mv_ok
        if not mv_ok:
            reasons.append(f"流通市值{mv:.0f}亿<{mv_min_yi}亿")
    else:
        checks["流通市值"] = "无数据跳过"

    # 3. 多周期量能对比
    vol_yang1 = (vol * real_yang).rolling(28, min_periods=1).sum()
    vol_yin1 = (vol * real_yin).rolling(28, min_periods=1).sum() + 1e-9
    vol_yang2 = (vol * real_yang).rolling(14, min_periods=1).sum()
    vol_yin2 = (vol * real_yin).rolling(14, min_periods=1).sum() + 1e-9
    yy1 = vol_yang1.iloc[-1] / vol_yin1.iloc[-1]
    yy2 = vol_yang2.iloc[-1] / vol_yin2.iloc[-1]
    yangyin_ok = yy1 > 1.65 or yy2 > 2.25
    checks["量能碾压"] = yangyin_ok
    if not yangyin_ok:
        reasons.append(f"量能不足(28日比{yy1:.2f}/14日比{yy2:.2f})")

    # 4. GOOD28: 28天内无【开盘高位+放量大跌】
    top15o = o >= o.rolling(28, min_periods=1).max() * 0.85   # 开盘在28日高位区
    fd15 = (c < o) & (vol > vol.rolling(20, min_periods=1).mean() * 1.5)  # 放量下跌
    bad_k = (top15o & fd15).rolling(28, min_periods=1).sum()
    good28 = bad_k.iloc[-1] <= 0
    checks["无高位放量跌"] = good28
    if not good28:
        reasons.append(f"28日内有{int(bad_k.iloc[-1])}根高位放量下跌")

    # 5. PLRY 放量进攻阳线
    avg40 = vol.rolling(40, min_periods=1).mean()
    prev_vol = vol.shift(1)
    plry = (vol > 1.85 * prev_vol) & (c > o) & (vol > avg40)
    plry14 = int(plry.iloc[-14:].sum()) if len(plry) >= 14 else 0
    plry28 = int(plry.iloc[-28:].sum()) if len(plry) >= 28 else 0
    plry_ok = plry14 >= 2 or plry28 >= 3
    checks["放量进攻阳线"] = plry_ok
    if not plry_ok:
        reasons.append(f"放量阳线不足(14日{plry14}根/28日{plry28}根)")

    # 6. 洗盘结构: 拉升+缩量洗盘交替（28日≥3）
    plry_first = plry & ~plry.shift(1).fillna(False).astype(bool)   # 首次进攻
    plry_cont = plry & plry.shift(1).fillna(False).astype(bool)     # 连续进攻
    half_down = (c < o) & (vol < vol.rolling(20, min_periods=1).mean() * 0.8)  # 缩量回调
    three_sum = (plry_first | plry_cont | half_down).rolling(28, min_periods=1).sum()
    three_ok = three_sum.iloc[-1] >= 3
    checks["拉升洗盘结构"] = three_ok
    if not three_ok:
        reasons.append(f"拉升/洗盘结构K线不足({int(three_sum.iloc[-1])}/3)")

    # 7. MAX28_OK: 28日最大量当天不是真阴线
    max_vol_28 = vol.rolling(28, min_periods=1).max()
    is_max_day = (vol == max_vol_28)
    max_bad = (is_max_day & real_yin).rolling(28, min_periods=1).sum()
    max28_ok = max_bad.iloc[-1] <= 0
    checks["无天量阴线"] = max28_ok
    if not max28_ok:
        reasons.append("28日最大成交量当天为阴线（天量出货嫌疑）")

    # 8. QL成本线
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    ma120 = c.rolling(120).mean()
    ma250 = c.rolling(250).mean()
    ql = 0.4 * ma20 + 0.3 * ma60 + 0.2 * ma120 + 0.1 * ma250
    ql_val = float(ql.iloc[-1])
    c_val = float(c.iloc[-1])
    ql_ok = c_val > 0.99 * ql_val
    checks["QL成本线上"] = ql_ok
    if not ql_ok:
        reasons.append(f"收盘{c_val:.2f}在QL成本线{ql_val:.2f}下方")

    b1 = all([j_ok, mv_ok, yangyin_ok, good28, plry_ok, three_ok, max28_ok, ql_ok])

    result = {
        "B1": bool(b1),
        "日期": str(df.index[-1]),
        "收盘": round(c_val, 2),
        "J值": round(j_val, 1),
        "QL": round(ql_val, 2),
        "检查": checks,
        "信号说明": reasons if not b1 else ["✓ B1启动信号成立（全部条件满足）"],
    }
    return result


def screen_b1(pool, fetch_func, mv_map=None):
    """
    批量B1选股
    pool: 股票代码列表
    fetch_func: 取K线函数(code)->df
    mv_map: {code: 流通股本}，None时跳过市值过滤
    返回: (命中列表, 未命中列表, 失败列表)
    """
    hits, misses, errors = [], [], []
    for code in pool:
        code = str(code).zfill(6)
        df = fetch_func(code)
        if df is None:
            errors.append((code, "无数据"))
            continue
        try:
            mv = mv_map.get(code) if mv_map else None
            r = b1_signal(df, liutong_guben=mv)
            if r["B1"]:
                hits.append({"代码": code, "收盘": r["收盘"], "J值": r["J值"]})
            else:
                misses.append((code, r.get("信号说明", ["未知"])[:2]))
        except Exception as e:
            errors.append((code, str(e)[:50]))
    return hits, misses, errors
