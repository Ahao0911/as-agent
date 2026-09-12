# -*- coding: utf-8 -*-
"""
个股把脉 · 对标高手看板
========================
数据源: 行情/指标走 ftshare + a-stock-data; 基本面走新浪三表(能直连) + 东财(astock skill, 用户本地可通)。

输出结构(对齐高手看板):
  股票基本信息 / 综合评分(基本面50+技术面50) / 日线指标 / 周线定方向
  B1检测(T1~T7) / B2放量 / S1出货排查 / 基本面(F10) / 质量评级 / 建议 / K线表格

用法:
    from utils.diag import get_diag
    r = get_diag("601899")
"""
import os
import sys
import json
import pandas as pd
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils.indicators import white_line, yellow_line, kdj_j, ma, ema
from utils.macd_analysis import macd_analysis
from utils.data_router import get_daily_bars


# ===================== 赛道分类(真科技) =====================

HARD_TECH = ["半导体", "芯片", "存储", "光模块", "光通信", "算力", "CPO", "PCB", "AI", "人工智能",
             "机器人", "新能源", "储能", "光伏", "风电", "电池", "锂电", "军工", "国防", "航天",
             "卫星", "创新药", "生物", "医疗", "软件", "云计算", "数据", "信创", "集成电路",
             "消费电子", "通信设备", "服务器", "液冷", "激光雷达", "智能驾驶", "汽车电子"]
RESOURCE = ["有色", "黄金", "铜", "铝", "稀土", "锂", "钴", "镍", "钢铁", "煤炭", "石油", "石化",
            "化工", "新材料", "矿业", "金属", "能源", "电力", "燃气", "航运", "船舶", "建材"]
TRADITIONAL = ["银行", "保险", "证券", "地产", "房地产", "白酒", "食品", "饮料", "消费", "零售",
               "家电", "纺织", "服装", "农业", "养殖", "旅游", "传媒", "公用", "交运", "物流"]

# 知名股名称 → 赛道(行业字段缺失时兜底, 避免知名白马被误判为"题材")
NAME_SECTOR = {
    # 硬科技·新能源/半导体/科技
    "宁德时代": "硬科技", "比亚迪": "硬科技", "中芯国际": "硬科技", "海光信息": "硬科技",
    "寒武纪": "硬科技", "北方华创": "硬科技", "韦尔股份": "硬科技", "兆易创新": "硬科技",
    "隆基绿能": "硬科技", "阳光电源": "硬科技", "亿纬锂能": "硬科技", "汇川技术": "硬科技",
    "立讯精密": "硬科技", "歌尔股份": "硬科技", "中兴通讯": "硬科技", "工业富联": "硬科技",
    "中际旭创": "硬科技", "新易盛": "硬科技", "通威股份": "硬科技", "三一重工": "硬科技",
    "恒瑞医药": "硬科技", "药明康德": "硬科技",
    # 稀缺资源·有色/能源
    "紫金矿业": "资源", "洛阳钼业": "资源", "山东黄金": "资源", "中国神华": "资源",
    "陕西煤业": "资源", "中国石油": "资源", "中国石化": "资源", "万华化学": "资源",
    "北方稀土": "资源", "赣锋锂业": "资源", "天齐锂业": "资源",
    # 传统·消费/金融
    "贵州茅台": "传统", "五粮液": "传统", "泸州老窖": "传统", "山西汾酒": "传统",
    "伊利股份": "传统", "海天味业": "传统", "招商银行": "传统", "平安银行": "传统",
    "中国平安": "传统", "工商银行": "传统", "建设银行": "传统", "美的集团": "传统",
    "格力电器": "传统", "中国中免": "传统", "牧原股份": "传统", "海螺水泥": "传统",
}


def classify_sector(industry, name=""):
    """行业/名称 → 赛道分类 → (赛道标签, 赛道分)

    优先级: 知名股名称映射 > 行业关键词 > 名称关键词
    """
    # 1. 知名股名称映射(行业字段缺失时兜底)
    for key, sect in NAME_SECTOR.items():
        if key in (name or ""):
            if sect == "硬科技":
                return "硬科技·高景气", 20
            if sect == "资源":
                return "稀缺资源·周期成长", 14
            return "传统行业", 8
    # 2. 行业 + 名称关键词
    text = (industry or "") + (name or "")
    if any(k in text for k in HARD_TECH):
        return "硬科技·高景气", 20
    if any(k in text for k in RESOURCE):
        return "稀缺资源·周期成长", 14
    if any(k in text for k in TRADITIONAL):
        return "传统行业", 8
    return "题材/其他", 4


