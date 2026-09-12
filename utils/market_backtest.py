# -*- coding: utf-8 -*-
"""
全市场战法回测 — 2024-01-01 ~ 今天
=================================
标准回测方法(用户指定股票池):
  1. 中证500全部成分股(500只) + 新浪快照补充500只非中证500(覆盖大中小盘) = 1000只
  2. 并发拉取1000只日K(腾讯源, 3线程, 带重试)
  3. 每只股票跑五套战法信号 → 次日开盘买入 → 按各战法自己的止损止盈离场
  4. 每套战法一个账户, 按时间轴满仓轮动复利, 统计: 收益率/年化/最大回撤/胜率/盈亏比
  5. 基准对照: 同区间沪深300涨跌幅

用法:
    python utils/market_backtest.py                     # 中证500全部+500只其他=1000只
    python utils/market_backtest.py --max-stocks 200    # 限制数量(测试)
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from utils.data_router import _from_tencent
from utils.backtest import compute_signals, backtest_stock, make_exit_rules
from utils.sim_screener import get_market_snapshot

START = "2024-01-01"


def get_zz500():
    """中证500成分股(akshare csindex官方), 清代理+重试+备用源"""
    import akshare as ak
    from utils.stock_data import _clear_proxy
    _clear_proxy()  # 清代理(项目已知: 代理会随机失效)
    for i in range(3):
        try:
            df = ak.index_stock_cons_csindex(symbol="000905")
            if df is not None and len(df) > 100:
                codes = [str(c).zfill(6) for c in df["成分券代码"].tolist()]
                names = dict(zip(codes, df["成分券名称"].tolist()))
                return [(c, names.get(c, c)) for c in codes]
        except Exception as e:
            print(f"      csindex第{i+1}次失败({type(e).__name__}), 重试...")
            time.sleep(2)
    # 备用源
    try:
        df = ak.index_stock_cons(symbol="000905")
        if df is not None and len(df) > 100:
            code_col = "品种代码" if "品种代码" in df.columns else df.columns[1]
            name_col = "品种名称" if "品种名称" in df.columns else df.columns[2]
            codes = [str(c).zfill(6) for c in df[code_col].tolist()]
            names = dict(zip(codes, df[name_col].tolist()))
            return [(c, names.get(c, c)) for c in codes]
    except Exception as e:
        print(f"      备用源也失败: {e}")
    raise RuntimeError("中证500成分股获取失败")


def get_universe(max_stocks=None, mode="zz500"):
    """构建股票池
    mode=zz500: 中证500全部 + 500只其他 = 1000只(用户指定)
    mode=full : 全部中国A股(排除ST/北交所/退市, 新浪快照源)
    mode=all  : 全市场(沪主板60 + 深主板00 + 创业板30 + 科创板688, 含双创),
                排除 ST/退市; 默认不纳入北交所。走 utils.backtest_data.get_universe_full()
    """
    if mode == "all":
        # T02-1: 全市场股票池(含双创, 排除ST), 由 backtest_data 统一提供
        from utils.backtest_data import get_universe_full, get_universe_names
        print("[1/4] 构建股票池: 全市场(沪主板+深主板+创业板+科创板, 排除ST)...")
        codes = get_universe_full(market="all", exclude_st=True, include_bj=False)
        if max_stocks:
            codes = codes[:max_stocks]
        names = get_universe_names(codes)
        universe = [(c, names.get(c, c)) for c in codes]
        print(f"      最终股票池: {len(universe)} 只")
        return universe

    if mode == "full":
        print("[1/4] 构建股票池: 全部中国A股(排除ST/北交所/退市)...")
        universe = []
        seen = set()
        try:
            df = get_market_snapshot(max_pages=90)
            print(f"      新浪快照: {len(df)} 只")
            for _, row in df.iterrows():
                code = str(row.get("纯代码", "")).zfill(6)
                name = str(row.get("名称", ""))
                if not code or len(code) != 6:
                    continue
                if code.startswith(("4", "8", "9")):  # 北交所/三板
                    continue
                if "ST" in name.upper() or "退" in name:
                    continue
                amount = float(row.get("成交额", 0) or 0)
                if amount < 30_000_000:  # 成交额 < 3000万(排除僵尸票)
                    continue
                if code in seen:
                    continue
                seen.add(code)
                universe.append((code, name))
        except Exception as e:
            print(f"      新浪快照失败({e}), 改用akshare全市场代码列表")
            import akshare as ak
            from utils.stock_data import _clear_proxy
            _clear_proxy()
            df = ak.stock_info_a_code_name()
            for _, row in df.iterrows():
                code = str(row["code"]).zfill(6)
                name = str(row["name"])
                if code.startswith(("4", "8", "9")):
                    continue
                if "ST" in name.upper() or "退" in name:
                    continue
                if code in seen:
                    continue
                seen.add(code)
                universe.append((code, name))
        if max_stocks:
            universe = universe[:max_stocks]
        print(f"      最终股票池: {len(universe)} 只")
        return universe

    print("[1/4] 构建股票池: 中证500全部 + 500只其他(覆盖大中小盘)...")
    zz500 = get_zz500()
    zz500_codes = {c for c, _ in zz500}
    print(f"      中证500成分: {len(zz500)} 只")

    # 快照拿全市场(限流时用akshare兜底)
    extra = []
    seen = set()
    try:
        df = get_market_snapshot(max_pages=90)
        print(f"      新浪快照: {len(df)} 只")
        for _, row in df.iterrows():
            code = str(row.get("纯代码", "")).zfill(6)
            name = str(row.get("名称", ""))
            if not code or len(code) != 6:
                continue
            if code.startswith(("4", "8", "9")):  # 北交所/三板
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            amount = float(row.get("成交额", 0) or 0)
            if amount < 30_000_000:  # 成交额 < 3000万
                continue
            if code in zz500_codes or code in seen:
                continue
            seen.add(code)
            extra.append((code, name))
    except Exception as e:
        print(f"      新浪快照失败({e}), 改用akshare全市场代码列表兜底")
        import akshare as ak
        df = ak.stock_info_a_code_name()
        for _, row in df.iterrows():
            code = str(row["code"]).zfill(6)
            name = str(row["name"])
            if code.startswith(("4", "8", "9")):
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            if code in zz500_codes or code in seen:
                continue
            seen.add(code)
            extra.append((code, name))

    # 用户要求: 中证500全部(500) + 500只其他
    if max_stocks is None:
        extra = extra[:500]
        universe = zz500 + extra
    else:
        # 测试模式: 优先保留中证500, 不足部分用其他补
        n_zz = min(len(zz500), max_stocks)
        n_ex = max(0, max_stocks - n_zz)
        universe = zz500[:n_zz] + extra[:n_ex]

    n_zz_final = sum(1 for c, _ in universe if c in zz500_codes)
    print(f"      最终股票池: {len(universe)} 只 (中证500 {n_zz_final} + 其他 {len(universe)-n_zz_final})")
    return universe


def fetch_klines(code, retry=3):
    """拉单只K线(腾讯源), 失败重试"""
    for i in range(retry):
        try:
            r = _from_tencent(code, bars=700)
            df = r.get("df")
            if df is not None and len(df) >= 100:
                return df
        except Exception:
            pass
        time.sleep(0.3)
    return None


def load_all_klines(universe, max_workers=3):
    """并发拉取K线, 返回 {code: df}"""
    print(f"[2/4] 并发拉取 {len(universe)} 只日K(腾讯源, {max_workers}线程)...")
    t0 = time.time()
    data = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_klines, c): c for c, _ in universe}
        for fut in as_completed(futs):
            code = futs[fut]
            df = fut.result()
            if df is not None:
                df.index = pd.to_datetime(df.index)
                data[code] = df
            done += 1
            if done % 200 == 0:
                print(f"      已拉取 {done}/{len(universe)} ({time.time()-t0:.0f}s)")
    print(f"      完成: 成功 {len(data)}/{len(universe)}, 耗时 {(time.time()-t0)/60:.1f} 分钟")
    return data


def run_backtest_market(klines, rules):
    """全市场回测: 每套战法一个账户满仓轮动"""
    print("[3/4] 全市场跑五套战法信号...")
    strat_names = list(rules.keys())
    agg = {s: [] for s in strat_names}
    n_ok = 0
    for code, df in klines.items():
        try:
            df = df[(df.index >= START)]
            if len(df) < 120:
                continue
            sig = compute_signals(df)
            res = backtest_stock(sig, df, rules=rules)
            for s in strat_names:
                agg[s].extend(res[s])
            n_ok += 1
        except Exception:
            continue
    print(f"      有效股票 {n_ok} 只")
    return agg


def equity_curve(trades):
    """按时间轴满仓轮动复利 → 资金曲线(计算收益率/年化/最大回撤)"""
    if not trades:
        return {"收益率%": 0.0, "年化%": 0.0, "最大回撤%": 0.0, "实际交易": 0}
    sorted_trades = sorted(trades, key=lambda x: x["日期"])
    capital = 1.0
    free_date = None
    peak = 1.0
    mdd = 0.0
    done = 0
    curve = []
    for t in sorted_trades:
        try:
            sig_date = datetime.strptime(t["日期"], "%Y-%m-%d")
        except Exception:
            continue
        hold = t.get("持有", 10)
        if free_date is None or sig_date >= free_date:
            capital *= (1 + t["收益"] / 100)
            free_date = sig_date + timedelta(days=hold + 3)
            done += 1
            if capital > peak:
                peak = capital
            dd = (peak - capital) / peak * 100
            if dd > mdd:
                mdd = dd
            curve.append({"日期": t["日期"], "净值": round(capital, 4)})
    years = max((datetime.now() - datetime(2024, 1, 1)).days / 365.25, 1.0)
    ret = (capital - 1) * 100
    annual = (capital ** (1 / years) - 1) * 100
    return {"收益率%": round(ret, 1), "年化%": round(annual, 1),
            "最大回撤%": round(mdd, 1), "实际交易": done, "曲线": curve}


def summarize(trades):
    """统计: 次数/胜率/盈亏比/平均单笔 + 时间轴复利收益率/年化/最大回撤"""
    if not trades:
        return {"次数": 0, "胜率": 0, "盈亏比": 0, "平均单笔%": 0,
                "收益率%": 0, "年化%": 0, "最大回撤%": 0, "实际交易": 0, "曲线": []}
    gains = [t["收益"] for t in trades]
    wins = [g for g in gains if g > 0]
    losses = [g for g in gains if g <= 0]
    win_rate = len(wins) / len(gains) * 100
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    ratio = round(avg_win / abs(avg_loss), 2) if avg_loss else 0
    ec = equity_curve(trades)
    return {
        "次数": len(gains),
        "胜率": round(win_rate, 1),
        "盈亏比": ratio,
        "平均单笔%": round(np.mean(gains), 2),
        **ec,
    }


def get_benchmark():
    """沪深300同区间涨跌幅基准"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000300")
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"] >= START]
        if len(df):
            ret = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100
            return round(ret, 1)
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=0, help="限制股票数量(0=不限)")
    parser.add_argument("--workers", type=int, default=3, help="并发线程数")
    parser.add_argument("--mode", default="all", choices=["all", "zz500", "full"],
                        help="all=全市场(沪主板+深主板+创业板+科创板, 排除ST, 默认); "
                             "zz500=中证500+500其他(1000只, 向后兼容); full=全部A股(新浪快照)")
    parser.add_argument("--save", default="", help="报告保存路径")
    args = parser.parse_args()

    universe = get_universe(args.max_stocks or None, mode=args.mode)
    rules = make_exit_rules()

    bench = get_benchmark()
    print(f"      沪深300同期基准: {bench}%" if bench is not None else "      基准获取失败")

    klines = load_all_klines(universe, max_workers=args.workers)
    agg = run_backtest_market(klines, rules)

    print("\n[4/4] 生成报告")
    print("=" * 100)
    print(f"股票池回测  {START} ~ {datetime.now().strftime('%Y-%m-%d')}  (满仓轮动复利)")
    print(f"样本: {len(klines)} 只 · 基准(沪深300同期): {bench}%")
    print("=" * 100)
    print(f"{'战法':<8}{'交易数':>7}{'胜率':>8}{'盈亏比':>8}{'收益率%':>10}{'年化%':>9}{'最大回撤%':>10}{'平均单笔%':>9}")
    print("-" * 100)
    report = {}
    for s, r in rules.items():
        st = summarize(agg[s])
        st["离场规则"] = r["desc"]
        report[s] = st
        flag = "✅" if st["次数"] >= 50 and st["盈亏比"] > 1.2 and st["收益率%"] > 0 else "⚠️"
        print(f"{s:<8}{st['次数']:>7}{st['胜率']:>7.1f}%{st['盈亏比']:>8.2f}{st['收益率%']:>9.1f}%{st['年化%']:>8.1f}%{st['最大回撤%']:>9.1f}%{st['平均单笔%']:>9.2f}%  {flag}")
    print("=" * 100)
    print(f"说明: 收益率=时间轴满仓轮动复利(资金空闲跳过信号), 非单笔简单累加")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump({"date": datetime.now().strftime("%Y-%m-%d"), "样本数": len(klines),
                       "基准沪深300": bench, "战法": report}, f, ensure_ascii=False, indent=2)
        print(f"报告已保存: {args.save}")


if __name__ == "__main__":
    main()
