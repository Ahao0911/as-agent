"""
活跃市值战法 — 大盘择时/仓位管理工具
活跃市值(0AmV) = 全市场 Σ(成交量 × 股价)，衡量真实参与交易的活跃资金体量

数据源(2026-08-10 接入用户提供的历史CSV): data/active_market_value.csv (1993~今 日线OHLCV)
- 优先读 CSV(收盘后可用,含今日收盘值)
- mootdx 全市场快照 作为盘中实时补充
用法:
    python3 utils/market_value.py               # 输出今日活跃市值 + 趋势判断 + 择时信号
"""
import sys
import os
import time
import json
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CSV_PATH = os.path.join(CACHE_DIR, "active_market_value.csv")


# ===================== CSV 历史数据源(主) =====================

def load_csv_history():
    """从 CSV 加载活跃市值日线历史, 返回 DataFrame(date升序, 最新行在最后)"""
    if not os.path.exists(CSV_PATH):
        return None
    df = pd.read_csv(CSV_PATH)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"])


def get_market_value_series(days=30):
    """最近N天活跃市值收盘序列 {日期字符串: 收盘值}"""
    df = load_csv_history()
    if df is None or len(df) == 0:
        return {}
    return {str(d.date()): float(r["close"]) for d, r in df.tail(days).iterrows()}


def get_today_from_csv():
    """
    从 CSV 取当日活跃市值数据。
    ⚠ 铁律(2026-08-13 修复): 只有 CSV 最新日期 == 今天 才返回，否则返回 None。
    活跃市值是指南针App独有数据、用户每日手动补充——绝不能把昨日值当今日用。
    """
    df = load_csv_history()
    if df is None or len(df) < 2:
        return None
    last, prev = df.iloc[-1], df.iloc[-2]
    today = datetime.now().strftime("%Y-%m-%d")
    if str(last.name.date()) != today:
        # 最新记录不是今天: 用户今日未补充 → 缺失(报告显示【待补充】并询问用户)
        return None
    chg = (last["close"] / prev["close"] - 1) * 100
    return {
        "日期": str(last.name.date()),
        "活跃市值": round(float(last["close"]), 1),
        "开盘": round(float(last["open"]), 1),
        "最高": round(float(last["high"]), 1),
        "最低": round(float(last["low"]), 1),
        "涨跌幅": round(chg, 2),
        "前一日": round(float(prev["close"]), 1),
    }


def record_user_value(date_str, close, high=None, low=None, open_=None, source="指南针app"):
    """
    记录用户每日手动补充的活跃市值(指南针App独有数据,无法自动抓取)
    date_str: 'YYYY-MM-DD' 或 'YYYY/MM/DD'
    close: 当日活跃市值收盘值(必填)
    追加到 data/active_market_value.csv(若该日期已存在则更新)
    用法:
        python utils/market_value.py record 2026-08-11 218000 221000 217000
        python utils/market_value.py record 2026-08-11 218000          # 只给收盘
    """
    import pandas as pd
    if not os.path.exists(CSV_PATH):
        print(f"✗ CSV 不存在: {CSV_PATH}")
        return False
    df = pd.read_csv(CSV_PATH)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    if date_str in df["date"].values:
        df.loc[df["date"] == date_str, ["close", "high", "low", "open"]] = [close, high or close, low or close, open_ or close]
        print(f"ℹ 更新已有日期 {date_str}: close={close}")
    else:
        # 追加一行,保留原格式(最后一行)
        new_row = {"date": date_str, "open": open_ or close, "high": high or close,
                   "low": low or close, "close": close}
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        print(f"✓ 已追加 {date_str}: close={close}")
    df.to_csv(CSV_PATH, index=False)
    print(f"✓ CSV 已保存({len(df)}行)")
    return True


