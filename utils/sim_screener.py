"""
Ahao Stock Agent · 代码条件选股(sim_screener)
纯代码选股,不依赖自然语言查询:
  1. 新浪全市场实时快照(5542只,~20s)
  2. 代码粗筛(涨幅/成交额/非ST/非北交所)
  3. 每战法取前N只,拉K线算指标精筛(黄白线/砖型/单针下20/单针下30)
战法分组(图形买点体系):
  - B1组:   J值超跌(≤13/20) + 黄白线多头
  - 砖型组: 砖型图绿翻强红/红柱
  - 单针组: 单针下20 = 短期线≤20 & 长期线≥CONFIG.NEEDLE_LONG_MIN (回调买点)
  - 深V组:  单针下30 = 短期线≤30 & 长期线≤80 (洗盘日补票;深V即单针,非独立形态)
用法:
    from utils.sim_screener import screen_by_code
    result = screen_by_code()   # {组名: [{代码,名称,最新价,score,note}, ...]}
"""
import os
import sys
import time
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from utils.strategy_config import CONFIG  # 图形买点体系唯一参数源(单针长期线门槛等)

GROUPS = ["B1组", "砖型组", "单针组", "深V组"]
TOP_N = 12          # 每战法粗筛后取前N只精筛(控制K线拉取量)
MIN_AMOUNT = 5e7    # 成交额下限 5000万


SINA_SPOT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SINA_PAGE_NUM = 80   # 新浪单页条数


