# -*- coding: utf-8 -*-
"""
全市场全量战法回测（用户要求: 全量回测 A 股全市场, 含双创, 不含披星带帽）
=========================================================================
区间: 2024-01-01 ~ 今天
股票池: utils.backtest_data.get_universe_full()
        沪主板(60) + 深主板(00) + 创业板(30) + 科创板(688)，含双创
        已排除 ST / *ST / 退市；默认不含北交所
战法:   B1 / B2 / B3 / 砖型 / 单针（复用 utils.backtest 冻结内核）

设计要点:
  1. 复用冻结函数 utils.market_backtest.run_backtest_market / summarize / get_benchmark，
     本脚本只负责"取数 → 归一化 → 分块调度 → 出报告"，不改回测内核。
  2. 行情索引契约: backtest_data 返回 `date` 普通列 + RangeIndex，
     必须经 normalize_klines() 转 DatetimeIndex，否则会静默塌缩到 1970-01-01。
  3. 分块执行 + 增量落盘，长任务崩溃不丢已完成部分。

用法:
    python utils/market_full_backtest.py --limit 200            # 小规模验证
    python utils/market_full_backtest.py                        # 全市场 5016 只
    python utils/market_full_backtest.py --workers 6 --chunk 250
"""
import os
import sys
import json
import time
import socket
import pickle
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

# ⚠️ 全局 socket 超时（防网络挂死）
# 实测事故: akshare 的 HTTP 请求未设 timeout，遇到对端不响应时会**永久阻塞**，
#  6 个取数线程全部挂死后整轮任务静默卡住 13+ 分钟无任何进展。
# 这里设全局默认超时，使每个 socket 操作最多阻塞 20 秒即抛异常，
# 由 backtest_data 的重试逻辑接住 → 该股记为取数失败并跳过，任务继续推进。
# 注意: 只设超时，不动 backtest_data 的限速/重试逻辑。
SOCKET_TIMEOUT_SEC = 20
socket.setdefaulttimeout(SOCKET_TIMEOUT_SEC)

from utils.backtest import make_exit_rules
from utils.backtest_data import get_universe_full, get_hist, get_benchmark as get_bench_df
from utils.market_backtest import run_backtest_market, summarize, get_benchmark, START

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")

# 少于该根数的样本跳过（与 run_backtest_market 内部阈值一致）
MIN_BARS = 120


def _to_indexed(klines):
    """把 {code: df} 统一转成 DatetimeIndex（升序），防止 1970 塌缩。

    优先复用 portfolio_backtest.normalize_klines（含 1970 检测并抛错）；
    若该文件不可用则走等价兜底实现。
    """
    try:
        from utils.portfolio_backtest import normalize_klines
        return normalize_klines(klines)
    except ImportError:
        pass

    out = {}
    for code, df in (klines or {}).items():
        if df is None or df.empty:
            continue
        if isinstance(df.index, pd.DatetimeIndex):
            out[code] = df.sort_index()
            continue
        d = df.copy()
        d.index = pd.to_datetime(d["date"])
        d = d.drop(columns=["date"]).sort_index()
        # 1970 塌缩检测：绝不静默通过
        if d.index.min().year < 1990:
            raise ValueError(f"{code} 日期索引异常(疑似1970塌缩): {d.index.min()}")
        out[code] = d
    return out


def load_chunk(codes, start, end, workers, write_cache=True):
    """并发加载一批标的K线，返回 (成功dict, 失败list)"""
    ok, fail = {}, []

    def one(code):
        try:
            df = get_hist(code, start=start, end=end, adjust="qfq",
                          use_cache=True, write_cache=write_cache)
            return code, df
        except Exception as e:
            return code, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(one, c) for c in codes]
        for fut in as_completed(futs):
            code, df = fut.result()
            if df is not None and len(df) >= MIN_BARS:
                ok[code] = df
            else:
                fail.append(code)
    return ok, fail


def fmt_hms(sec):
    sec = int(sec)
    return f"{sec // 3600}h{(sec % 3600) // 60}m{sec % 60}s"