# ===================== 指标计算 =====================

def _last(v):
    try:
        return float(v.iloc[-1])
    except Exception:
        return None


def compute_daily(df):
    """日线指标: KDJ / MACD / 黄白线 / 均线 / 量能 / 位置 / 环境"""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)

    c_now = _last(c)
    v_now = _last(v)

    # KDJ
    k, d, j = kdj_j(h, l, c)
    kdj = {"J": round(_last(j), 2), "K": round(_last(k), 2), "D": round(_last(d), 2)}

    # MACD
    macd = macd_analysis(df)
    macd_info = {kk: macd.get(kk) for kk in ("DIF", "DEA", "MACD柱", "零轴")}

    # 黄白线
    wx = white_line(c)
    yx = yellow_line(c)
    wx_now = _last(wx)
    yx_now = _last(yx)
    c_wx = round((c_now - wx_now) / wx_now * 100, 2) if (wx_now and c_now) else None
    wy = {"WX": round(wx_now, 2), "YX": round(yx_now, 2),
          "状态": "多头" if (wx_now and yx_now and wx_now > yx_now) else "空头"}

    # 均线
    mas = {}
    for n in (5, 10, 20, 60):
        if len(c) >= n:
            mas[f"MA{n}"] = round(_last(ma(c, n)), 2)
    # 环境(均线排列)
    env = "均线纠缠"
    if all(k in mas for k in ("MA5", "MA10", "MA20")):
        if mas["MA5"] > mas["MA10"] > mas["MA20"]:
            env = "多头排列"
        elif mas["MA5"] < mas["MA10"] < mas["MA20"]:
            env = "空头排列"

    # 量能(volume 单位: 股; 1万手=1e6股)
    v20_high = float(v.iloc[-20:].max()) if len(v) >= 20 else v_now
    v20_mean = float(v.iloc[-20:].mean()) if len(v) >= 20 else v_now
    liang = {"量柱占比": round(v_now / v20_high * 100, 1) if v20_high else 0,  # 缩量程度=当前量/20日高量
             "今量(万手)": round(v_now / 1e6, 1),
             "20日高量(万手)": round(v20_high / 1e6, 1)}
    # 连缩天数
    shrink = 0
    for i in range(len(df) - 1, 0, -1):
        if float(v.iloc[i]) < float(v.iloc[i - 1]):
            shrink += 1
        else:
            break
    liang["连缩天数"] = shrink

    # 位置
    pos = {}
    if len(df) >= 60:
        h60 = float(h.iloc[-60:].max()); l60 = float(l.iloc[-60:].min())
        pos["60日高"] = round(h60, 2); pos["60日低"] = round(l60, 2)
        pos["距60日高"] = round((c_now - h60) / h60 * 100, 2)
    if len(df) >= 250:
        h250 = float(h.iloc[-250:].max()); l250 = float(l.iloc[-250:].min())
        pos["250日高"] = round(h250, 2); pos["250日低"] = round(l250, 2)

    return {"KDJ": kdj, "MACD": macd_info, "黄白线": wy, "C_WX": c_wx,
            "均线": mas, "环境": env, "量能": liang, "位置": pos,
            "现价": round(c_now, 2)}


def _yellow_flex(close):
    """黄线: 数据不足时降级参数(周线场景)"""
    n = len(close)
    if n >= 114:
        return yellow_line(close)
    if n >= 57:
        return (ma(close, 14) + ma(close, 28) + ma(close, 57)) / 3
    if n >= 28:
        return (ma(close, 14) + ma(close, 28)) / 2
    if n >= 14:
        return ma(close, 14)
    return ma(close, max(3, n))


