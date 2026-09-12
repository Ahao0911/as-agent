"""
统一行情数据路由 get_daily_bars() — Ahao Stock Agent 唯一入口
Agent 不自行决定数据源，路由规则全部由程序控制

# 数据源分工（2026-08-12 晚更新，用户指定: a-stock-data → ftshare → 妙想MX → akshare/腾讯/mootdx，前两个免费好用）:
# - a-stock-data: 第0优先级（GitHub simonlin1212/a-stock-data V3.6.1，skill已装 ~/.workbuddy/skills/a-stock-data）
#   十层47端点: 行情(mootdx+腾讯+百度K线) / 研报(东财+同花顺+iwencai) / 信号(强势股+北向+龙虎榜+解禁+行业)
#   / 资金面(两融+大宗+股东户数+分红+资金流) / 新闻(东财+财联社) / 财务三表+F10 / 公告(巨潮)
#   / 打板(涨停池+连板+重点监控+日内异动) / ETF期权(T型+IV) / 舆情互动(互动易+热榜)
#   原则: mootdx/腾讯不封IP优先, 东财走内置em_get()限流防封, 主源被封查「备用源速查」降级
# - ftshare: 第1优先级（非凸科技免费，153子skill，限速10次/秒）
#   A股日线兜底 + 港股K线/财报 + 美股行情 + 宏观经济 + 指数权重 + 涨停跌停池 + 可转债 + 北向南向资金 + 新闻搜索
# - 妙想MX: 第2优先级（东财免费额度，优先新闻/美股/选股/财务查询，iFinD兜底）
# - akshare/腾讯/mootdx: 第3优先级兜底
#   - MooTdx: 全市场批量日线/分钟/指数（批量主力）
#   - 腾讯:   自选股/持仓/候选池日线（小池主力，限速+熔断）
#   - AKShare: 乐咕情绪/新浪板块/韩股KOSPI等特色数据
# - iFinD:  受控增强源（财务/估值/股东/问财/新闻；默认不用它拉普通K线，额度2000次）
#
# 路由流程:
#   本地缓存 → 按用途选源 → 质量检查 → 标准化返回
#   腾讯连续3次失败 → 熔断30分钟 → 自动切MooTdx → ftshare兜底
"""
import os
import sys
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from utils.kline_cache import save_klines, load_klines, last_trade_date
from utils.data_quality import validate_kline
from utils.stock_data import (_clear_proxy, _random_delay,
                              get_index_data, get_sector_data,
                              get_market_amount, get_market_sentiment)
