"""
周一/每日复盘快速入口 — 持仓检查 + 自选池跟踪 + 大盘环境
基于 data/watchlist.json 的自选池和持仓（指标使用边界已遵守：个股用黄白线+砖型图，ETF用均线20/60+量能，板块用均线，大盘看活跃市值）

用法:
    python3 utils/daily_review.py          # 完整跟踪
    python3 utils/daily_review.py --code 600276   # 单票快速检查
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.data_router import get_kline_df, get_sentiment_with_meta
from utils.indicators import analyze_dual_line
from utils.brick import brick_signal
from utils.needle20 import needle20_signal

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# 流通股本缓存 {code: 股本}，避免重复请求 finance
_LIUTONG_CACHE = {}


def get_liutong(code):
    """流通股本(股)：mootdx finance（单代码），带缓存"""
    if code in _LIUTONG_CACHE:
        return _LIUTONG_CACHE[code]
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std')
        fin = client.finance(symbol=code)
        lt = None
        if fin is not None:
            for col in ('liutongguben', '流通股本'):
                if col in fin.columns:
                    lt = float(fin[col].iloc[0])
                    break
        _LIUTONG_CACHE[code] = lt
        return lt
    except Exception:
        _LIUTONG_CACHE[code] = None
        return None


def turnover_verdict(code, df):
    """换手率档位判定（图形买点体系规则）：>15%超高换手不做；7-15%高换手谨慎；3-7%黄金区间；<3%冷清
    返回 (换手率dict or None, 是否可做, 降级原因 or None)
    ⚠ 防御：缓存volume单位不统一（688012类=股、600667类=手），换手率>100%自动按"股"重算"""
    lt = get_liutong(code)
    if not lt:
        return None, True, None
    try:
        from utils.turnover import turnover_signal
        t = turnover_signal(df, liutong_guben=lt)
        if "error" in t:
            return t, True, None
        cur = t["当前换手率"]
        if cur > 100:
            # volume实际是"股"而非"手"：重算（股/流通股本）
            vol_share = df["volume"].astype(float).iloc[-1]
            cur = vol_share / lt * 100
            t = dict(t)
            t["当前换手率"] = round(cur, 2)
            # 同步更新信号列表里的档位描述
            new_signals = []
            for s in t.get("信号", []):
                if "换手" in s and "%" in s:
                    new_signals.append(f"{'超高' if cur > 15 else '高' if cur >= 7 else '温和' if cur >= 3 else '低'}换手({cur:.1f}%)")
                else:
                    new_signals.append(s)
            t["信号"] = new_signals
        if cur > 15:
            return t, False, f"✗ 超高换手({cur:.1f}%>15%)，换手太夸张不做"
        if cur >= 7:
            return t, True, f"⚠ 高换手({cur:.1f}%)，分歧加大谨慎"
        return t, True, None
    except Exception:
        return None, True, None


def is_etf(code):
    """ETF代码判断（5/15/56/58 开头；ETF本质=板块载体，不用黄白线+砖型图）"""
    return str(code).startswith(("5", "15", "56", "58"))


def check_etf(code, name=""):
    """ETF标的检查（均线20/60+量能，与板块规则一致；不用黄白线+红绿砖）"""
    df, meta = get_kline_df(code, bars=120)
    if df is None or len(df) < 60:
        return {"代码": code, "名称": name, "错误": "数据不足"}
    close = df['close'].iloc[-1]
    ma20 = df['close'].rolling(20).mean().iloc[-1]
    ma60 = df['close'].rolling(60).mean().iloc[-1]
    vol = df['volume'].iloc[-1]
    vol_ma20 = df['volume'].iloc[-21:-1].mean()
    vol_ratio = vol / vol_ma20 if vol_ma20 else 1.0
    up = bool(df['close'].iloc[-1] >= df['open'].iloc[-1])
    dev20 = (close - ma20) / ma20 * 100 if ma20 else 0
    bull = bool(ma20 > ma60)  # 均线多头 = MA20在MA60上方

    # 量能状态
    if vol_ratio >= 1.5:
        vol_state = "放量"
    elif vol_ratio <= 0.7:
        vol_state = "缩量"
    else:
        vol_state = "平量"

    # 结论分级（均线+量能，贴近板块规则）
    if bull and up and vol_ratio >= 1.2 and -3 < dev20 < 5:
        concl = "★★ 均线多头+放量上攻（贴近MA20，可关注）"
    elif bull and up:
        concl = "★ 均线多头+收阳（位置偏高，谨慎）"
    elif bull:
        concl = "○ 均线多头（等回踩MA20）"
    else:
        concl = "✗ 均线空头（MA20<MA60，规避）"

    return {
        "代码": code, "名称": name, "类型": "ETF",
        "收盘": float(close), "MA20": float(ma20), "MA60": float(ma60),
        "偏离MA20": float(round(dev20, 1)), "均线多头": bull,
        "量能比": float(round(vol_ratio, 2)), "量能": vol_state,
        "收阳": up, "结论": concl,
    }


def check_stock(code, name=""):
    """单票标的级检查：个股用黄白线+砖型图+单针；ETF用均线20/60+量能（图形买点体系不适用于ETF）"""
    if is_etf(code):
        return check_etf(code, name)
    df, meta = get_kline_df(code, bars=120)
    if df is None or len(df) < 60:
        return {"代码": code, "名称": name, "错误": "数据不足"}
    dual = analyze_dual_line(df)
    brick = brick_signal(df, dual)
    needle = needle20_signal(df, dual)

    close = df['close'].iloc[-1]
    yl = dual.get('黄线', 0)
    wl = dual.get('白线', 0)
    dev = (close - yl) / yl * 100 if yl else 0
    bull = dual['多头区间']
    brick_s = brick['状态']
    brick_ok = brick.get('绿翻强红') or brick.get('今日翻红')

    # 结论分级（先看换手率硬过滤：>15%超高换手直接不做）
    t, t_ok, t_why = turnover_verdict(code, df)
    if not t_ok:
        concl = t_why
    elif bull and brick_ok and -5 < dev < 5:
        concl = "★★ 买点信号（多头+翻红+贴近黄线）"
    elif bull and brick_ok:
        concl = "★ 多头+翻红（位置偏高，谨慎）"
    elif bull and "绿柱" not in brick_s:
        concl = "○ 多头持有（等回踩）"
    elif bull:
        concl = "△ 多头但砖型绿柱（等翻红）"
    else:
        concl = "✗ 空头区间（禁止开仓）"

    return {
        "代码": code, "名称": name, "类型": "个股",
        "收盘": float(close), "白线": float(wl), "黄线": float(yl),
        "偏离黄线": round(dev, 1), "J值": dual.get("J值"),
        "多头": bull, "砖型": brick_s, "翻红": brick_ok,
        "单针短期": needle.get("短期"),
        "换手率": round(t["当前换手率"], 2) if t and "当前换手率" in t else None,
        "换手档位": t["信号"][0] if t and t.get("信号") else None,
        "换手信号": t.get("信号") if t and "信号" in t else None,
        "结论": concl,
    }


def check_holdings(holdings):
    """持仓检查：对照止损位"""
    print("\n═══ 持仓检查 ═══")
    for h in holdings:
        r = check_stock(h["代码"], h["名称"])
        if "错误" in r:
            print(f"  {h['名称']}: {r['错误']}")
            continue
        stop = h.get("止损", 0)
        price = r["收盘"]
        hit = "⚠已破止损!" if stop and price < stop else "正常"
        if r.get("类型") == "ETF":
            print(f"  {h['名称']}({h['代码']}): 收盘{price:.3f} 止损{stop} {hit}")
            print(f"    MA20={r['MA20']:.3f} MA60={r['MA60']:.3f} 均线多头={'✓' if r['均线多头'] else '✗'} "
                  f"量能={r['量能']}({r['量能比']}x) 偏离MA20={r['偏离MA20']:+.1f}%")
        else:
            print(f"  {h['名称']}({h['代码']}): 收盘{price:.3f} 止损{stop} {hit}")
            print(f"    白线{r['白线']:.3f} 黄线{r['黄线']:.3f} 多头={'✓' if r['多头'] else '✗'} "
                  f"砖型={r['砖型']} 翻红={'✓' if r['翻红'] else '-'}")
            if r.get("换手率") is not None:
                print(f"    换手率={r['换手率']}% {r['换手档位'] or ''}")
        print(f"    {r['结论']}")


def check_watchlist(watchlist):
    """自选池跟踪"""
    print("\n═══ 自选池跟踪 ═══")
    for w in watchlist:
        r = check_stock(w["代码"], w["名称"])
        if "错误" in r:
            print(f"  {w['名称']}: {r['错误']}")
            continue
        if r.get("类型") == "ETF":
            print(f"  {w['名称']}({w['代码']}): 收盘{r['收盘']:.2f} 偏离MA20{r['偏离MA20']:+.1f}% "
                  f"量能={r['量能']}({r['量能比']}x) 均线多头={'✓' if r['均线多头'] else '-'}")
        else:
            print(f"  {w['名称']}({w['代码']}): 收盘{r['收盘']:.2f} 偏离黄线{r['偏离黄线']:+.1f}% "
                  f"J={r['J值']:.0f} 砖型={r['砖型']} 翻红={'✓' if r['翻红'] else '-'}")
            if r.get("换手率") is not None:
                print(f"    换手率={r['换手率']}% {r['换手档位'] or ''}")
        print(f"    {r['结论']}")


def check_kospi_live():
    """韩国KOSPI实时（早盘9:00开盘，比A股早25分钟，科技风向标）"""
    try:
        import requests
        h = {"Referer": "https://finance.sina.com.cn/"}
        r = requests.get("https://hq.sinajs.cn/list=znb_KOSPI", headers=h, timeout=8)
        r.encoding = "gbk"
        text = r.text
        if "znb_KOSPI" not in text:
            return "KOSPI实时数据缺失"
        # var hq_str_znb_KOSPI="名称,现价,涨跌额,涨跌幅%,时间,ts,日期,北京时间,最低,昨收,今开,..."
        parts = text.split('"')[1].split(",")
        name, price, chg, pct = parts[0], float(parts[1]), float(parts[2]), float(parts[3])
        prev_close = float(parts[9]) if len(parts) > 9 else 0
        low = float(parts[8]) if len(parts) > 8 else 0
        arrow = "▲" if pct >= 0 else "▼"
        verdict = ""
        if pct <= -2:
            verdict = " → 科技承压，A股半导体/科创开盘防低开"
        elif pct >= 2:
            verdict = " → 科技偏强，A股科技情绪加分"
        return (f"韩国KOSPI(实时): {name} {price:.2f} {arrow}{pct:+.2f}% "
                f"(昨收{prev_close:.2f} 今开{parts[10] if len(parts) > 10 else '-'} 低{low:.2f}){verdict}")
    except Exception as e:
        return f"KOSPI实时获取失败: {str(e)[:60]}"


def main():
    watch_path = os.path.join(DATA_DIR, "watchlist.json")
    if not os.path.exists(watch_path):
        print("未找到 watchlist.json，请先生成自选池")
        return
    with open(watch_path, "r", encoding="utf-8") as f:
        wl = json.load(f)

    args = sys.argv[1:]
    if args and args[0] == "--code" and len(args) > 1:
        r = check_stock(args[1])
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return

    # 韩国KOSPI实时（早盘科技风向标，比A股早开盘）
    print("═══ 外盘风向 ═══")
    print(f"  {check_kospi_live()}")
    print()

    # 大盘环境（活跃市值为主）
    print("═══ 大盘环境 ═══")
    sent = get_sentiment_with_meta()
    try:
        s = sent["value"]
        up, down = s["上涨家数"], s["下跌家数"]
        width = up / max(1, up + down) * 100
        print(f"  涨跌家数: {up}/{down} 宽度{width:.0f}% 涨停{s['涨停']} 活跃度{s['活跃度']}%")
        if width > 65:
            print("  → 普涨环境，可积极")
        elif width > 45:
            print("  → 结构行情，精选")
        else:
            print("  → 弱势环境，防守")
    except Exception:
        print("  情绪数据缺失")

    check_holdings(wl.get("持仓", []))
    check_watchlist(wl.get("自选池", []))

    print("\n═══ 周一观察要点 ═══")
    for p in wl.get("周一观察要点", []):
        print(f"  ☐ {p}")


if __name__ == "__main__":
    main()