def compute_weekly(df):
    """周线定方向: 日线 resample 周线 → 周 WX/YX/J/C-WX"""
    try:
        w = df.copy()
        w.index = pd.to_datetime(w.index)
        wk = w.resample("W-FRI").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        if len(wk) < 10:
            return None
        c = wk["close"].astype(float)
        wx = white_line(c)
        yx = _yellow_flex(c)
        wx_now = _last(wx); yx_now = _last(yx); c_now = _last(c)
        k, d, j = kdj_j(wk["high"].astype(float), wk["low"].astype(float), c)
        return {"趋势": "多头" if (wx_now and yx_now and wx_now > yx_now) else "空头",
                "WX": round(wx_now, 2), "YX": round(yx_now, 2),
                "周C_WX": round((c_now - wx_now) / wx_now * 100, 2),
                "周J": round(_last(j), 2)}
    except Exception:
        return None


# ===================== 战法检测 =====================

def b1_check(df, ind):
    """B1 买点检测(阶段低点极致缩量) T1~T7"""
    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    c_now = _last(c)
    prev_c = float(c.iloc[-2]) if len(c) > 1 else c_now
    chg = (c_now - prev_c) / prev_c * 100
    v_now = _last(v)
    v20_high = float(v.iloc[-20:].max()) if len(v) >= 20 else v_now
    v20_mean = float(v.iloc[-20:].mean()) if len(v) >= 20 else v_now

    j_now = ind["KDJ"]["J"]
    wx_now = ind["黄白线"]["WX"]
    yx_now = ind["黄白线"]["YX"]

    T1 = bool(wx_now and yx_now and wx_now > yx_now)          # 白线>黄线 多头
    T2 = bool(j_now is not None and j_now < 20)               # J<20 超卖
    T3 = -3.0 <= chg <= 2.5                                   # 涨跌幅区间
    T4 = v_now < 0.6 * v20_high                               # 缩量<0.6x20日高
    T5 = ind["量能"]["连缩天数"] >= 1                          # 连缩>=1天
    T6 = bool(wx_now and (c_now / wx_now) >= 0.95)            # C/WX>=95%
    T7 = ind["量能"]["量柱占比"] <= 40                          # 量柱<=40%(缩量到20日高量40%以下)

    return {"成立": all([T1, T2, T3, T4, T5, T6, T7]),
            "T1_WX大于YX": T1, "T2_J小于20": T2, "T3_涨跌区间": T3,
            "T4_缩量": T4, "T5_连缩": T5, "T6_C_WX": T6, "T7_量柱": T7,
            "当日涨跌幅": round(chg, 2)}


def b2_check(df):
    """B2 放量: 近5日放量阳线(量>5日均量1.5倍 且 收>开)"""
    c = df["close"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)
    hits = []
    for i in range(max(0, len(df) - 5), len(df)):
        v5 = float(v.iloc[max(0, i - 4):i + 1].mean())
        if float(v.iloc[i]) > v5 * 1.5 and float(c.iloc[i]) > float(o.iloc[i]):
            hits.append(str(df.index[i])[:10])
    return {"近5日放量阳线": bool(hits), "日期": hits}


def s1_check(df):
    """S1 出货排查: 近90日高位放量长上影/放量大阴线(简化)"""
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)
    signs = 0
    lookback = min(90, len(df))
    for i in range(len(df) - lookback, len(df)):
        rng = (float(h.iloc[i]) - float(l.iloc[i])) or 1e-9
        upper_shadow = (float(h.iloc[i]) - max(float(c.iloc[i]), float(o.iloc[i]))) / rng
        v20 = float(v.iloc[max(0, i - 19):i + 1].mean())
        body = float(c.iloc[i]) - float(o.iloc[i])
        # 放量长上影 或 放量大阴线
        if (float(v.iloc[i]) > v20 * 1.8 and upper_shadow > 0.5) or \
           (float(v.iloc[i]) > v20 * 1.5 and body < -0.03 * float(c.iloc[i])):
            signs += 1
    return {"近90日出货信号数": signs, "有出货": signs >= 3}


# ===================== 基本面 =====================

def _f(v):
    """安全转 float(新浪返回字符串)"""
    try:
        return float(str(v).replace(",", "").replace("%", "").replace("亿", ""))
    except (TypeError, ValueError):
        return None