def get_active_market_value():
    """
    当日活跃市值: CSV优先(收盘后可靠) → mootdx全市场快照兜底(盘中实时)
    返回: dict {活跃市值(亿), 股票数, 日期, 数据源}
    ⚠ 收盘后(>=15:00)若无当日CSV → 直接标记缺失,不走mootdx全市场拉取(慢且是盘中值)
    """
    from_csv = get_today_from_csv()
    if from_csv:
        return {
            "活跃市值": from_csv["活跃市值"],
            "涨跌幅": from_csv.get("涨跌幅"),
            "股票数": None,
            "日期": from_csv["日期"].replace("-", ""),
            "数据源": "CSV历史",
        }

    # 收盘后没有当日数据 → 缺失(用户未补充),报告显示【待补充】
    if datetime.now().hour >= 15:
        return {
            "活跃市值": None,
            "涨跌幅": None,
            "股票数": None,
            "日期": datetime.now().strftime("%Y%m%d"),
            "数据源": "缺失(用户未补充)",
        }

    # mootdx 兜底(盘中)
    from mootdx.quotes import Quotes

    # 当日缓存
    today = datetime.now().strftime("%Y%m%d")
    cache_path = os.path.join(CACHE_DIR, f"{today}_market_value.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    client = Quotes.factory(market='std')

    # 获取全部股票代码（沪深）
    codes = []
    for mkt in [0, 1]:
        stocks = client.stocks(market=mkt)
        for code in stocks['code']:
            code = str(code).zfill(6)
            if code.startswith(('60', '00', '30', '68')) and code.isdigit():
                codes.append(code)

    # 分批拉快照（mootdx单次最多返回80只）
    total_mv = 0.0
    count = 0
    batch_size = 80
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        try:
            q = client.quotes(symbol=batch)
            if q is None or len(q) == 0:
                continue
            price = q['price'].astype(float)
            vol = q['vol'].astype(float)  # 手
            # 活跃市值 = Σ(价格 × 成交量(股))，vol单位是手=100股
            mv = (price * vol * 100).sum()
            total_mv += mv
            count += len(q)
        except Exception as e:
            print(f"[批{i}失败] {e}")
        time.sleep(0.15)

    result = {
        "活跃市值": round(total_mv / 1e8, 2),  # 亿
        "股票数": count,
        "日期": today,
        "数据源": "mootdx快照",
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def trend_judge(current, history):
    """
    活跃市值趋势判断
    history: 历史序列 dict {日期: 活跃市值}
    返回: 判断文本
    """
    if not history:
        return "无历史数据，首次记录"

    dates = sorted(history.keys())
    values = [history[d] for d in dates]
    recent = values[-5:] if len(values) >= 5 else values
    n = len(recent)

    # 3~5天趋势方向
    if n >= 2:
        slope = (recent[-1] - recent[0]) / max(1, n - 1)
        if slope > 0.005 * recent[0]:  # 0.5%+/天
            return f"持续上行（3-5日趋势↑）→ 增量资金进场，可重仓，积极参与B1/B2买点"
        elif slope < -0.005 * recent[0]:
            return f"连续回落（3-5日趋势↓）→ 活跃资金离场，防守为主，减少开新仓"
        else:
            return "走平震荡 → 存量博弈，降仓位，以搬砖战法和短线快进快出为主"

    return "数据不足"


def amv_timing_signal(chg_pct):
    """
    图形买点体系择时信号(基于活跃市值单日涨跌幅)
    入场: 单日 +4% 以上阳线 → 增量资金进场信号
    离场: 单日 -2.3% 以上跌幅 → 波段结束信号
    返回: (级别, 信号文本)
    """
    if chg_pct is None:
        return "未知", "无当日数据"
    if chg_pct >= 8:
        return "大波段", f"活跃市值 +{chg_pct:.1f}% 大波段阳线 → 炒主线/主题，积极参与"
    if chg_pct >= 4:
        return "小波段", f"活跃市值 +{chg_pct:.1f}% ≥ +4% → 小波段入场信号，增量资金进场，炒主题"
    if chg_pct <= -2.3:
        return "离场", f"活跃市值 {chg_pct:.1f}% ≤ -2.3% → 波段结束信号，开始减仓或清仓"
    if chg_pct <= -1:
        return "警惕", f"活跃市值 {chg_pct:.1f}% 接近离场线(-2.3%)，谨慎观望"
    return "平静", f"活跃市值 {chg_pct:+.1f}% 无明显信号，维持现有策略"


def main():
    mv = get_active_market_value()
    print("═" * 50)
    print(f"活跃市值: {mv['活跃市值']:.0f}（日期 {mv.get('日期','?')}，数据源 {mv.get('数据源','?')}）")
    if mv.get("涨跌幅") is not None:
        lv, sig = amv_timing_signal(mv["涨跌幅"])
        print(f"涨跌幅: {mv['涨跌幅']:+.2f}%  [{lv}]")
        print(f"择时信号: {sig}")
    print("═" * 50)

    # 历史序列(CSV 或缓存)
    hist = get_market_value_series(30)
    if not hist:
        for f in os.listdir(CACHE_DIR):
            if f.endswith("_market_value.json"):
                d = f.split("_")[0]
                with open(os.path.join(CACHE_DIR, f), "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    hist[d] = data["活跃市值"]

    print(f"历史记录: {len(hist)} 天")
    for d in sorted(hist.keys())[-10:]:
        print(f"  {d}: {hist[d]:.0f}")
    print()
    print("趋势判断:", trend_judge(mv["活跃市值"], hist))


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) >= 3 and _sys.argv[1] == "record":
        # python utils/market_value.py record 2026-08-11 218000 [high] [low] [open]
        d = _sys.argv[2]
        close = float(_sys.argv[3])
        high = float(_sys.argv[4]) if len(_sys.argv) > 4 else None
        low = float(_sys.argv[5]) if len(_sys.argv) > 5 else None
        op = float(_sys.argv[6]) if len(_sys.argv) > 6 else None
        record_user_value(d, close, high, low, op)
    else:
        main()