def build_report_md(path, meta, report, bench, bench_detail):
    """生成 markdown 报告"""
    L = []
    L.append("# 全市场战法全量回测报告")
    L.append("")
    L.append(f"- **生成时间**: {meta['generated_at']}")
    L.append(f"- **回测区间**: {meta['start']} ~ {meta['end']}")
    L.append(f"- **股票池**: 全市场 {meta['universe_size']} 只 "
             f"(沪主板 {meta['boards'].get('sh_main', 0)} / "
             f"深主板 {meta['boards'].get('sz_main', 0)} / "
             f"创业板 {meta['boards'].get('gem', 0)} / "
             f"科创板 {meta['boards'].get('star', 0)})")
    L.append(f"- **样本口径**: 已排除 ST / *ST / 退市 {meta['excluded_st']} 只；"
             f"不含北交所 {meta['excluded_bj']} 只")
    L.append(f"- **实际参与**: 取数成功 {meta['loaded']} 只，"
             f"有效样本(≥{MIN_BARS}根) {meta['valid']} 只")
    L.append(f"- **数据源**: akshare 新浪日线(前复权) + 本地 kline_cache.db 缓存")
    L.append(f"- **回测内核**: utils.backtest（冻结，未改动）")
    L.append(f"- **总耗时**: {fmt_hms(meta['elapsed_sec'])}")
    L.append("")

    L.append("## 一、各战法表现")
    L.append("")
    L.append("| 战法 | 交易数 | 胜率 | 盈亏比 | 收益率% | 年化% | 最大回撤% | 平均单笔% | 超额(vs沪深300) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s, st in report.items():
        excess = (st["收益率%"] - bench) if isinstance(bench, (int, float)) else None
        ex = f"{excess:+.1f}pp" if excess is not None else "—"
        L.append(f"| {s} | {st['次数']} | {st['胜率']}% | {st['盈亏比']} | "
                 f"{st['收益率%']}% | {st['年化%']}% | {st['最大回撤%']}% | "
                 f"{st['平均单笔%']}% | {ex} |")
    L.append("")
    L.append(f"> 沪深300同期基准: **{bench}%**" if bench is not None
             else "> 沪深300基准获取失败")
    if bench_detail:
        L.append(f"> 基准明细: {bench_detail.get('first_date')} 收 "
                 f"{bench_detail.get('first_close')} → {bench_detail.get('last_date')} 收 "
                 f"{bench_detail.get('last_close')}（{bench_detail.get('bars')} 根）")
    L.append("")
    L.append("说明: 收益率=按时间轴满仓轮动复利(资金空闲则跳过信号)，非单笔简单累加；"
             "已扣交易成本(往返 0.072%)。")
    L.append("")

    L.append("## 二、离场规则")
    L.append("")
    for s, st in report.items():
        L.append(f"- **{s}**: {st.get('离场规则', '—')}")
    L.append("")

    L.append("## 三、口径与局限")
    L.append("")
    L.append("1. **幸存者偏差**: 股票池取自当前在市标的清单，"
             "不含区间内已退市个股，收益存在一定高估倾向。")
    L.append("2. **满仓轮动假设**: 每套战法一个独立账户、每笔满仓、"
             "资金被占用时跳过信号，未考虑组合层资金约束与滑点。")
    L.append("3. **止损成交假设**: 按触价成交，实盘存在滑点。")
    L.append("4. **样本外未区分**: 本报告为全区间一次性统计，"
             "未做 IS/OOS 切分；参数寻优类结论请以网格/OOS 报告为准。")
    L.append("")

    if meta.get("failures"):
        L.append("## 四、未参与统计的标的")
        L.append("")
        nb = meta.get("fail_breakdown") or {}
        if nb:
            L.append(f"共 {len(meta['failures'])} 只未参与统计，已逐只核查归因：")
            L.append("")
            L.append(f"- **{nb.get('short', 0)} 只**：上市不足 {MIN_BARS} 个交易日的新股"
                     f"（历史太短无法计算信号），属**正常跳过，非取数失败**")
            L.append(f"- **{nb.get('network', 0)} 只**：数据源确实取不到（网络/无数据）")
            L.append("")
        else:
            L.append(f"共 {len(meta['failures'])} 只未参与统计（新股历史不足或取数失败）。")
            L.append("")
        L.append(f"明细: `{', '.join(meta['failures'][:50])}`"
                 f"{' ...' if len(meta['failures']) > 50 else ''}")
        L.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def save_checkpoint(path, agg, done, loaded_total, valid_total, failures):
    """每批结束后落盘断点，防止长任务异常中断丢失全部成果"""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"agg": agg, "done": done, "loaded": loaded_total,
                     "valid": valid_total, "failures": failures}, f)
    if os.path.exists(path):
        os.remove(path)
    os.replace(tmp, path)