def _pct(v):
    """同比 → 百分比字符串"""
    f = _f(v)
    return f"{f * 100:+.1f}%" if f is not None else ""


def get_fundamental(code):
    """基本面: 新浪三表(营收/净利/现金流/同比) + 东财行业/经营范围"""
    code = str(code).zfill(6)
    try:
        sys.path.insert(0, os.path.join(ROOT, "data-sources-astock"))
        from _skill_defs import sina_financial_report, eastmoney_stock_info
    except Exception:
        sina_financial_report = eastmoney_stock_info = None

    fund = {"行业": "", "营收": "", "营收同比": "", "净利润": "", "净利润同比": "",
            "经营现金流": "", "主营构成": "", "业务范围": "", "报告期": ""}

    # 新浪利润表
    if sina_financial_report:
        try:
            lrb = sina_financial_report(code, "lrb", 2)
            if lrb:
                row = lrb[0]
                fund["报告期"] = row.get("报告期", "")
                rev = _f(row.get("营业总收入"))
                np_ = _f(row.get("归属于母公司所有者的净利润") or row.get("净利润"))
                if rev:
                    fund["营收"] = f"{rev / 1e8:.2f}亿"
                    fund["营收同比"] = _pct(row.get("营业总收入_同比"))
                if np_:
                    fund["净利润"] = f"{np_ / 1e8:.2f}亿"
                    fund["净利润同比"] = _pct(row.get("归属于母公司所有者的净利润_同比") or row.get("净利润_同比"))
        except Exception:
            pass
        # 现金流量表
        try:
            llb = sina_financial_report(code, "llb", 2)
            if llb:
                cf = _f(llb[0].get("经营活动产生的现金流量净额"))
                if cf:
                    fund["经营现金流"] = f"{cf / 1e8:.2f}亿"
        except Exception:
            pass

    # 东财行业/经营范围(用户本地可通; 沙箱会 TLS 断开, 降级)
    if eastmoney_stock_info:
        try:
            info = eastmoney_stock_info(code)
            if info and info.get("industry"):
                fund["行业"] = info["industry"]
        except Exception:
            pass

    return fund


# ===================== 质量评级 & 综合评分 =====================

def quality_rating(fund, industry, name):
    """质量评级(满分100, 四维): 盈利30 + 现金流25 + 成长25 + 赛道20"""
    score = 0
    detail = []

    def _num(s):
        try:
            return float(str(s).replace("亿", "").replace("+", "").replace("%", ""))
        except Exception:
            return None

    rev = _num(fund.get("营收")); np_ = _num(fund.get("净利润")); cf = _num(fund.get("经营现金流"))
    rev_yoy = _num(fund.get("营收同比")); np_yoy = _num(fund.get("净利润同比"))

    # 1. 盈利质量(30)
    earn = 0
    if np_ and np_ > 0:
        earn += 8
    if np_yoy is not None:
        if np_yoy > 50: earn += 11
        elif np_yoy > 30: earn += 7
        elif np_yoy > 0: earn += 3
    if rev_yoy is not None and rev_yoy > 20: earn += 11
    elif rev_yoy is not None and rev_yoy > 0: earn += 5
    score += earn; detail.append(f"盈利{earn}")

    # 2. 现金流质量(25)
    cash = 0
    if cf and cf > 0:
        cash += 12
        if np_ and cf >= np_ * 0.8:
            cash += 13
    score += cash; detail.append(f"现金流{cash}")

    # 3. 成长性(25)
    grow = 0
    if rev_yoy is not None:
        if rev_yoy > 30: grow += 13
        elif rev_yoy > 15: grow += 9
        elif rev_yoy > 0: grow += 4
    if np_yoy is not None:
        if np_yoy > 100: grow += 12
        elif np_yoy > 50: grow += 8
        elif np_yoy > 0: grow += 4
    score += grow; detail.append(f"成长{grow}")

    # 4. 赛道(20)
    tag, sector_score = classify_sector(industry, name)
    score += sector_score; detail.append(f"赛道{sector_score}")

    if score >= 80: rating, icon = "优质·真成长", "🟢"
    elif score >= 65: rating, icon = "良好·真业绩", "🟢"
    elif score >= 50: rating, icon = "一般·观察", "🟡"
    elif score >= 35: rating, icon = "偏弱·谨慎", "🟡"
    else: rating, icon = "风险·回避", "🔴"

    return {"分数": score, "评级": rating, "图标": icon, "赛道": tag, "明细": " ".join(detail)}


