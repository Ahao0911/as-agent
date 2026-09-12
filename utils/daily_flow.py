"""
每日复盘完整工作流 — 黄白线作为最后一道过滤网（必要条件）
流程: 大盘 → 主线板块 → 板块成分候选 → 黄白线过滤 → 可做池

核心逻辑（图形买点体系）:
- 白线在黄线之上 = 多头区间（必要条件，只做这种）
- 黄白线过滤之前，先用"更厉害的条件"缩小范围:
  1. 大盘择时（活跃市值/指数趋势）
  2. 主线板块（涨幅+成交额）
  3. 板块内强势股（领涨）
- 最后用黄白线砍掉空头区间的票

用法:
    python3 utils/daily_flow.py                  # 全流程
    python3 utils/daily_flow.py --top 5          # 取涨幅前5的板块
    python3 utils/daily_flow.py --pool 600519 300750  # 自定义候选池，只做黄白线过滤
"""
import sys
import os
import time
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from utils.stock_data import (get_index_data, get_sector_data,
                              get_market_amount, get_market_sentiment,
                              _clear_proxy, _random_delay)


# ===================== 1. 大盘择时 =====================
def step1_market():
    """大盘数据 + 简单择时判断"""
    idx = get_index_data()
    amt = get_market_amount()
    sent = get_market_sentiment()
    return {"指数": idx, "成交额": amt, "情绪": sent}


# ===================== 2. 主线板块 + 领涨股候选 =====================
def step2_sectors(top_n=5):
    """
    提取主线板块，并直接拿到每板块领涨股（新浪接口自带）
    完全符合图形买点体系"只买最强"理念：主线板块的领涨股 = 候选
    """
    import akshare as ak
    _random_delay()
    _clear_proxy()
    try:
        df = ak.stock_sector_spot(indicator='概念')
        df['涨跌幅'] = df['涨跌幅'].astype(float)
        df['总成交额'] = df['总成交额'].astype(float)
        df = df[df['总成交额'] > 5e8]  # 只留活跃概念板块
        df = df.sort_values('涨跌幅', ascending=False)

        concepts = []
        for _, row in df.head(top_n).iterrows():
            concepts.append({
                "板块": row['板块'],
                "涨幅": round(row['涨跌幅'], 2),
                "成交额": round(row['总成交额'] / 1e8, 0),
                "领涨股代码": str(row['股票代码']).replace('sh', '').replace('sz', '').replace('bj', '')[-6:],
                "领涨股名称": row['股票名称'],
                "领涨股涨幅": round(row['个股-涨跌幅'], 2)
            })
        return concepts
    except Exception as e:
        print(f"[板块失败] {e}")
        return []


# ===================== 3. 候选池（领涨股方案） =====================
def step3_candidates(concepts, max_per_block=None):
    """
    主线板块领涨股 -> 候选池
    图形买点体系逻辑：只买最强，板块内最强的就是领涨股
    """
    candidates = []
    seen = set()
    for c in concepts:
        code = c.get("领涨股代码")
        if code and code.isdigit() and len(code) == 6 and code not in seen:
            candidates.append(code)
            seen.add(code)
    return candidates, concepts


# ===================== 4. 黄白线过滤（必要条件） =====================
def step4_dual_line_filter(codes):
    """对候选池逐只算黄白线，保留多头区间票"""
    from utils.indicators import analyze_dual_line

    bulls, bears, errors = [], [], []
    for code in codes:
        df = fetch_kline(code)
        if df is None:
            errors.append((code, "无数据"))
            continue
        try:
            r = analyze_dual_line(df)
            if "error" in r:
                errors.append((code, r["error"]))
                continue
            item = {"代码": code, "收盘": r["收盘"], "白线": r["白线"],
                    "黄线": r["黄线"], "J值": r["J值"],
                    "偏离度": round((r["收盘"] - r["黄线"]) / r["黄线"] * 100, 2)}
            if r["多头区间"]:
                bulls.append(item)
            else:
                bears.append(item)
        except Exception as e:
            errors.append((code, str(e)[:50]))
        time.sleep(random.uniform(0.2, 0.5))

    bulls.sort(key=lambda x: x["偏离度"], reverse=True)
    bears.sort(key=lambda x: x["偏离度"], reverse=True)
    return bulls, bears, errors


