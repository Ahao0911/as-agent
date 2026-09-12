"""
ftshare-market-data 集成模块
数据源：https://market.ft.tech/gateway
限速：未注册10次/秒，注册20次/秒
所有函数返回统一的 _meta() 格式，可直接被 data_router 使用
"""
import os
import sys
import json
import subprocess
from datetime import datetime

FTSHARE_HOME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data-sources-ftshare")
RUN_PY = os.path.join(FTSHARE_HOME, "run.py")


def _run(subskill, *args, timeout=15):
    """执行ftshare子skill，返回解析后的JSON"""
    if not os.path.exists(RUN_PY):
        return {"status": "NOT_INSTALLED", "msg": "ftshare-market-data 未安装"}
    cmd = [sys.executable, RUN_PY, subskill] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return {"status": "ERROR", "msg": r.stderr.strip() or r.stdout.strip()}
        if not r.stdout.strip():
            return {"status": "EMPTY", "msg": "空响应"}
        return {"status": "OK", "data": json.loads(r.stdout)}
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "msg": f"请求超时({timeout}s)"}
    except json.JSONDecodeError:
        return {"status": "PARSE_ERROR", "msg": r.stdout[:200] if r.stdout else "无输出"}
    except Exception as e:
        return {"status": "EXCEPTION", "msg": str(e)[:200]}


def _meta(value, source, status="normal", **extra):
    return {
        "value": value, "source": source,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status, **extra,
    }


# ===================== A股K线（兜底/增量源） =====================

def get_ftshare_klines(code, since_days=120, limit=None):
    """
    A股日K线（ftshare，第1优先级免费源）
    code: 6位代码，如 '600000'
    since_days: 取多少天前的数据(原始模式)
    limit: 指定则用 v2 模式返回最近 N 根(推荐, daec接口已迁移)
    返回: dict {status, source, data: [{}]}  字段: date/open/high/low/close/volume/amount
    """
    symbol = f"{code}.XSHG" if code.startswith(("6", "9")) else f"{code}.SZ"
    if limit:
        # v2 兼容模式: daec 统一接口, 返回最近 N 根
        r = _run("stock-ohlcs", "--symbol", symbol, "--compat", "v2",
                 "--span", "DAY1", "--limit", str(limit))
        if r.get("status") != "OK":
            return _meta(None, "ftshare", status="missing", msg=r.get("msg"))
        payload = r.get("data", {})
        ohlcs = payload.get("data", {}).get("ohlcs", []) or []
        if not ohlcs:
            return _meta(None, "ftshare", status="missing", msg="v2模式无K线数据")
        # 转换 v2 缩写字段 → 标准字段
        records = []
        for o in ohlcs:
            try:
                ts = o.get("ctm") or o.get("otm") or 0
                date = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
                records.append({
                    "date": date,
                    "open": float(o.get("o")),
                    "high": float(o.get("h")),
                    "low": float(o.get("l")),
                    "close": float(o.get("c")),
                    "volume": int(o.get("v", 0)),
                    "amount": float(o.get("t", 0)),
                    "close_ts_ms": str(ts),  # 兼容旧消费方
                    "turnover": float(o.get("t", 0)),
                })
            except (KeyError, ValueError, TypeError):
                continue
        if not records:
            return _meta(None, "ftshare", status="missing", msg="v2数据解析失败")
        records.sort(key=lambda x: x["date"])
        return _meta(records, "ftshare", status="OK")
    # 原始模式(daec迁移后可能返回空, 保留兜底)
    from datetime import timedelta
    since = (datetime.now() - timedelta(days=since_days)).strftime("%Y%m%d")
    r = _run("stock-ohlcs", "--symbol", symbol, "--since", since)
    if r.get("status") != "OK":
        return _meta(None, "ftshare", status="missing", msg=r.get("msg"))
    ohlcs = r.get("data", {}).get("ohlcs", [])
    if not ohlcs:
        return _meta(None, "ftshare", status="missing", msg="原始模式无K线数据")
    return _meta(ohlcs, "ftshare", status="OK")


# ===================== 港股 =====================