def tech_score(ind, b1, b2):
    """技术面评分(满分50): 趋势15 + 买点10 + 位置10 + 量价8 + 动能7"""
    s = 0
    wy = ind["黄白线"]
    # 趋势(15)
    if wy["状态"] == "多头":
        s += 15
    else:
        s += 5
    # 买点(10)
    if b1["成立"]:
        s += 10
    elif b2["近5日放量阳线"]:
        s += 8
    # 位置(10)
    dist = ind["位置"].get("距60日高")
    if dist is not None:
        if -15 <= dist <= -5:
            s += 10
        elif dist > 0:
            s += 3
        elif dist < -30:
            s += 7
        else:
            s += 6
    # 量价(8)
    liang = ind["量能"]
    if liang["量柱占比"] <= 40 and liang["连缩天数"] >= 1:
        s += 8
    elif liang["量柱占比"] > 150:
        s += 2
    else:
        s += 4
    # 动能(7)
    macd = ind["MACD"]
    mc = macd.get("MACD柱")
    if mc is not None:
        if macd.get("零轴") == "多头" and mc > 0:
            s += 7
        elif macd.get("零轴") == "多头":
            s += 3
        else:
            s -= 2
    return max(0, min(50, s))


def composite_score(fund, industry, name, ind, b1, b2):
    """综合评分 = 基本面50(质量评级/2) + 技术面50"""
    q = quality_rating(fund, industry, name)
    base = round(q["分数"] / 2)  # 质量评级100 → 基本面50
    tech = tech_score(ind, b1, b2)
    total = base + tech
    if total >= 80: rating, icon = "优秀", "🟢"
    elif total >= 65: rating, icon = "良好", "🟢"
    elif total >= 50: rating, icon = "一般", "🟡"
    elif total >= 35: rating, icon = "偏弱", "🟡"
    else: rating, icon = "风险", "🔴"
    return {"总分": total, "基本面": base, "技术面": tech, "评级": rating, "图标": icon}


# ===================== 建议生成 =====================

def gen_advice(ind, b1, weekly, fund):
    wy = ind["黄白线"]
    c = ind["现价"]
    # 已持仓
    if wy["状态"] == "多头":
        hold = "🟢 趋势向好(白线>黄线)，可继续持有，跌破黄线(YX)或白线死叉再离场"
    else:
        hold = "🟡 空头区间，反弹减仓，白线不上穿黄线不重仓"
    # 想买入
    if b1["成立"]:
        buy = "🟢 出现B1底部买入信号，可在尾盘14:45轻仓介入，止损-3%~-5%"
    elif wy["状态"] == "多头" and (ind["位置"].get("距60日高") or 0) < 0:
        buy = "🟡 多头但未到B1，等回踩黄线/缩量企稳再低吸，勿追高"
    else:
        buy = "🔴 空头区间，暂不参与，等白线上穿黄线或B1信号"
    return {"已持仓": hold, "想买入": buy}


def company_nature(fund, industry, name):
    """公司性质标签"""
    q = classify_sector(industry, name)[0]
    np_ = None
    try:
        np_ = float(str(fund.get("净利润")).replace("亿", "").replace("+", ""))
    except Exception:
        pass
    if np_ is not None and np_ > 0 and ("科技" in q or "资源" in q or "制造" in q):
        return "✅ 真科技·有真业绩" if "科技" in q else "✅ 有真业绩"
    if np_ is not None and np_ > 0:
        return "✅ 盈利稳定"
    return "⚠️ 业绩待验证"


# ===================== 聚合入口 =====================