from utils.ftshare_data import (
    get_ftshare_klines, get_ftshare_hk_klines, get_ftshare_hk_view,
    get_ftshare_us_latest, get_ftshare_us_basic,
    get_ftshare_china_cpi, get_ftshare_china_ppi, get_ftshare_china_pmi,
    get_ftshare_china_gdp, get_ftshare_china_money_supply, get_ftshare_china_lpr,
    get_ftshare_us_economic,
    get_ftshare_index_weight, get_ftshare_index_weight_list,
    get_ftshare_limit_up, get_ftshare_limit_down,
    get_ftshare_cb_list,
    get_ftshare_northbound, get_ftshare_southbound,
    get_ftshare_news, get_ftshare_concept_boards, get_ftshare_board_constituents,
    get_ftshare_index_klines, get_ftshare_market_extra,
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
THS_QUOTA_FILE = os.path.join(CACHE_DIR, "ths_quota.json")

# ===================== iFinD 额度控制 =====================

DEFAULT_QUOTA = 2000  # 试用额度


def get_quota():
    """读取iFinD额度"""
    if os.path.exists(THS_QUOTA_FILE):
        with open(THS_QUOTA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"total": DEFAULT_QUOTA, "used": 0, "remaining": DEFAULT_QUOTA,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def use_quota(cost=1):
    """消耗额度，返回是否允许"""
    q = get_quota()
    if q["remaining"] < cost:
        return False, f"iFinD额度不足（剩余{q['remaining']}）"
    q["used"] += cost
    q["remaining"] -= cost
    q["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(THS_QUOTA_FILE, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)
    return True, f"已用{q['used']}/{q['total']}，剩余{q['remaining']}"


def _symbol_to_tencent(code):
    """6位代码 → 腾讯symbol"""
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"


# ===================== 数据源实现 =====================

def _from_mootdx(code, bars=250):
    """MooTdx 批量/兜底源"""
    from utils.screen_bull import fetch_kline
    try:
        df = fetch_kline(code, bars=bars)
        if df is not None and len(df) > 0:
            return {"status": "OK", "df": df, "source": "mootdx",
                    "adjustment": "qfq", "volume_unit": "lot"}
    except Exception as e:
        pass
    return {"status": "NETWORK_ERROR", "msg": "mootdx失败"}


def _from_ftshare(code, bars=250):
    """ftshare A股日线（第1优先级免费源, daec v2 接口）"""
    try:
        r = get_ftshare_klines(code, limit=max(bars, 130))
        if r.get("status") == "OK" and r.get("value"):
            ohlcs = r["value"]
            import pandas as pd
            records = []
            for o in ohlcs:
                try:
                    # v2 转换后为标准字段; 兼容旧格式(close_ts_ms)
                    date = o.get("date") or (o["close_ts_ms"][:10] if o.get("close_ts_ms") and len(str(o["close_ts_ms"])) > 10 else "")
                    if not date:
                        continue
                    records.append({
                        "date": date,
                        "open": float(o.get("open", o.get("o"))),
                        "high": float(o.get("high", o.get("h"))),
                        "low": float(o.get("low", o.get("l"))),
                        "close": float(o.get("close", o.get("c"))),
                        "volume": int(o.get("volume", o.get("v", 0))),
                        "amount": float(o.get("amount", o.get("turnover", o.get("t", 0))) or 0),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            if len(records) >= min(bars, 10):
                df = pd.DataFrame(records)
                df.set_index("date", inplace=True)
                df.index = pd.to_datetime(df.index)
                return {"status": "OK", "df": df, "source": "ftshare",
                        "adjustment": "qfq", "volume_unit": "lot"}
    except Exception as e:
        pass
    return {"status": "NETWORK_ERROR", "msg": "ftshare失败"}


def _from_tencent(code, bars=250, adjustment="qfq"):
    """腾讯源"""
    from utils.tencent import get_tencent
    symbol = _symbol_to_tencent(code)
    return get_tencent().fetch(symbol, count=bars, adjustment=adjustment)


def _df_stale(df):
    """K线最后一根是否滞后于今天（交易日）"""
    try:
        if df is None or len(df) == 0:
            return True
        last = str(df.index[-1])[:10]
        today = datetime.now().strftime("%Y-%m-%d")
        return datetime.now().weekday() < 5 and last < today
    except Exception:
        return False


# ===================== 统一入口 =====================

def get_daily_bars(symbols, count=120, adjustment="qfq",
                   purpose="indicator", freshness="latest",
                   preferred_source=None, force_refresh=False):
    """
    统一行情入口（Agent唯一调用）
    symbols: 单个代码 或 列表
    count: K线数量
    adjustment: qfq(指标默认) / raw(成本对比)
    purpose: indicator(指标) / trade_review(操作复盘) / bulk(批量) / watch(自选)
    preferred_source: 默认None；仅用户明确要求时才传 "ifind" 或 "tencent"
    force_refresh: 强制重新拉取（跳过缓存）

    返回（单只）:
    {
      "status": "OK"|"EMPTY_DATA"|...,
      "source": "tencent"|"mootdx"|"ifind"|"cache",
      "fallback_used": bool,
      "adjustment": "qfq",
      "volume_unit": "lot",
      "amount_unit": "yuan",
      "as_of": "2026-07-31",
      "warnings": [],
      "data": [...]
    }
    """
    single = isinstance(symbols, str)
    codes = [symbols] if single else list(symbols)
    results = {}

    for code in codes:
        code = str(code).zfill(6)
        cache_key = f"{code}_day_{adjustment}"
        warnings = []
        df = None
        source = None
        fallback = False

        # 1. 本地缓存优先（除非强制刷新）
        cache_stale = False
        if not force_refresh:
            cached = load_klines(code, adjustment=adjustment, limit=count)
            if cached is not None and len(cached) >= min(count, 30):
                last_local = str(cached.index[-1])[:10]
                # 新鲜度检查：收盘后本地数据应该就是最新的
                today = datetime.now().strftime("%Y-%m-%d")
                is_trading_day = datetime.now().weekday() < 5
                if freshness == "latest" and is_trading_day and last_local == today:
                    df, source = cached, "cache"
                elif freshness != "latest":
                    df, source = cached, "cache"
                else:
                    # 本地旧了（最后交易日<今天）：不直接返回，走拉源增量更新
                    cache_stale = True
                    warnings.append(f"缓存最后交易日{last_local}<今天{today}，触发增量更新")

        # 2. 按用途选源
        if df is None:
            if purpose == "bulk":
                # 批量：MooTdx（腾讯/akshare 不适合高并发；ftshare 限速10次/秒也不适合批量）
                r = _from_mootdx(code, bars=count)
            elif purpose in ("watch", "trade_review", "indicator"):
                # 小池/单只：ftshare 优先(免费) → 腾讯 → mootdx 兜底
                r = _from_ftshare(code, bars=count)
                if r.get("status") != "OK":
                    fallback = True
                    r = _from_tencent(code, bars=count, adjustment=adjustment)
                if r.get("status") != "OK":
                    fallback = True
                    r = _from_mootdx(code, bars=count)
                elif _df_stale(r.get("df")):
                    # ftshare 当天K线滞后 → 腾讯 → mootdx 兜底
                    fallback = True
                    r2 = _from_tencent(code, bars=count, adjustment=adjustment)
                    if r2.get("status") == "OK":
                        r = r2
                    else:
                        r3 = _from_mootdx(code, bars=count)
                        if r3.get("status") == "OK":
                            r = r3
                        else:
                            warnings.append("ftshare K线滞后且腾讯/mootdx兜底均失败，沿用ftshare数据")
            else:
                # 默认：ftshare → 腾讯 → mootdx
                r = _from_ftshare(code, bars=count)
                if r.get("status") != "OK":
                    fallback = True
                    r = _from_tencent(code, bars=count, adjustment=adjustment)
                if r.get("status") != "OK":
                    fallback = True
                    r = _from_mootdx(code, bars=count)

            # iFinD 特殊请求（仅用户明确要求）
            if preferred_source == "ifind" or (purpose == "trade_review" and r.get("status") != "OK"):
                allowed, msg = use_quota()
                if allowed:
                    from utils.ths_mcp import summary
                    # iFinD 只做核验，返回标注
                    warnings.append("iFinD核验请求")
                    # 这里仅记录，主数据仍用腾讯/mootdx结果

            if r.get("status") == "OK" and r.get("df") is not None:
                df = r["df"]
                source = r.get("source", "unknown")
                # 3. 质量检查
                status, issues = validate_kline(df)
                if status != "OK":
                    results[code] = {"status": status, "warnings": issues}
                    continue
                # 4. 写缓存
                save_klines(code, df, adjustment=adjustment, source=source)
            else:
                if cache_stale and cached is not None:
                    # 拉源失败：回退旧缓存并标注（保证有数据可用）
                    df, source = cached, "cache"
                    warnings.append(f"拉源失败({r.get('status')})，回退缓存{str(cached.index[-1])[:10]}")
                else:
                    results[code] = {"status": r.get("status", "ERROR"),
                                     "msg": r.get("msg", ""), "warnings": warnings}
                    continue

        # 5. 标准化输出
        if df is not None:
            data = []
            for date, row in df.iterrows():
                data.append({
                    "date": str(date)[:10],
                    "open": round(float(row["open"]), 3),
                    "high": round(float(row["high"]), 3),
                    "low": round(float(row["low"]), 3),
                    "close": round(float(row["close"]), 3),
                    "volume": float(row.get("volume", 0)),
                    "amount": float(row["amount"]) if row.get("amount") is not None else None,
                })
            results[code] = {
                "status": "OK",
                "source": source,
                "fallback_used": fallback,
                "adjustment": adjustment,
                "volume_unit": "lot",
                "amount_unit": "yuan",
                "as_of": data[-1]["date"] if data else None,
                "warnings": warnings,
                "data": data,
            }

    return results[list(results.keys())[0]] if single else results


def get_kline_df(code, bars=250, adjustment="qfq"):
    """便捷函数：返回 (DataFrame, meta)，供现有指标模块直接使用"""
    r = get_daily_bars(code, count=bars, adjustment=adjustment)
    if r.get("status") == "OK":
        data = r["data"]
        df = pd.DataFrame(data).set_index("date")
        df.index = pd.to_datetime(df.index)
        return df, r
    return None, r


# ===================== 特色数据（AKShare，保持原有） =====================

def get_index_quotes():
    """指数行情：妙想mx优先（自然语言，权威库）→ mootdx+akshare 兜底"""
    try:
        from utils.mx_data import query_quote
        r = query_quote("上证指数 深证成指 创业板指 科创50 最新收盘 涨跌幅")
        if r.get("ok") and r.get("output"):
            return _meta({"text": r["output"], "raw_files": r.get("raw_files", [])}, "mx妙想")
    except Exception:
        pass
    return _meta(get_index_data(), "mootdx+akshare")


def get_sector_with_meta():
    return _meta(get_sector_data(), "akshare新浪")


def get_sentiment_with_meta():
    return _meta(get_market_sentiment(), "akshare乐咕")


def get_amount_with_meta():
    return _meta(get_market_amount(), "mootdx")


def get_market_value_with_meta():
    """活跃市值(0AmV): CSV历史数据优先(收盘后可靠) → mootdx兜底。含 图形买点体系择时信号"""
    try:
        from utils.market_value import get_active_market_value, get_market_value_series, trend_judge, amv_timing_signal
        mv = get_active_market_value()
        hist = get_market_value_series(30)
        level, sig = amv_timing_signal(mv.get("涨跌幅"))
        trend = trend_judge(mv["活跃市值"], hist)
        return _meta({
            "活跃市值": mv["活跃市值"],
            "日期": mv.get("日期"),
            "涨跌幅": mv.get("涨跌幅"),
            "择时级别": level,
            "择时信号": sig,
            "趋势": trend,
            "近30日序列": hist,
        }, mv.get("数据源", "CSV"))
    except Exception as e:
        return _meta(None, "未知", status="missing", error=str(e)[:100])


def get_kospi():
    """韩国KOSPI（首尔综合指数）：新浪全球指数"""
    try:
        import akshare as ak
        _clear_proxy()
        _random_delay()
        df = ak.index_global_hist_sina(symbol="首尔综合指数")
        if df is not None and len(df):
            last = df.iloc[-1]
            prev = df.iloc[-2]
            chg = (float(last["close"]) - float(prev["close"])) / float(prev["close"]) * 100
            return _meta({
                "收盘": round(float(last["close"]), 2),
                "涨跌幅": round(chg, 2),
                "日期": str(last["date"]),
            }, "akshare新浪")
    except Exception:
        pass
    return _meta(None, "未知", status="missing")


def get_us_market():
    """美股上一交易日：妙想mx优先（2026-08-03，东财免费额度）→ iFinD兜底（iFinD常返回脏数据）"""
    try:
        from utils.mx_data import query_quote
        r = query_quote("道琼斯指数 纳斯达克指数 标普500 最新收盘 涨跌幅")
        if r.get("ok") and r.get("output"):
            return _meta({"text": r["output"], "raw_files": r.get("raw_files", [])}, "mx妙想")
    except Exception:
        pass
    try:
        from utils.ths_mcp import call as ths_call
        r = ths_call("global_stock", "global_stock_quotes", {"query": "道琼斯指数 纳斯达克指数 标普500 最新收盘 涨跌幅"})
        if r.get("ok"):
            return _meta(r, "iFinD")
    except Exception:
        pass
    return _meta(None, "未知", status="missing")


def _meta(value, source, as_of=None, status="normal", fallback_used=False, **extra):
    return {
        "value": value, "source": source,
        "as_of": as_of or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status, "fallback_used": fallback_used, **extra,
    }


def get_news(keyword, days=7, size=5):
    """新闻：妙想mx优先（2026-08-03接入，不耗iFinD额度）→ iFinD兜底（受控额度）"""
    try:
        from utils.mx_data import query_news
        r = query_news(keyword)
        if r.get("ok") and r.get("output"):
            return {"status": "OK", "source": "mx妙想", "text": r["output"], "raw_files": r.get("raw_files", [])}
    except Exception:
        pass
    allowed, msg = use_quota()
    if not allowed:
        return {"status": "QUOTA_EXHAUSTED", "msg": msg}
    from utils.ths_mcp import news
    return news(keyword, days=days, size=size)


def get_mx_quote(symbol_query, kind="data"):
    """妙想增强查询（行情/财务/选股，自然语言），排在iFinD前
    返回: {"status": "OK"|"MX_UNAVAILABLE", "source": "mx妙想", "text": str, "raw_files": [...]}
    """
    try:
        from utils.mx_data import call
        r = call(kind, symbol_query)
        if r.get("ok") and r.get("output"):
            return {"status": "OK", "source": "mx妙想", "text": r["output"], "raw_files": r.get("raw_files", [])}
        return {"status": "MX_UNAVAILABLE", "msg": r.get("error", "妙想无结果")}
    except Exception as e:
        return {"status": "MX_UNAVAILABLE", "msg": str(e)[:100]}


def build_market_snapshot():
    """统一市场快照（一次抓取全模块复用）"""
    snapshot = {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "指数": get_index_quotes(),
        "板块": get_sector_with_meta(),
        "情绪": get_sentiment_with_meta(),
        "成交额": get_amount_with_meta(),
        "活跃市值": get_market_value_with_meta(),
        "美股": get_us_market(),
        "韩股": get_kospi(),
        "ftshare": get_ftshare_market_extra(),
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    cache_path = os.path.join(CACHE_DIR, f"{today}_snapshot.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
    return snapshot


def load_snapshot():
    today = datetime.now().strftime("%Y%m%d")
    cache_path = os.path.join(CACHE_DIR, f"{today}_snapshot.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="600519")
    parser.add_argument("--purpose", default="watch", choices=["watch", "bulk", "indicator", "trade_review"])
    parser.add_argument("--count", type=int, default=120)
    args = parser.parse_args()

    r = get_daily_bars(args.code, count=args.count, purpose=args.purpose)
    print(f"状态: {r.get('status')} | 源: {r.get('source')} | 复权: {r.get('adjustment')}")
    print(f"as_of: {r.get('as_of')} | fallback: {r.get('fallback_used')} | warnings: {r.get('warnings')}")
    if r.get("status") == "OK":
        print(f"数据: {len(r['data'])} 根, 最新收盘 {r['data'][-1]['close']}")

    # 显示缓存统计
    from utils.kline_cache import cache_stats
    stats = cache_stats()
    print(f"\n缓存: {sum(stats.values())} 根 / {len(stats)} 标的")