# ===================== K线获取（复用 screen_bull） =====================
def fetch_kline(code):
    """拉取日线K线（akshare优先，mootdx兜底）"""
    import akshare as ak

    code = str(code).zfill(6)
    try:
        _random_delay()
        _clear_proxy()
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date="20250101", end_date="20261231", adjust="qfq")
        if df is not None and len(df) > 30:
            df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                    "最高": "high", "最低": "low", "成交量": "volume"})
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")[["open", "high", "low", "close", "volume"]]
    except Exception:
        pass

    # 兜底1: akshare 新浪日线（东财接口不通时使用）
    try:
        _random_delay()
        prefix = "sh" if code.startswith(("5", "6", "9")) else ("bj" if code.startswith(("4", "8")) else "sz")
        df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}",
                                 start_date="20250101", end_date="20261231", adjust="qfq")
        if df is not None and len(df) > 30:
            df = df.rename(columns={"date": "date", "open": "open", "close": "close",
                                    "high": "high", "low": "low", "volume": "volume"})
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")[["open", "high", "low", "close", "volume"]]
    except Exception:
        pass

    # 兜底2: 腾讯日K线(股票/ETF通用,免费)
    try:
        import requests
        import random
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,250,qfq"
        _random_delay()
        r = requests.get(url, timeout=15)
        d = r.json()
        kdata = d.get("data", {}).get(f"{prefix}{code}", {})
        klines = kdata.get("qfqday") or kdata.get("day") or []
        rows = []
        for k in klines:
            rows.append({"date": pd.to_datetime(k[0]), "open": float(k[1]), "close": float(k[2]),
                         "high": float(k[3]), "low": float(k[4]), "volume": float(k[5])})
        if len(rows) > 30:
            return pd.DataFrame(rows).set_index("date")[["open", "high", "low", "close", "volume"]]
    except Exception:
        pass

    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std')
        df = client.bars(symbol=code, frequency=9, offset=250)
        df = df.rename(columns={"datetime": "date", "open": "open", "high": "high",
                                "low": "low", "close": "close", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"])
        keep = {}
        for col in ["date", "open", "high", "low", "close", "volume"]:
            if col in df.columns:
                keep[col] = df[col] if col != "volume" else (df[col].iloc[:, 0] if isinstance(df[col], pd.DataFrame) else df[col])
        return pd.DataFrame(keep).set_index("date")
    except Exception as e:
        print(f"[{code} mootdx失败] {e}")
    return None


# ===================== 主流程 =====================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="每日复盘完整工作流")
    parser.add_argument("--top", type=int, default=5, help="主线板块数量")
    parser.add_argument("--pool", nargs="*", help="自定义候选池（跳过板块选股，直接黄白线过滤）")
    args = parser.parse_args()

    print("═" * 60)
    print("图形买点体系 · 每日复盘工作流")
    print("═" * 60)

    # Step 1: 大盘
    print("\n▸ Step 1/4 大盘择时 ...")
    market = step1_market()
    idx_text = "  ".join(f"{d['名称']}{d['涨跌幅']:+.2f}%" for d in market["指数"])
    amt = market["成交额"]
    sent = market["情绪"]
    up_ratio = sent["上涨家数"] / max(1, sent["上涨家数"] + sent["下跌家数"]) * 100
    print(f"  {idx_text}")
    print(f"  成交额: {amt['沪深合计']:.0f}亿  涨跌: {sent['上涨家数']}/{sent['下跌家数']}  涨停: {sent['涨停']}")

    # Step 2: 主线板块
    if args.pool:
        print("\n▸ Step 2/4 使用自定义候选池（跳过板块选股）")
        candidates_list = [c.zfill(6) for c in args.pool]
    else:
        print("\n▸ Step 2/4 提取主线板块 + 领涨股 ...")
        concepts = step2_sectors(top_n=args.top)
        if not concepts:
            print("  ⚠ 无主线板块数据，使用内置股票池")
            from utils.screen_bull import DEFAULT_POOL
            candidates_list = DEFAULT_POOL
        else:
            print("  主线板块及领涨股:")
            for c in concepts:
                print(f"    ▸ {c['板块']}  +{c['涨幅']}%  ({c['成交额']:.0f}亿)  领涨: {c['领涨股名称']}({c['领涨股代码']}) +{c['领涨股涨幅']}%")
            candidates_list, _ = step3_candidates(concepts)

    print(f"  候选股: {len(candidates_list)} 只")

    # Step 3: 黄白线过滤（必要条件）
    print("\n▸ Step 3/4 黄白线过滤（只做多头区间）...")
    bulls, bears, errors = step4_dual_line_filter(candidates_list)

    # Step 4: 输出
    print("\n" + "═" * 60)
    print(f"★ 可做池（多头区间，白线在黄线上）: {len(bulls)} 只")
    print("═" * 60)
    if bulls:
        print(f"{'代码':<8}{'收盘':>8}{'白线':>8}{'黄线':>8}{'J值':>7}{'偏离黄线%':>9}")
        for b in bulls:
            print(f"{b['代码']:<8}{b['收盘']:>8.2f}{b['白线']:>8.2f}{b['黄线']:>8.2f}{b['J值']:>7.1f}{b['偏离度']:>9.2f}")
    else:
        print("  (无符合条件标的)")

    print()
    print("═" * 60)
    print(f"✗ 过滤掉（空头区间）: {len(bears)} 只")
    print("═" * 60)
    if bears:
        print("  " + "  ".join(f"{b['代码']}({b['偏离度']:+.1f}%)" for b in bears[:10]))

    if errors:
        print(f"\n⚠ 失败: {len(errors)} 只")
        print("  " + "  ".join(f"{c}" for c, _ in errors[:10]))


if __name__ == "__main__":
    main()
