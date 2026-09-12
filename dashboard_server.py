#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AS 交易工作台 · 数据桥服务
把 ahao-stock-agent 的全部数据能力暴露为本地 JSON API, 供前端工作台调用。

启动:  .venv\\Scripts\\python dashboard_server.py [--port 8899]
访问:  http://127.0.0.1:8899/api/snapshot

API 一览:
  GET /api/snapshot          大盘快照(指数/板块/情绪/成交额/活跃市值/美股/韩股)
  GET /api/quote?codes=600519,000001   腾讯批量实时报价
  GET /api/stock?code=600519 个股当日详情
  GET /api/kline?code=600519&count=120 K线 + 黄白线/砖型/单针指标 + 趋势分析
  GET /api/sim                模拟盘账户状态(四组持仓/现金/流水)
  GET /api/journal            真实交易记录(带持仓聚合)
  GET /api/news?size=300      新闻情绪量化(三分类+板块映射+重点新闻)
  GET /api/reports?days=7     每日复盘报告列表(盘前/收盘/模拟盘, 按日期聚合)
  GET /api/health            健康检查
"""
import os
import sys
import json
import argparse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def _json_safe(o):
    """递归转 numpy 类型为原生 JSON 类型; NaN/Infinity 转 null(浏览器 JSON.parse 拒绝裸 NaN)"""
    import numpy as np
    import math
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(x) for x in o]
    return o


# ===================== 数据适配层 =====================

def get_snapshot():
    """大盘快照(读当日缓存, 无缓存才实时抓取)"""
    from utils.data_router import load_snapshot, build_market_snapshot
    snap = load_snapshot()
    if snap is None:
        try:
            snap = build_market_snapshot()
        except Exception:
            return {"status": "ERROR", "msg": "快照构建失败"}
    # 精简: 只保留前端需要的字段
    out = {"生成时间": snap.get("生成时间", "")}
    for key in ("指数", "板块", "情绪", "成交额", "活跃市值", "美股", "韩股"):
        v = snap.get(key, {})
        if isinstance(v, dict):
            out[key] = {"value": v.get("value"), "source": v.get("source"),
                        "status": v.get("status", "normal")}
        else:
            out[key] = {"value": v}
    return out


def get_quotes(codes):
    """腾讯批量实时报价(优先 a-stock-data 第0优先级源, 失败回退 mootdx)"""
    result = {}
    # 1. a-stock-data tencent_quote(已实测: 600519/000001 正常)
    try:
        sys.path.insert(0, os.path.join(ROOT, "data-sources-astock"))
        from _skill_defs import tencent_quote
        q = tencent_quote(codes)
        for c in codes:
            c = str(c).strip()
            if c in q and q[c].get("name"):
                item = q[c]
                result[c] = {
                    "name": item.get("name"),
                    "price": item.get("price"),
                    "change_pct": item.get("change_pct") or item.get("pct_chg"),
                    "high": item.get("high"), "low": item.get("low"),
                    "open": item.get("open"),
                    "pe": item.get("pe_ttm"), "pb": item.get("pb"),
                    "mcap": item.get("mcap"),
                    "turnover": item.get("turnover_rate"),
                }
        if result:
            return result
    except Exception:
        pass
    # 2. mootdx 兜底
    try:
        from utils.screen_bull import fetch_kline
        for c in codes:
            c = str(c).strip()
            if not c or c in result:
                continue
            df = fetch_kline(c, bars=5)
            if df is not None and len(df) > 0:
                last = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else last
                pct = (float(last["close"]) - float(prev["close"])) / float(prev["close"]) * 100
                result[c] = {"name": c, "price": round(float(last["close"]), 2),
                             "change_pct": round(pct, 2)}
    except Exception:
        pass
    return result


def get_stock(code):
    """个股当日详情
    优先 a-stock-data tencent_quote(第0优先级源, 已验证可用) → mootdx get_stock_detail 兜底
    """
    code = str(code).zfill(6)
    # 1. a-stock-data 腾讯报价
    try:
        sys.path.insert(0, os.path.join(ROOT, "data-sources-astock"))
        from _skill_defs import tencent_quote
        q = tencent_quote([code])
        if code in q and q[code].get("name") and q[code].get("price"):
            item = q[code]
            price = float(item["price"])
            pre_close = float(item.get("last_close") or item.get("pre_close") or 0)
            pct = item.get("change_pct")
            if pct is None and pre_close:
                pct = (price - pre_close) / pre_close * 100
            amount_wan = item.get("amount_wan") or item.get("amount") or 0
            return {
                "代码": code, "名称": item.get("name"),
                "现价": round(price, 2),
                "昨收": round(pre_close, 2) if pre_close else None,
                "今开": round(float(item["open"]), 2) if item.get("open") else None,
                "涨跌幅": round(float(pct), 2) if pct is not None else None,
                "最高": round(float(item["high"]), 2) if item.get("high") else None,
                "最低": round(float(item["low"]), 2) if item.get("low") else None,
                "成交额": round(float(amount_wan) / 1e4, 2),
            }
    except Exception:
        pass
    # 2. mootdx 兜底
    from utils.stock_data import get_stock_detail
    return get_stock_detail(code)


def get_kline(code, count=120):
    """K线 + 指标分析"""
    from utils.data_router import get_daily_bars
    from utils.indicators import analyze_dual_line, analyze_trend, white_line, yellow_line
    import pandas as pd

    r = get_daily_bars(code, count=count, purpose="indicator")
    if r.get("status") != "OK":
        return {"status": r.get("status"), "msg": r.get("msg", "")}

    data = r["data"]
    df = pd.DataFrame(data).set_index("date")
    df.index = pd.to_datetime(df.index)

    # 黄白线分析(BBI)
    dual = {}
    trend = {}
    try:
        dual = analyze_dual_line(df)
        # 补全序列供前端画线
        wl = white_line(df["close"].astype(float))
        yl = yellow_line(df["close"].astype(float))
        dual["白线序列"] = [round(v, 3) for v in wl.tolist()]
        dual["黄线序列"] = [round(v, 3) for v in yl.tolist()]
        # 信号列表转文本(JSON安全)
        dual["信号"] = [str(s) for s in dual.get("信号", [])]
        dual["多头区间"] = bool(dual.get("多头区间", False))
    except Exception:
        pass
    try:
        trend = analyze_trend(df)
        trend = {k: _json_safe(v) for k, v in trend.items()}
    except Exception:
        pass

    return {
        "status": "OK",
        "source": r.get("source"),
        "as_of": r.get("as_of"),
        "adjustment": r.get("adjustment"),
        "klines": data,
        "dual_line": dual,
        "trend": trend,
    }


def get_sim():
    """模拟盘账户"""
    from utils.sim_portfolio import load_account, status
    acc = load_account()
    if not acc:
        return {"status": "EMPTY", "msg": "模拟盘未初始化"}
    groups = {}
    for gname, g in acc.get("分组", {}).items():
        groups[gname] = {
            "预算": g.get("预算", 0),
            "持仓": g.get("持仓", {}),
        }
    try:
        st = status(acc)
    except Exception:
        st = {}
    return {
        "status": "OK",
        "起始日": acc.get("起始日"),
        "结束日": acc.get("结束日"),
        "总资金": acc.get("总资金"),
        "现金": acc.get("现金"),
        "分组": groups,
        "流水": acc.get("流水", []),
        "已平仓": acc.get("已平仓", []),
        "status": st,
    }


def get_news_sentiment(page_size=300):
    """新闻情绪量化分析(新浪7x24 → 三分类 → 板块映射 → 聚合)"""
    from utils.news_sentiment import analyze_news
    return analyze_news(page_size=page_size)


def get_risk_overview():
    """风控总览: 市场环境系数 + 模拟盘持仓风控状态"""
    import pandas as pd
    result = {"status": "OK", "环境": {}, "风控": {"持仓风控": []}}
    # 市场环境(上证 MA5/10/20 分级)
    try:
        from utils.market_env import market_env
        from utils.data_router import get_daily_bars
        r = get_daily_bars("000001", count=250, purpose="indicator")
        if r.get("status") == "OK":
            df = pd.DataFrame(r["data"]).set_index("date")
            df.index = pd.to_datetime(df.index)
            result["环境"] = market_env(df)
    except Exception as e:
        result["环境"] = {"error": str(e)[:100]}
    # 模拟盘风控状态
    try:
        from utils.sim_portfolio import load_account, risk_status, _fetch_live_prices
        acc = load_account()
        codes = set()
        for g in acc["分组"].values():
            codes.update(g["持仓"].keys())
        prices = _fetch_live_prices(list(codes)) if codes else {}
        result["风控"] = risk_status(acc, prices)
    except Exception as e:
        result["风控"] = {"error": str(e)[:100]}
    return result


def _report_gen_time(json_path):
    """从报告 json 读取「生成时间」字段, 缺失返回空串"""
    if not os.path.exists(json_path):
        return ""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            j = json.load(f)
        return str(j.get("生成时间", "") or "")
    except Exception:
        return ""


def _file_mtime(path):
    from datetime import datetime
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def get_reports(days=7):
    """每日复盘报告列表(盘前/收盘/模拟盘)，按日期聚合，含摘要 + markdown 原文

    供前端「每日复盘」面板: 日期 tab 切换 + 报告卡片展开/收起。
    兜底: has_today=False 时前端提示「今日休市/未生成」。
    """
    import re
    from datetime import datetime
    rdir = os.path.join(ROOT, "data", "reports")
    dates = set()
    if os.path.isdir(rdir):
        for fn in os.listdir(rdir):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})_(pre_market|post_market)\.(md|json)$", fn)
            if m:
                dates.add(m.group(1))
            m2 = re.match(r"^sim_(\d{4}-\d{2}-\d{2})\.md$", fn)
            if m2:
                dates.add(m2.group(1))
    dates = sorted(dates, reverse=True)[:days]

    def _read(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    days_list = []
    for d in dates:
        day = {"日期": d, "报告": []}

        # 盘前分析
        jp = os.path.join(rdir, f"{d}_pre_market.json")
        mp = os.path.join(rdir, f"{d}_pre_market.md")
        if os.path.exists(jp) or os.path.exists(mp):
            summary = ""
            if os.path.exists(jp):
                try:
                    j = json.load(open(jp, encoding="utf-8"))
                    plan = j.get("一句话计划", "") or ""
                    risk = j.get("风险等级", "") or ""
                    if isinstance(risk, dict):
                        risk = risk.get("风险等级", "") or risk.get("仓位区间", "") or ""
                    if not isinstance(plan, str):
                        plan = json.dumps(plan, ensure_ascii=False)
                    summary = (f"[{risk}] " if risk else "") + str(plan)
                except Exception:
                    pass
            day["报告"].append({
                "类型": "pre_market", "图标": "🌅", "标题": "盘前分析",
                "生成时间": _report_gen_time(jp),
                "摘要": summary[:140],
                "markdown": _read(mp),
            })

        # 收盘全复盘
        jp = os.path.join(rdir, f"{d}_post_market.json")
        mp = os.path.join(rdir, f"{d}_post_market.md")
        if os.path.exists(jp) or os.path.exists(mp):
            summary = ""
            if os.path.exists(jp):
                try:
                    j = json.load(open(jp, encoding="utf-8"))
                    me = j.get("市场环境") or {}
                    if isinstance(me, dict):
                        summary = f"{me.get('环境', '')} | {me.get('结论', '')}".strip(" |")
                except Exception:
                    pass
            day["报告"].append({
                "类型": "post_market", "图标": "🌆", "标题": "收盘全复盘",
                "生成时间": _report_gen_time(jp),
                "摘要": summary[:140],
                "markdown": _read(mp),
            })

        # 午间模拟盘
        sp = os.path.join(rdir, f"sim_{d}.md")
        if os.path.exists(sp):
            day["报告"].append({
                "类型": "sim", "图标": "📈", "标题": "午间模拟盘",
                "生成时间": _file_mtime(sp),
                "摘要": "",
                "markdown": _read(sp),
            })

        if day["报告"]:
            days_list.append(day)

    today = datetime.now().strftime("%Y-%m-%d")
    has_today = any(x["日期"] == today for x in days_list)
    return {"status": "OK", "today": today, "has_today": has_today,
            "days": days_list, "总天数": len(days_list)}


def get_journal():
    """真实交易记录 + 持仓聚合"""
    jpath = os.path.join(ROOT, "data", "trade_journal.json")
    if not os.path.exists(jpath):
        return {"status": "EMPTY", "records": [], "holdings": []}
    with open(jpath, "r", encoding="utf-8") as f:
        records = json.load(f)
    # 聚合当前持仓(按代码: 买-卖 汇总)
    holdings_map = {}
    for rec in records:
        code = rec.get("代码", "")
        side = rec.get("方向", "")
        qty = rec.get("股数") or rec.get("手数") or 0
        price = rec.get("成交均价") or 0
        name = rec.get("名称", code)
        unit = rec.get("单位", "手")
        if code not in holdings_map:
            holdings_map[code] = {"代码": code, "名称": name, "单位": unit,
                                  "数量": 0, "成本": 0, "方向": side}
        h = holdings_map[code]
        if side == "buy":
            h["数量"] += qty
            h["成本"] = (h["成本"] * (h["数量"] - qty) + price * qty) / h["数量"] if h["数量"] > 0 else price
        elif side == "sell":
            h["数量"] -= qty
    holdings = [v for v in holdings_map.values() if v["数量"] > 0]
    return {"status": "OK", "records": records[-50:], "holdings": holdings}


# ===================== HTTP 服务 =====================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send(self, obj, code=200):
        body = json.dumps(_json_safe(obj), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path, code=200):
        """发送静态 HTML(工作台页面)"""
        full = os.path.join(ROOT, path.lstrip("/"))
        if not os.path.isfile(full):
            self._send({"status": "ERROR", "msg": f"文件不存在: {path}"}, 404)
            return
        with open(full, "r", encoding="utf-8") as f:
            body = f.read().encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")  # 防浏览器缓存旧版
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/health":
                self._send({"status": "OK", "app": "ahao-dashboard", "time": __import__("datetime").datetime.now().isoformat()})
            elif u.path == "/api/snapshot":
                self._send(get_snapshot())
            elif u.path == "/api/quote":
                codes = q.get("codes", [""])[0].split(",")
                self._send({"status": "OK", "quotes": get_quotes(codes)})
            elif u.path == "/api/stock":
                code = q.get("code", [""])[0]
                if not code:
                    self._send({"status": "ERROR", "msg": "缺少 code 参数"}, 400)
                else:
                    self._send({"status": "OK", "detail": get_stock(code)})
            elif u.path == "/api/kline":
                code = q.get("code", [""])[0]
                count = int(q.get("count", ["120"])[0])
                if not code:
                    self._send({"status": "ERROR", "msg": "缺少 code 参数"}, 400)
                else:
                    self._send(get_kline(code, count))
            elif u.path == "/api/sim":
                self._send(get_sim())
            elif u.path == "/api/journal":
                self._send(get_journal())
            elif u.path == "/api/news":
                ps = int(q.get("size", ["300"])[0])
                self._send(get_news_sentiment(ps))
            elif u.path == "/api/reports":
                dy = int(q.get("days", ["7"])[0])
                self._send(get_reports(dy))
            elif u.path == "/api/diag":
                code = q.get("code", [""])[0]
                if not code:
                    self._send({"status": "ERROR", "msg": "缺少 code 参数"}, 400)
                else:
                    from utils.diag import get_diag
                    self._send(get_diag(code))
            elif u.path == "/api/risk":
                self._send(get_risk_overview())
            elif u.path in ("/", "/index.html"):
                # 同源打开工作台(解决 file:// 打开时的跨域/混合内容拦截)
                self._send_html("trading_workbench.html")
            else:
                self._send({"status": "ERROR", "msg": f"未知路径 {u.path}"}, 404)
        except Exception as e:
            self._send({"status": "ERROR", "msg": f"{type(e).__name__}: {str(e)[:200]}"}, 500)


# 多线程服务器: 单个慢请求(如K线拉取超时)不阻塞 health/其它请求
class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="AS 交易工作台数据桥")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    print(f"🌉 AS 数据桥已启动: http://127.0.0.1:{args.port}")
    print(f"   📊 打开工作台: http://127.0.0.1:{args.port}/  (推荐, 同源无跨域)")
    print(f"   大盘快照: http://127.0.0.1:{args.port}/api/snapshot")
    print(f"   个股K线:  http://127.0.0.1:{args.port}/api/kline?code=600519")
    print(f"   模拟盘:   http://127.0.0.1:{args.port}/api/sim")
    print("   Ctrl+C 停止")
    ThreadedServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