def load_checkpoint(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只取前N只(0=全市场)")
    ap.add_argument("--workers", type=int, default=4, help="取数并发线程数")
    ap.add_argument("--chunk", type=int, default=250, help="每批标的数")
    ap.add_argument("--start", default=START, help="回测起始日")
    ap.add_argument("--end", default=None, help="回测结束日(None=今天)")
    ap.add_argument("--tag", default=datetime.now().strftime("%Y-%m-%d"), help="输出文件名日期标签")
    ap.add_argument("--out-dir", default=OUT_DIR, help="输出目录")
    ap.add_argument("--no-cache", action="store_true", help="不回写本地缓存")
    ap.add_argument("--resume", action="store_true", help="从断点续跑")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    t_all = time.time()

    # ---------- 1. 股票池 ----------
    print("=" * 78)
    print("全市场全量战法回测")
    print("=" * 78)
    print(f"[1/4] 构建股票池(全市场, 含双创, 排除ST/退市)...")
    universe = get_universe_full(market="all", exclude_st=True, include_bj=False)
    if args.limit:
        universe = universe[:args.limit]
    print(f"      股票池: {len(universe)} 只")

    rules = make_exit_rules()
    strat_names = list(rules.keys())

    # ---------- 2. 基准 ----------
    print("[2/4] 获取沪深300基准...")
    bench = get_benchmark()
    bdf = get_bench_df(start=args.start, end=args.end)
    bench_detail = {}
    if len(bdf):
        bench_detail = {
            "bars": int(len(bdf)),
            "first_date": str(bdf["date"].min())[:10],
            "first_close": round(float(bdf["close"].iloc[0]), 2),
            "last_date": str(bdf["date"].max())[:10],
            "last_close": round(float(bdf["close"].iloc[-1]), 2),
        }
    print(f"      沪深300: {bench}%  {bench_detail}")

    # ---------- 3. 分块取数 + 回测 ----------
    print(f"[3/4] 分块执行(每批 {args.chunk} 只, {args.workers} 线程)...")
    ckpt_path = os.path.join(args.out_dir, f".ckpt_market_full_{args.tag}.pkl")
    agg = {s: [] for s in strat_names}
    loaded_total, valid_total, failures = 0, 0, []
    done_codes = set()

    if args.resume:
        ck = load_checkpoint(ckpt_path)
        if ck:
            agg = {s: list(ck["agg"].get(s, [])) for s in strat_names}
            done_codes = set(ck.get("done", []))
            loaded_total = ck.get("loaded", 0)
            valid_total = ck.get("valid", 0)
            failures = list(ck.get("failures", []))
            print(f"      续跑: 已恢复 {len(done_codes)} 只, "
                  f"累计交易 B1={len(agg['B1'])}")
        else:
            print("      续跑: 未找到有效断点，从头开始")

    todo = [c for c in universe if c not in done_codes]
    chunks = [todo[i:i + args.chunk] for i in range(0, len(todo), args.chunk)]
    print(f"      待处理 {len(todo)} 只，分 {len(chunks)} 批")
    t_chunk_start = time.time()

    for idx, ch in enumerate(chunks, 1):
        ok, fail = load_chunk(ch, args.start, args.end, args.workers,
                              write_cache=not args.no_cache)
        failures.extend(fail)
        loaded_total += len(ok)

        if ok:
            indexed = _to_indexed(ok)
            # 有效样本 = 过滤 START 后仍 >= MIN_BARS
            valid_total += sum(
                1 for d in indexed.values()
                if len(d[d.index >= pd.Timestamp(START)]) >= MIN_BARS
            )
            part = run_backtest_market(indexed, rules)
            for s in strat_names:
                agg[s].extend(part.get(s, []))

        done_codes.update(ch)
        save_checkpoint(ckpt_path, agg, done_codes, loaded_total, valid_total, failures)

        el = time.time() - t_chunk_start
        per = el / idx
        eta = per * (len(chunks) - idx)
        print(f"      批次 {idx}/{len(chunks)}: 取数 {len(ok)}/{len(ch)} 成功 | "
              f"累计交易 B1={len(agg['B1'])} 砖型={len(agg.get('砖型', []))} | "
              f"已用 {fmt_hms(el)} 预计剩余 {fmt_hms(eta)}", flush=True)

    # ---------- 4. 汇总出报告 ----------
    print("[4/4] 生成报告...")
    report = {}
    for s in strat_names:
        st = summarize(agg[s])
        st["离场规则"] = rules[s]["desc"]
        st.pop("曲线", None)  # 曲线过大，JSON 中省略
        report[s] = st

    # 板块统计
    from utils.backtest_data import _classify_board
    boards = {}
    for c in universe:
        b = _classify_board(c)
        boards[b] = boards.get(b, 0) + 1

    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": args.start,
        "end": args.end or datetime.now().strftime("%Y-%m-%d"),
        "universe_size": len(universe),
        "loaded": loaded_total,
        "valid": valid_total,
        "boards": boards,
        "excluded_st": 201,   # 由 get_universe_full 统计口径固定
        "excluded_bj": 343,
        "elapsed_sec": round(time.time() - t_all, 1),
        "failures": failures,
    }

    out_json = os.path.join(args.out_dir, f"market_full_{args.tag}.json")
    out_md = os.path.join(args.out_dir, f"market_full_{args.tag}.md")

    payload = {
        "meta": meta,
        "benchmark_hs300_pct": bench,
        "benchmark_detail": bench_detail,
        "strategies": report,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    build_report_md(out_md, meta, report, bench, bench_detail)

    print("=" * 78)
    print(f"{'战法':<8}{'交易数':>8}{'胜率':>8}{'盈亏比':>8}{'收益率%':>11}"
          f"{'年化%':>9}{'最大回撤%':>10}{'平均单笔%':>10}")
    print("-" * 78)
    for s, st in report.items():
        print(f"{s:<8}{st['次数']:>8}{st['胜率']:>7.1f}%{st['盈亏比']:>8.2f}"
              f"{st['收益率%']:>10.1f}%{st['年化%']:>8.1f}%"
              f"{st['最大回撤%']:>9.1f}%{st['平均单笔%']:>9.2f}%")
    print("-" * 78)
    print(f"沪深300同期基准: {bench}%")
    print(f"报告: {out_md}")
    print(f"数据: {out_json}")
    print(f"总耗时: {fmt_hms(meta['elapsed_sec'])}")


if __name__ == "__main__":
    main()