def get_market_snapshot(max_pages=90, retry=2, sleep=0.15):
    """新浪全市场实时快照(自研分页拉取,替代 akshare 全量接口)
    akshare 的 stock_zh_a_spot() 分页拉70页,任一一页返回异常即整体抛
    "No value to decode";且连跑易被新浪临时封IP。此处改为逐页拉取,
    失败页重试 retry 次仍失败则跳过,单页异常不拖垮全流程。
    返回: DataFrame(列与 akshare 版一致: 代码/名称/最新价/涨跌幅/...)
    """
    import requests as _req
    rows = []
    page = 1
    while page <= max_pages:
        payload = {
            "page": page, "num": SINA_PAGE_NUM,
            "sort": "symbol", "asc": 1, "node": "hs_a",
        }
        ok = False
        data = None
        for attempt in range(retry + 1):
            try:
                r = _req.get(SINA_SPOT_URL, params=payload, timeout=10)
                data = r.json()
                if not isinstance(data, list) or not data:
                    break  # 空页=已到末尾
                rows.extend(data)
                ok = True
                break
            except Exception:
                if attempt < retry:
                    time.sleep(sleep * 3)
                continue
        if not ok or data is None:
            break  # 连续失败,停止翻页
        if len(data) < SINA_PAGE_NUM:
            break  # 不足整页 = 最后一页
        page += 1
        time.sleep(sleep)

    if not rows:
        raise RuntimeError("新浪全市场快照拉取失败(0行),请稍后重试")
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "symbol": "代码", "name": "名称", "trade": "最新价",
        "changepercent": "涨跌幅", "amount": "成交额",
        "open": "今开", "settlement": "昨收",
        "high": "最高", "low": "最低", "volume": "成交量",
    })
    df["代码"] = df["代码"].astype(str)
    df["纯代码"] = df["代码"].str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6)
    for c in ["最新价", "涨跌幅", "成交额", "今开", "昨收", "最高", "最低"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def rough_screen(df):
    """
    代码粗筛 → 每战法候选池(纯代码条件)
    返回: {组名: DataFrame}
    """
    # 基础过滤: 非北交所、非ST、非次新(C/N开头)、有价、成交额够
    base = df[
        (~df["代码"].str.startswith("bj"))
        & (~df["名称"].str.contains("ST", na=False))
        & (~df["名称"].str.contains("退", na=False))
        & (~df["名称"].str.startswith(("C", "N")))   # 次新
        & (df["最新价"] > 2)
        & (df["最新价"] < 300)
        & (df["成交额"] > MIN_AMOUNT)
        & (df["昨收"] > 0)
    ].copy()
    if base.empty:
        return {g: pd.DataFrame() for g in GROUPS}

    cands = {}
    # B1组: 温和回调(涨跌幅-4%~1.5%), 缩量特征优先
    b1 = base[(base["涨跌幅"] >= -4) & (base["涨跌幅"] <= 1.5)]
    b1 = b1.sort_values("涨跌幅", ascending=False)
    cands["B1组"] = b1.head(TOP_N)

    # 砖型组: 上涨且未涨停(0.5%~8%), 量能大优先
    bk = base[(base["涨跌幅"] >= 0.5) & (base["涨跌幅"] <= 8)]
    bk = bk.sort_values("成交额", ascending=False)
    cands["砖型组"] = bk.head(TOP_N)

    # 单针组(单针下20=回调买点): 当日小幅回调 -3%~2% + 振幅2%~8%(长下影需振幅但不能暴涨)
    base["振幅"] = (base["最高"] - base["最低"]) / base["昨收"] * 100
    nd = base[(base["涨跌幅"] >= -3) & (base["涨跌幅"] <= 2)
              & (base["振幅"] >= 2) & (base["振幅"] <= 8)]
    nd = nd.sort_values("振幅", ascending=False)
    cands["单针组"] = nd.head(TOP_N)

    # 深V组(单针下30=洗盘日补票): 低开(今开<昨收)且翻红(涨0~5%,非涨停) 且现价>今开
    dv = base[(base["今开"] < base["昨收"])
              & (base["涨跌幅"] >= 0) & (base["涨跌幅"] <= 5)
              & (base["最新价"] > base["今开"])]
    dv = dv.sort_values("涨跌幅", ascending=False)
    cands["深V组"] = dv.head(TOP_N)
    return cands


def _fast_kline(code, bars=250):
    """腾讯日K线直连(快,~1s/只),股票/ETF通用"""
    import requests
    import pandas as pd
    prefix = "sh" if str(code).startswith(("5", "6", "9")) else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{bars},qfq"
    r = requests.get(url, timeout=8)
    d = r.json()
    kdata = d.get("data", {}).get(f"{prefix}{code}", {})
    klines = kdata.get("qfqday") or kdata.get("day") or []
    if len(klines) < 60:
        return None
    rows = [{"date": pd.to_datetime(k[0]), "open": float(k[1]), "close": float(k[2]),
             "high": float(k[3]), "low": float(k[4]), "volume": float(k[5])} for k in klines]
    return pd.DataFrame(rows).set_index("date")


def _verify_code(code, group):
    """
    拉K线(腾讯直连),纯代码算指标判定战法
    返回: (ok, score, note)
    """
    if code.startswith(("4", "8", "92")):
        return False, 0, "北交所"
    try:
        df = _fast_kline(code, 250)
        if df is None:
            return False, 0, "K线不足"
    except Exception as e:
        return False, 0, f"K线失败:{str(e)[:30]}"

    from utils.indicators import analyze_dual_line, ma
    from utils.brick import analyze_brick_patterns
    last = df.iloc[-1]
    try:
        dual = analyze_dual_line(df)
        bull = dual.get("多头区间")
        j = dual.get("J值", 99)

        if group == "B1组":
            # B系战法组(B1/B2/B3 同一战法波段, 全算信号):
            #   B1超跌首买(J≤13+多头) / B2突破确认(放量阳线破黄线/前高) / B3加速主升(J≥80+快速拉升)
            s = 0; n = []
            if bull: s += 3; n.append("多头")
            if j <= 13: s += 3; n.append(f"B1:J{j:.0f}≤13")
            elif j <= 20: s += 2; n.append(f"J{j:.0f}")
            # B2: 放量阳线突破黄线/20日前高, 多头环境
            c_now = float(df["close"].iloc[-1])
            c_prev = float(df["close"].iloc[-2])
            o_now = float(df["open"].iloc[-1])
            vol = df["volume"]
            v_now = float(vol.iloc[-1]); v_prev = float(vol.iloc[-2])
            yl_val = float(dual.get("黄线", 0) or 0)
            hh20 = float(df["high"].iloc[-21:-1].max()) if len(df) > 21 else c_now
            is_yang = c_now >= o_now
            vol_ratio = v_now / v_prev if v_prev > 0 else 0
            b2_break = bull and (c_now > yl_val > 0 or c_now > hh20) and is_yang and vol_ratio >= 1.5
            if b2_break:
                s += 3; n.append("B2:放量突破")
            # B3: J≥80 + 5日涨超8% + 多头环境(加速主升, 非追高)
            c_5ago = float(df["close"].iloc[-6])
            if bull and j >= 80 and c_now > c_5ago * 1.08 and v_now > float(vol.iloc[-20:].mean()):
                s += 3; n.append(f"B3:J{j:.0f}加速")
            if not n: n.append(f"J{j:.0f}")
            return s >= 3, s, ";".join(n)

        if group == "砖型组":
            brick = analyze_brick_patterns(df, None)
            if not isinstance(brick, dict):
                return False, 0, "砖型异常"
            bs = str(brick.get("状态", ""))
            if brick.get("绿翻强红"):
                return True, 6, "绿翻强红"
            if brick.get("今日翻红") or "红柱" in bs:
                return True, 3, "红柱:" + bs[:8]
            return False, 0, bs[:10]

        if group == "单针组":
            # 单针下20(回调买点): 白线(短期)≤20 & 红线(长期)≥CONFIG.NEEDLE_LONG_MIN → 大势多头短期恐慌=回调买点
            from utils.needle20 import needle20_lines
            lines = needle20_lines(df)
            s_ = float(lines["短期"].iloc[-1])
            l_ = float(lines["长期"].iloc[-1])
            if s_ <= 20 and l_ >= CONFIG.NEEDLE_LONG_MIN:
                return True, 4, f"单针下20:短{s_:.0f}≤20/长{l_:.0f}≥{CONFIG.NEEDLE_LONG_MIN}"
            if s_ <= 20:
                return False, 0, f"短{s_:.0f}≤20但长{l_:.0f}<{CONFIG.NEEDLE_LONG_MIN}(趋势弱)"
            return False, 0, f"短{s_:.0f}>20(未下20)"

        if group == "深V组":
            # 单针下30(洗盘补票): 短期线掉到30以下 + 长期线还在80以下 = 上升趋势洗盘日补票
            from utils.needle20 import needle20_lines
            lines = needle20_lines(df)
            s_ = float(lines["短期"].iloc[-1])
            l_ = float(lines["长期"].iloc[-1])
            if s_ <= 30 and l_ <= 80:
                return True, 4, f"单针下30:短{s_:.0f}≤30/长{l_:.0f}≤80"
            if s_ <= 30:
                return False, 0, f"短{s_:.0f}≤30但长{l_:.0f}>80(趋势过热)"
            return False, 0, f"短{s_:.0f}>30(未下30)"
    except Exception as e:
        return False, 0, f"指标异常:{str(e)[:30]}"
    return False, 0, "?"


def screen_by_code(top_n=TOP_N):
    """
    主入口: 全市场代码条件选股
    返回: {组名: [{代码, 名称, 最新价, 涨跌幅, score, note}]}
    """
    global TOP_N
    TOP_N = top_n
    df = get_market_snapshot()
    cands = rough_screen(df)
    result = {}
    for g in GROUPS:
        sub = cands.get(g)
        if sub is None or sub.empty:
            result[g] = []
            continue
        picks = []
        for _, r in sub.iterrows():
            code = r["纯代码"]
            ok, score, note = _verify_code(code, g)
            if ok:
                picks.append({
                    "代码": code, "名称": r["名称"],
                    "最新价": float(r["最新价"]), "涨跌幅": float(r["涨跌幅"]),
                    "score": score, "note": note,
                })
            time.sleep(random.uniform(0.2, 0.4))  # 限速
            if len(picks) >= 5:
                break
        picks.sort(key=lambda x: -x["score"])
        result[g] = picks
    return result


if __name__ == "__main__":
    import json
    res = screen_by_code()
    for g, picks in res.items():
        print(f"[{g}] {len(picks)}只")
        for p in picks[:3]:
            print(f"   {p['代码']} {p['名称']} {p['最新价']} (分{p['score']}:{p['note']})")
