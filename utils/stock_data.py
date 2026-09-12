import time
import json
import os
import random
from datetime import datetime
import pandas as pd
from mootdx.quotes import Quotes
import akshare as ak

# ===================== 基础配置 =====================
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(CACHE_DIR, exist_ok=True)

def _get_today_str():
    return datetime.now().strftime("%Y%m%d")

def _save_cache(data, filename):
    cache_path = os.path.join(CACHE_DIR, f"{_get_today_str()}_{filename}.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_cache(filename):
    cache_path = os.path.join(CACHE_DIR, f"{_get_today_str()}_{filename}.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def _random_delay(min_sec=0.8, max_sec=2.0):
    time.sleep(random.uniform(min_sec, max_sec))

def _retry_request(func, max_retries=3):
    for i in range(max_retries):
        try:
            return func()
        except Exception as e:
            if i == max_retries - 1:
                raise e
            time.sleep((i + 1) ** 2)
    return None

def _clear_proxy():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
              'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['NO_PROXY'] = '*'

# ===================== 四大核心指数 =====================
def get_index_data():
    """mootdx取上证/深证/创业板，akshare取科创50"""
    cache = _load_cache("index")
    if cache:
        return cache

    result = []
    try:
        _random_delay()
        client = Quotes.factory(market='std')
        raw = client.quotes(symbol=['1A0001', '399001', '399006'])
        for code, name in [('000001', '上证'), ('399001', '深证'), ('399006', '创业板')]:
            item = raw[raw['code'] == code].iloc[0]
            lc, p = float(item['last_close']), float(item['price'])
            h, lo = float(item['high']), float(item['low'])
            result.append({
                "名称": name, "昨收": round(lc, 2),
                "今开": round(float(item['open']), 2), "收盘": round(p, 2),
                "涨跌幅": round((p - lc) / lc * 100, 2),
                "最高": round(h, 2), "最低": round(lo, 2),
                "振幅": round((h - lo) / lc * 100, 2)
            })
    except Exception as e:
        print(f"[mootdx失败] {e}")

    try:
        # 科创50：腾讯实时接口最稳（mootdx指数映射错、akshare新浪滞后）
        import urllib.request
        req = urllib.request.Request("https://qt.gtimg.cn/q=sh000688",
                                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="ignore")
        parts = raw.split("~")
        if len(parts) < 35:
            raise ValueError("腾讯返回字段不足")
        lc = float(parts[4])   # 昨收
        p = float(parts[3])    # 最新/收盘
        o = float(parts[5])    # 今开
        h = float(parts[33]) if parts[33] else p   # 最高
        lo = float(parts[34]) if parts[34] else p  # 最低
        chg = float(parts[32]) if parts[32] else 0.0
        result.append({
            "名称": "科创50", "昨收": round(lc, 2),
            "今开": round(o, 2), "收盘": round(p, 2),
            "涨跌幅": round(chg, 2),
            "最高": round(h, 2), "最低": round(lo, 2),
            "振幅": round((h - lo) / lc * 100, 2)
        })
    except Exception as e:
        print(f"[科创50腾讯接口失败] {e}")

    if len(result) < 3:
        _random_delay()
        _clear_proxy()
        result = []
        for code, name in [("sh000001","上证"),("sz399001","深证"),("sz399006","创业板"),("sh000688","科创50")]:
            df = ak.stock_zh_index_daily(symbol=code)
            last, prev = df.iloc[-1], df.iloc[-2]
            lc, p = float(prev['close']), float(last['close'])
            result.append({
                "名称": name, "昨收": round(lc, 2),
                "今开": round(float(last['open']), 2), "收盘": round(p, 2),
                "涨跌幅": round((p - lc) / lc * 100, 2),
                "最高": round(float(last['high']), 2), "最低": round(float(last['low']), 2),
                "振幅": round((float(last['high']) - float(last['low'])) / lc * 100, 2)
            })

    _save_cache(result, "index")
    return result

# ===================== 板块数据（新浪行业+概念双口径） =====================
def get_sector_data():
    """
    行业板块 + 概念板块双口径
    数据源：新浪财经（不走东方财富push2）
    """
    cache = _load_cache("sector")
    if cache:
        return cache

    result = {}

    # 行业板块
    try:
        _random_delay()
        _clear_proxy()
        df = ak.stock_sector_spot(indicator='行业')
        df['涨跌幅'] = df['涨跌幅'].astype(float)
        df['总成交额'] = df['总成交额'].astype(float)
        df = df[df['总成交额'] > 1e8]

        up = df.sort_values('涨跌幅', ascending=False).head(10)
        down = df.sort_values('涨跌幅', ascending=True).head(8)

        result["行业涨幅"] = [{"板块": row['板块'], "涨幅": round(row['涨跌幅'], 2),
                           "成交额": round(row['总成交额'] / 1e8, 0)} for _, row in up.iterrows()]
        result["行业跌幅"] = [{"板块": row['板块'], "跌幅": round(row['涨跌幅'], 2),
                           "成交额": round(row['总成交额'] / 1e8, 0)} for _, row in down.iterrows()]
    except Exception as e:
        print(f"[行业板块失败] {e}")
        result["行业涨幅"] = []
        result["行业跌幅"] = []

    # 概念板块（捕捉半导体、AI等核心主线）
    try:
        _random_delay()
        _clear_proxy()
        df = ak.stock_sector_spot(indicator='概念')
        df['涨跌幅'] = df['涨跌幅'].astype(float)
        df['总成交额'] = df['总成交额'].astype(float)
        df = df[df['总成交额'] > 5e8]

        up = df.sort_values('涨跌幅', ascending=False).head(8)
        down = df.sort_values('涨跌幅', ascending=True).head(5)

        result["概念涨幅"] = [{"板块": row['板块'], "涨幅": round(row['涨跌幅'], 2),
                           "成交额": round(row['总成交额'] / 1e8, 0)} for _, row in up.iterrows()]
        result["概念跌幅"] = [{"板块": row['板块'], "跌幅": round(row['涨跌幅'], 2),
                           "成交额": round(row['总成交额'] / 1e8, 0)} for _, row in down.iterrows()]
    except Exception as e:
        print(f"[概念板块失败] {e}")
        result["概念涨幅"] = []
        result["概念跌幅"] = []

    _save_cache(result, "sector")
    return result

# ===================== 全市场成交额 =====================
def get_market_amount():
    """沪深两市成交额汇总。优先 mootdx,失败后 akshare 官方接口兜底(收盘后 mootdx 不返回快照)"""
    cache = _load_cache("amount")
    if cache:
        return cache

    result = None
    # 方案1: mootdx 通达信快照
    try:
        _random_delay()
        client = Quotes.factory(market='std')
        raw = client.quotes(symbol=['1A0001', '399001'])
        if raw is not None and len(raw) > 0 and 'code' in raw.columns:
            sh = float(raw[raw['code'] == '000001'].iloc[0]['amount'])
            sz = float(raw[raw['code'] == '399001'].iloc[0]['amount'])
            result = {
                "沪深合计": round((sh + sz) / 1e8, 2),
                "沪市": round(sh / 1e8, 2),
                "深市": round(sz / 1e8, 2)
            }
        else:
            print("[成交额] mootdx 返回空快照,改用 akshare 官方接口")
    except Exception as e:
        print(f"[成交额] mootdx 失败({e}),改用 akshare 官方接口")

    # 方案2: 腾讯指数成交额(收盘后稳定, 单位万元) — 2026-08-18 优先, 修复akshare深市异常
    if result is None:
        try:
            import requests
            r = requests.get("https://qt.gtimg.cn/q=s_sh000001,s_sz399001", timeout=10)
            r.encoding = "gbk"
            import re
            m = re.findall(r'v_s_s[hz]\d+="([^"]+)"', r.text)
            if len(m) >= 2:
                sh = float(m[0].split("~")[7]) / 1e4   # 万元 -> 亿
                sz = float(m[1].split("~")[7]) / 1e4
                if sh > 100 and sz > 100:               # 合理性校验(非空/非0)
                    result = {
                        "沪深合计": round(sh + sz, 2),
                        "沪市": round(sh, 2),
                        "深市": round(sz, 2)
                    }
                    print(f"[成交额] 腾讯指数: 沪 {sh}亿 深 {sz}亿")
        except Exception as e:
            print(f"[成交额] 腾讯接口失败({e})")

    # 方案3: akshare 交易所官方接口(上交所成交金额 单位亿 / 深交所成交金额 单位元)
    if result is None:
        try:
            import akshare as ak
            from datetime import datetime as _dt
            today = _dt.now().strftime("%Y%m%d")
            sse = ak.stock_sse_deal_daily(date=today)      # 上交所
            sh_row = sse[sse["单日情况"] == "成交金额"].iloc[0]
            sh = float(sh_row["股票"])                       # 单位: 亿元
            szse = ak.stock_szse_summary()                  # 深交所
            sz_row = szse[szse["证券类别"] == "股票"].iloc[0]
            sz = float(sz_row["成交金额"]) / 1e8             # 单位: 元 -> 亿
            result = {
                "沪深合计": round(sh + sz, 2),
                "沪市": round(sh, 2),
                "深市": round(sz, 2)
            }
            print(f"[成交额] akshare 官方: 沪 {sh}亿 深 {sz}亿")
        except Exception as e:
            print(f"[成交额失败] {e}")
            result = {"沪深合计": 0, "沪市": 0, "深市": 0}

    _save_cache(result, "amount")
    return result

# ===================== 市场情绪（涨跌家数+涨停跌停） =====================
def get_market_sentiment():
    """
    获取全市场涨跌家数、涨停跌停数
    数据源：乐咕市场概况 + 东方财富涨停池
    """
    cache = _load_cache("sentiment")
    if cache:
        return cache

    result = {
        "上涨家数": 0, "下跌家数": 0, "平盘": 0,
        "涨停": 0, "跌停": 0, "活跃度": 0
    }

    # 乐咕市场概况（涨跌家数）
    try:
        _random_delay()
        _clear_proxy()
        df = ak.stock_market_activity_legu()
        data = dict(zip(df['item'], df['value']))
        result["上涨家数"] = int(data.get("上涨", 0))
        result["下跌家数"] = int(data.get("下跌", 0))
        result["平盘"] = int(data.get("平盘", 0))
        result["涨停"] = int(data.get("涨停", 0))
        result["跌停"] = int(data.get("跌停", 0))
        val = str(data.get("活跃度", "0")).replace("%", "");
        result["活跃度"] = float(val)
    except Exception as e:
        print(f"[乐咕失败] {e}")

    # 东方财富涨停池（补充验证）
    try:
        _random_delay()
        _clear_proxy()
        today = datetime.now().strftime("%Y%m%d")
        df_zt = ak.stock_zt_pool_em(date=today)
        result["涨停"] = max(result["涨停"], len(df_zt))
    except Exception:
        pass

    try:
        _random_delay()
        _clear_proxy()
        today = datetime.now().strftime("%Y%m%d")
        df_dt = ak.stock_limit_down_pool_em(date=today)
        result["跌停"] = max(result["跌停"], len(df_dt))
    except Exception:
        pass

    _save_cache(result, "sentiment")
    return result

# ===================== 单只个股详情 =====================
def get_stock_detail(code):
    cache = _load_cache(f"stock_{code}")
    if cache:
        return cache

    try:
        _random_delay()
        client = Quotes.factory(market='std')
        raw = client.quotes(symbol=[code])
        item = raw.iloc[0]
        lc = float(item['last_close'])
        result = {
            "代码": code, "昨收": round(lc, 2),
            "今开": round(float(item['open']), 2),
            "现价": round(float(item['price']), 2),
            "涨跌幅": round((float(item['price']) - lc) / lc * 100, 2),
            "最高": round(float(item['high']), 2), "最低": round(float(item['low']), 2),
            "成交量": int(item['vol']),
            "成交额": round(float(item['amount']) / 1e8, 2)
        }
        _save_cache(result, f"stock_{code}")
        return result
    except Exception:
        pass

    _random_delay()
    _clear_proxy()
    df = ak.stock_zh_a_spot_em()
    item = df[df['代码'] == code].iloc[0]
    result = {
        "代码": code, "昨收": round(float(item['昨收']), 2),
        "今开": round(float(item['今开']), 2),
        "现价": round(float(item['最新价']), 2),
        "涨跌幅": round(float(item['涨跌幅']), 2),
        "最高": round(float(item['最高']), 2), "最低": round(float(item['最低']), 2),
        "成交量": int(item['成交量']),
        "成交额": round(float(item['成交额']) / 1e8, 2)
    }
    _save_cache(result, f"stock_{code}")
    return result
