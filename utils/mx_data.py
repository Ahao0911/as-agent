"""
妙想金融数据源封装（东方财富妙想 MXSkills，2026-08-03 接入）
数据源优先级：腾讯/mootdx（行情） → 妙想mx（增强） → iFinD（受控增强）
用途：自然语言查询东财权威数据库（行情/财务/资讯/选股），适合 Agent 盘前/盘后分析补充

API Key: 环境变量 MX_APIKEY（已写入 ~/.bashrc）
端点: mkapi2.dfcfs.com（mx-data=query / mx-search=news-search / mx-xuangu=选股 / mx-zixuan=自选 / mx-moni=组合 / mx-poster=社区）
"""
import os
import subprocess
import json
import sys

MX_SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data-sources-mx")
MX_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mx_output")


def _load_api_key():
    """获取MX_APIKEY：环境变量优先 → ~/.hermes/.env → ~/.bashrc 兜底（新会话/ cron 自动生效）"""
    key = os.getenv("MX_APIKEY", "").strip()
    if key:
        return key
    # AS .env（每次启动加载）
    for env_path in ("~/.hermes/.env", "~/.hermes/profiles/default/.env"):
        p = os.path.expanduser(env_path)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("MX_APIKEY="):
                            key = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if key:
                                return key
            except Exception:
                pass
    # .bashrc 兜底
    bashrc = os.path.expanduser("~/.bashrc")
    if os.path.exists(bashrc):
        try:
            with open(bashrc, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export MX_APIKEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if key:
                            return key
        except Exception:
            pass
    return ""


MX_APIKEY = _load_api_key()

_SCRIPT_MAP = {
    "data": "mx-data/mx_data.py",        # 金融数据查询（行情/财务/关系）
    "search": "mx-search/mx_search.py",  # 资讯搜索
    "xuangu": "mx-xuangu/mx_xuangu.py",  # 智能选股
    "zixuan": "mx-zixuan/mx_zixuan.py",  # 自选股管理
    "moni": "mx-moni/mx_moni.py",        # 模拟组合管理
    "poster": "mx-poster/mx_poster.py",  # AI社区
}


def _available():
    """检查 API Key 是否配置"""
    if not MX_APIKEY:
        return False, "MX_APIKEY 未配置（~/.bashrc 未设置或会话未加载）"
    return True, ""


def call(kind="data", query="", timeout=60):
    """
    调用妙想技能脚本（自然语言查询）
    kind: data/search/xuangu/zixuan/moni/poster
    query: 自然语言问句，如 "贵州茅台最新价 涨跌幅"
    返回: {"ok": bool, "output": str, "error": str, "raw_files": [...]}
    """
    ok, err = _available()
    if not ok:
        return {"ok": False, "output": "", "error": err, "raw_files": []}
    script = _SCRIPT_MAP.get(kind)
    if not script:
        return {"ok": False, "output": "", "error": f"未知技能类型: {kind}", "raw_files": []}
    script_path = os.path.join(MX_SKILL_DIR, script)
    if not os.path.exists(script_path):
        return {"ok": False, "output": "", "error": f"技能脚本不存在: {script_path}", "raw_files": []}
    try:
        os.makedirs(MX_OUTPUT_DIR, exist_ok=True)
        env = dict(os.environ)
        env["MX_APIKEY"] = MX_APIKEY
        # mx-xuangu 参数格式与其他不同：--output-dir 需显式传
        if kind == "xuangu":
            cmd = [sys.executable, script_path, query, "--output-dir", MX_OUTPUT_DIR]
        else:
            cmd = [sys.executable, script_path, query, MX_OUTPUT_DIR]
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        out = out.strip()
        # 收集生成的文件（同前缀）
        qname = "".join(c if c.isalnum() or c in "_-" else "_" for c in query[:20])
        raw_files = []
        if os.path.isdir(MX_OUTPUT_DIR):
            for fn in os.listdir(MX_OUTPUT_DIR):
                if qname in fn:
                    raw_files.append(os.path.join(MX_OUTPUT_DIR, fn))
        if r.returncode != 0:
            return {"ok": False, "output": out, "error": f"脚本退出码 {r.returncode}", "raw_files": raw_files}
        # 失败检测只看开头3行（脚本的显式错误输出都在头部，正文可能含"异常波动"等词）
        head = "\n".join(out.splitlines()[:3])
        if "错误" in head or "失败" in head:
            return {"ok": False, "output": out, "error": out[:200], "raw_files": raw_files}
        return {"ok": True, "output": out, "error": "", "raw_files": raw_files}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": f"妙想API超时({timeout}s)", "raw_files": []}
    except Exception as e:
        return {"ok": False, "output": "", "error": f"妙想调用异常: {str(e)[:120]}", "raw_files": []}


# ===================== 项目数据源集成接口 =====================

def query_quote(symbol_query):
    """行情查询（自然语言），如 '京东方A最新价 涨跌幅 换手率'"""
    return call("data", symbol_query)


def query_news(keywords, days=1, size=5):
    """资讯搜索（自然语言），如 '半导体设备板块最新消息'"""
    return call("search", keywords)


def query_finance(symbol_query):
    """财务数据查询，如 '贵州茅台近三年净利润 营业收入'"""
    return call("data", symbol_query)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="妙想数据源封装测试")
    parser.add_argument("kind", nargs="?", default="data", choices=list(_SCRIPT_MAP.keys()))
    parser.add_argument("query", nargs="?", default="贵州茅台最新价 涨跌幅")
    args = parser.parse_args()
    r = call(args.kind, args.query)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str)[:2000])