def get_ftshare_hk_klines(code, days=60):
    """
    港股K线
    code: 港股代码，如 '00700' 或 '00700.HK'
    """
    trade_code = code if ".HK" in code else f"{code}.HK"
    until = datetime.now().strftime("%Y-%m-%d")
    r = _run("hk-candlesticks", "--trade-code", trade_code,
             "--interval-unit", "day", "--until-date", until)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-hk", status="missing", msg=r.get("msg"))
    items = r.get("data", {}).get("items", [])
    if not items:
        return _meta(None, "ftshare-hk", status="missing", msg="无港股K线")
    return _meta(items, "ftshare-hk", status="OK")


def get_ftshare_hk_view(code):
    """港股概况"""
    trade_code = code if ".HK" in code else f"{code}.HK"
    r = _run("hk-view", "--code", trade_code)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-hk", status="missing")
    return _meta(r["data"], "ftshare-hk", status="OK")


# ===================== 美股 =====================

def get_ftshare_us_latest(stock_code):
    """美股最新行情（东财美股）"""
    r = _run("eastmoney-us-stock-latest-ohlc", "--stock_code", stock_code)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-us", status="missing")
    payload = r.get("data", {})
    inner = payload.get("data", {}) if isinstance(payload, dict) else {}
    records = inner.get("records", []) if isinstance(inner, dict) else []
    return _meta(records, "ftshare-us", status="OK" if records else "missing")


def get_ftshare_us_basic(stock_code):
    """美股基本信息"""
    r = _run("us-basic", "--stock_code", stock_code)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-us", status="missing")
    return _meta(r.get("data", {}).get("items", []), "ftshare-us", status="OK")


# ===================== 宏观经济 =====================

def get_ftshare_china_cpi():
    """中国CPI月度"""
    r = _run("economic-china-cpi-monthly")
    return _meta(r.get("data"), "ftshare-macro", status="OK" if r.get("status") == "OK" else "missing")


def get_ftshare_china_ppi():
    """中国PPI月度"""
    r = _run("economic-china-ppi-monthly")
    return _meta(r.get("data"), "ftshare-macro", status="OK" if r.get("status") == "OK" else "missing")


def get_ftshare_china_pmi():
    """中国PMI月度"""
    r = _run("economic-china-pmi-monthly")
    return _meta(r.get("data"), "ftshare-macro", status="OK" if r.get("status") == "OK" else "missing")


def get_ftshare_china_gdp():
    """中国GDP季度"""
    r = _run("economic-china-gdp-quarterly")
    return _meta(r.get("data"), "ftshare-macro", status="OK" if r.get("status") == "OK" else "missing")


def get_ftshare_china_money_supply():
    """中国M2货币供应"""
    r = _run("economic-china-money-supply-monthly")
    return _meta(r.get("data"), "ftshare-macro", status="OK" if r.get("status") == "OK" else "missing")


def get_ftshare_china_lpr():
    """LPR利率"""
    r = _run("economic-china-lpr-monthly")
    return _meta(r.get("data"), "ftshare-macro", status="OK" if r.get("status") == "OK" else "missing")


def get_ftshare_us_economic(econ_type="nonfarm-payroll"):
    """美国经济数据
    econ_type: nonfarm-payroll(非农) / cpi / gdp / retail-sales / industrial-production / ...
    """
    r = _run("economic-us-economic-by-type", "--type", econ_type)
    return _meta(r.get("data"), "ftshare-macro", status="OK" if r.get("status") == "OK" else "missing")


# ===================== 指数权重 =====================

def get_ftshare_index_weight(index_code="000300"):
    """指数权重概况，如沪深300=000300"""
    r = _run("index-weight-summary", "--index-code", index_code, "--page", "1", "--page-size", "50")
    if r.get("status") != "OK":
        return _meta(None, "ftshare-index", status="missing")
    return _meta(r.get("data", {}).get("index_weights", []), "ftshare-index", status="OK")


def get_ftshare_index_weight_list(index_code="000300"):
    """指数成份权重明细"""
    r = _run("index-weight-list", "--index-code", index_code, "--page", "1", "--page-size", "50")
    if r.get("status") != "OK":
        return _meta(None, "ftshare-index", status="missing")
    return _meta(r.get("data", {}).get("index_weights", []), "ftshare-index", status="OK")


# ===================== 涨停/跌停池 =====================

def get_ftshare_limit_up(trade_date=None):
    """涨停池"""
    args = ["--trade-date", trade_date] if trade_date else []
    r = _run("limit-up-pool", *args)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-limit", status="missing")
    data = r["data"]
    if isinstance(data, list):
        return _meta(data, "ftshare-limit", status="OK")
    return _meta(None, "ftshare-limit", status="missing")


