"""
同花顺 iFinD MCP 查询封装 — 走 AS skill 的 call-node.js
试用密钥已配置在 ~/.hermes/skills/ifind-finance-data/mcp_config.json

用法:
    python3 utils/ths_mcp.py summary "贵州茅台600519最新估值"
    python3 utils/ths_mcp.py wencai "A股半导体板块换手率大于5%的股票"
    python3 utils/ths_mcp.py news "AI芯片 最新消息"
    python3 utils/ths_mcp.py tools
"""
import sys
import os
import json
import subprocess

SKILL_DIR = os.path.expanduser("~/.hermes/skills/ifind-finance-data")

# server_type 映射: 数据服务类型
SERVERS = {
    "summary": "stock",      # A股摘要
    "wencai": "stock",       # 智能选股
    "news": "news",          # 新闻公告
    "fund": "fund",          # 基金
    "index": "index",        # 指数板块
    "edb": "edb",            # 宏观行业
    "global": "global_stock",  # 港美股
    "bond": "bond",          # 债券
}


def call(server_type, tool_name, params):
    """调用 iFinD MCP（node脚本）"""
    script = f"""
const {{ call }} = require('{SKILL_DIR}/call-node.js');
(async () => {{
  try {{
    const r = await call('{server_type}', '{tool_name}', {json.dumps(params, ensure_ascii=False)});
    console.log(JSON.stringify(r));
  }} catch (e) {{ console.log(JSON.stringify({{error: e.message}})); }}
}})();
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"error": r.stderr[:300]}
    try:
        return json.loads(r.stdout.strip().split("\n")[-1])
    except json.JSONDecodeError:
        return {"error": r.stdout[-500:]}


def _parse_result(r):
    """统一解析 iFinD 返回，兼容多种结构：dict/嵌套str/list/markdown文本"""
    try:
        text = r["data"]["result"]["content"][0]["text"]
        d = json.loads(text)
        # 深度搜索 answer 字段（逐层）
        seen = 0
        while isinstance(d, dict) and seen < 5:
            seen += 1
            if "answer" in d and d["answer"]:
                return d["answer"]
            if isinstance(d.get("data"), str):
                inner = d["data"]
                try:
                    d = json.loads(inner)
                except Exception:
                    return inner
            elif isinstance(d.get("data"), dict):
                d = d["data"]
            else:
                break
        if isinstance(d, dict):
            return d.get("answer", json.dumps(d, ensure_ascii=False))
        return str(d)
    except Exception:
        return ""


def _mx_first(query, mx_kind, ths_fn):
    """妙想优先 → iFinD兜底（2026-08-03：东财每天免费额度，iFinD总额度2000次省着用）
    mx_kind: mx-data 的 kind（data=行情/财务, xuangu=选股, search=资讯）
    ths_fn: 无参调用 iFinD 的闭包
    """
    try:
        from utils.mx_data import call as mx_call
        r = mx_call(mx_kind, query)
        if r.get("ok") and r.get("output"):
            return {"source": "mx妙想", "text": r["output"], "raw_files": r.get("raw_files", [])}
    except Exception:
        pass
    return {"source": "iFinD", "text": ths_fn()}


def summary(query):
    """A股摘要查询（估值/行情/财务/股东等）：妙想优先 → iFinD兜底"""
    return _mx_first(query, "data", lambda: _parse_result(call("stock", "get_stock_summary", {"query": query})))


def search_stocks(query):
    """智能选股（问财）：妙想选股优先 → iFinD兜底"""
    return _mx_first(query, "xuangu", lambda: _parse_result(call("stock", "search_stocks", {"query": query})))


def news(query, days=7, size=5):
    """新闻公告搜索（默认最近7天）：妙想优先 → iFinD兜底"""
    def _ths():
        from datetime import datetime, timedelta
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return _parse_result(call("news", "search_news", {
            "query": query, "time_start": start, "time_end": end, "size": size
        }))
    return _mx_first(query, "search", _ths)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "tools":
        # 列出工具
        script = f"const {{ listTools }} = require('{SKILL_DIR}/call-node.js'); (async () => {{ const r = await listTools('stock'); console.log(JSON.stringify(r)); }})();"
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
        print(r.stdout[:1000])
        return

    query = " ".join(args[1:])
    if cmd == "summary":
        print(json.dumps(summary(query), ensure_ascii=False, indent=2)[:1500])
    elif cmd == "wencai":
        print(json.dumps(search_stocks(query), ensure_ascii=False, indent=2)[:1500])
    elif cmd == "news":
        print(json.dumps(news(query), ensure_ascii=False, indent=2)[:1500])
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