def get_diag(code):
    """个股把脉主入口"""
    code = str(code).zfill(6)
    result = {"status": "OK", "代码": code}

    # 1. K线(700根: 够算250日位置 + 周线黄线完整参数114周)
    r = get_daily_bars(code, count=700, purpose="indicator")
    if r.get("status") != "OK":
        return {"status": "ERROR", "msg": r.get("msg", "行情拉取失败"), "代码": code}
    data = r["data"]
    df = pd.DataFrame(data).set_index("date")
    df.index = pd.to_datetime(df.index)

    # 2. 实时价(tencent_quote)
    stock = {"名称": "", "现价": None, "涨跌幅": None, "日期": str(df.index[-1])[:10]}
    try:
        sys.path.insert(0, os.path.join(ROOT, "data-sources-astock"))
        from _skill_defs import tencent_quote
        q = tencent_quote([code])
        if code in q and q[code].get("name"):
            it = q[code]
            stock["名称"] = it.get("name", "")
            stock["现价"] = round(float(it["price"]), 2) if it.get("price") else None
            pc = float(it.get("last_close") or it.get("pre_close") or 0)
            if it.get("change_pct") is not None:
                stock["涨跌幅"] = round(float(it["change_pct"]), 2)
            elif pc:
                stock["涨跌幅"] = round((stock["现价"] - pc) / pc * 100, 2)
    except Exception:
        pass
    if not stock["现价"]:
        stock["现价"] = round(float(df["close"].iloc[-1]), 2)

    # 3. 指标计算
    ind = compute_daily(df)
    weekly = compute_weekly(df)
    b1 = b1_check(df, ind)
    b2 = b2_check(df)
    s1 = s1_check(df)
    # B1 完整战法(三层过滤+双通道+四类型) + 对子底
    from utils.b1_full import b1_full_check, pair_bottom_check
    b1_full = b1_full_check(df)
    pair_bottom = pair_bottom_check(df)

    # 4. 基本面 + 评分
    fund = get_fundamental(code)
    industry = fund.get("行业", "")
    name = stock.get("名称", "")
    q = quality_rating(fund, industry, name)
    score = composite_score(fund, industry, name, ind, b1, b2)
    advice = gen_advice(ind, b1, weekly, fund)

    # 5. 概述 + 性质
    chg = stock.get("涨跌幅")
    chg_txt = f"{chg:+.2f}%" if chg is not None else ""
    overview = (f"{name}，现价{stock['现价']}元（今日{chg_txt}）。"
                f"综合评分{score['总分']}分（{score['图标']}{score['评级']}）。"
                f"技术上{'多头' if ind['黄白线']['状态']=='多头' else '空头'}，"
                f"{'出现B1底部买入信号' if b1['成立'] else '暂无B1信号'}。")

    # 6. 近10日K线表格
    ktable = []
    last10 = df.tail(10)
    prev_close = None
    for idx, row in last10.iterrows():
        c_ = float(row["close"])
        pct = (c_ - prev_close) / prev_close * 100 if prev_close else None
        ktable.append({
            "日期": str(idx)[:10],
            "开": round(float(row["open"]), 2),
            "收": round(c_, 2),
            "高": round(float(row["high"]), 2),
            "低": round(float(row["low"]), 2),
            "涨跌": f"{pct:+.2f}%" if pct is not None else "—",
            "量万手": round(float(row["volume"]) / 1e6, 1),
        })
        prev_close = c_

    # 黄白线序列(前端画图)
    c_series = df["close"].astype(float)
    dual_seq = {
        "白线序列": [round(float(v), 3) for v in white_line(c_series).tail(120).tolist()],
        "黄线序列": [round(float(v), 3) for v in yellow_line(c_series).tail(120).tolist()],
    }

    result.update({
        "股票": stock,
        "综合评分": score,
        "概述": overview,
        "公司性质": company_nature(fund, industry, name),
        "业绩表现": ("✅ 业绩扎实：赚钱+增长+现金流入" if q["分数"] >= 50 else "⚠️ 业绩一般，关注财报"),
        "建议": advice,
        "日线指标": ind,
        "周线": weekly,
        "B1": b1,
        "B1完整": b1_full,
        "对子底": pair_bottom,
        "B2": b2,
        "S1": s1,
        "基本面": fund,
        "质量评级": q,
        "K线表格": ktable,
        "klines": data[-120:],  # 近120根(英文字段, 前端画图)
        "dual_line": dual_seq,
    })
    return result


if __name__ == "__main__":
    import json
    code = sys.argv[1] if len(sys.argv) > 1 else "601899"
    r = get_diag(code)
    print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