def get_ftshare_limit_down(trade_date=None):
    """跌停池"""
    args = ["--trade-date", trade_date] if trade_date else []
    r = _run("limit-down-pool", *args)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-limit", status="missing")
    data = r["data"]
    if isinstance(data, list):
        return _meta(data, "ftshare-limit", status="OK")
    return _meta(None, "ftshare-limit", status="missing")


# ===================== 可转债 =====================

def get_ftshare_cb_list():
    """可转债列表"""
    r = _run("cb-lists")
    if r.get("status") != "OK":
        return _meta(None, "ftshare-cb", status="missing")
    items = r.get("data", {}).get("items", r.get("data", []))
    if isinstance(items, list):
        return _meta(items, "ftshare-cb", status="OK")
    return _meta(None, "ftshare-cb", status="missing")


# ===================== 北向/南向资金 =====================

def get_ftshare_northbound(date=None):
    """北向资金（单日）"""
    date = date or datetime.now().strftime("%Y%m%d")
    r = _run("northbound", "--date", date)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-nb", status="missing")
    payload = r.get("data", {})
    inner = payload.get("data", {}) if isinstance(payload, dict) else {}
    if inner.get("channels"):
        return _meta(inner, "ftshare-nb", status="OK")
    return _meta(None, "ftshare-nb", status="missing")


def get_ftshare_southbound(date=None):
    """南向资金（单日）"""
    date = date or datetime.now().strftime("%Y%m%d")
    r = _run("southbound", "--date", date)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-sb", status="missing")
    payload = r.get("data", {})
    inner = payload.get("data", {}) if isinstance(payload, dict) else {}
    if inner.get("channels"):
        return _meta(inner, "ftshare-sb", status="OK")
    return _meta(None, "ftshare-sb", status="missing")


# ===================== 新闻搜索 =====================

def get_ftshare_news(query, limit=5):
    """语义搜索新闻（妙想兜底）"""
    r = _run("semantic-search-news", "--query", query)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-news", status="missing")
    data = r["data"]
    if isinstance(data, list):
        return _meta(data[:limit], "ftshare-news", status="OK")
    return _meta(None, "ftshare-news", status="missing")


# ===================== 板块 =====================

def get_ftshare_concept_boards():
    """东财概念板块列表"""
    r = _run("eastmoney-concept-boards")
    if r.get("status") != "OK":
        return _meta(None, "ftshare-board", status="missing")
    if isinstance(r.get("data"), list):
        return _meta(r["data"], "ftshare-board", status="OK")
    return _meta(None, "ftshare-board", status="missing")


def get_ftshare_board_constituents(board_type="concept", board_name=""):
    """板块成份股"""
    r = _run("eastmoney-board-constituents", "--board-type", board_type, "--board-name", board_name)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-board", status="missing")
    return _meta(r.get("data"), "ftshare-board", status="OK")


# ===================== 指数K线 =====================

def get_ftshare_index_klines(index_code, since_days=30):
    """指数K线，如 000300.XSHG（沪深300）"""
    from datetime import timedelta
    since = (datetime.now() - timedelta(days=since_days)).strftime("%Y%m%d")
    r = _run("index-ohlcs", "--index", index_code, "--since", since)
    if r.get("status") != "OK":
        return _meta(None, "ftshare-index", status="missing")
    ohlcs = r.get("data", {}).get("ohlcs", [])
    return _meta(ohlcs, "ftshare-index", status="OK" if ohlcs else "missing")


# ===================== 市场快照增强 =====================

def get_ftshare_market_extra():
    """ftshare特色数据：宏观经济+涨停+北向+指数权重"""
    results = {}
    # 宏观经济（精简版）
    for name, func in [("CPI", get_ftshare_china_cpi),
                        ("PMI", get_ftshare_china_pmi),
                        ("M2", get_ftshare_china_money_supply)]:
        try:
            r = func()
            if r.get("status") == "OK":
                results[name] = r
        except Exception:
            pass
    # 涨停池
    try:
        r = get_ftshare_limit_up()
        if r.get("status") == "OK":
            results["涨停池"] = r
    except Exception:
        pass
    # 北向资金
    try:
        r = get_ftshare_northbound()
        if r.get("status") == "OK":
            results["北向资金"] = r
    except Exception:
        pass
    return results