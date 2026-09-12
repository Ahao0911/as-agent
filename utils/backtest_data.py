# -*- coding: utf-8 -*-
"""
回测长历史数据层（T02-1 第一阻塞项）
====================================
目标：绕开 ``utils/screen_bull.py::fetch_kline()`` 里写死的 ``start_date="20250101"``，
      把回测数据窗口从 2024-01-01 放宽到 2018-01-01（可配），并持久化到 ``data/kline_cache.db``。

数据源优先级（★2026-09 实测结论，见文件末尾「实测记录」）：
  1. 本地 ``kline_cache.db``（表 ``klines``）命中且覆盖足够 → 直接用，零网络；
  2. akshare 新浪源 ``stock_zh_a_daily``（可给 2018 起 2110 根，代理环境下可用）；
     先尝试 akshare 东财源 ``stock_zh_a_hist``（若网络允许亦可用）；
  3. 全部失败 → 返回 None（**绝不伪造数据**）。

为什么主源是新浪而非东财：
  - 本机 HTTP(S)_PROXY = http://127.0.0.1:4578，东财域（push2his/push2.eastmoney.com）
    经代理返回 ``ProxyError``，直连返回 ``RemoteDisconnected``，两条路都不通；
  - 新浪域（money.finance.sina.com.cn / finance.sina.com.cn）经代理可用，
    ``stock_zh_a_daily(symbol='sh600519', start_date='20180101', adjust='qfq')``
    实测返回 2110 根（2018-01-02 ~ 2026-09-10），与架构师目标完全一致。

对外接口：
  - get_hist(code, start, end, adjust) -> DataFrame | None   单只标准列长历史
  - get_universe_full(market, exclude_st, include_bj) -> list[str]  全市场股票池（含双创，排除 ST）
  - get_universe_names(codes) -> dict[str, str]              代码 → 名称映射
  - get_benchmark(start, end) -> DataFrame                   沪深300 基准
  - load_universe_klines(codes, start, ...) -> dict[str, DataFrame]   批量加载
  - ensure_cached(codes, start, ...) -> int                  确保缓存覆盖，返回新写根数
  - coverage_report(codes, start) -> DataFrame               开工前体检

依赖：pandas / numpy / akshare（均已存在），不引入任何新包。
"""
import os
import sys
import time
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

# ===================== 常量 =====================

DEFAULT_START = "2018-01-01"
DEFAULT_ADJUST = "qfq"

# 标准输出列
STD_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]

# kline_cache.db 路径（与 utils/kline_cache.py 保持一致）
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "kline_cache.db")

# 限速参数（akshare 高频会被限流/封 IP）
_AK_DELAY_MIN = 0.20
_AK_DELAY_MAX = 0.35
_AK_RETRY = 3

# 全市场板块前缀（6 位代码）
#   沪市主板 60xxxx       深市主板 00xxxx
#   创业板   30xxxx       科创板   688xxx
#   北交所   8xxxxx / 4xxxxx（默认不纳入）
MAIN_PREFIXES = ("60", "00")
GEM_PREFIX = "30"      # 创业板（双创之一）
STAR_PREFIX = "688"    # 科创板（双创之一）
BJ_PREFIXES = ("83", "87", "88", "43", "92", "82", "420", "8", "4")  # 北交所/老三板

# ST / 退市 关键词（名称过滤）
ST_KEYWORDS = ("ST", "*ST", "退")


# ===================== 内部工具 =====================

def _clear_proxy():
    """清空代理环境变量（项目既有约定，见 utils/stock_data.py::_clear_proxy）。

    本机代理会拦截部分数据源；先清代理再尝试直连，失败则由上层回退。
    注意：新浪源在本机**经代理**可用（代理对 sina 放行），故此处只作为备选路径，
    并非强制。
    """
    for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"]:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"


def _random_delay(min_sec=_AK_DELAY_MIN, max_sec=_AK_DELAY_MAX):
    """随机休眠，降低被限流概率。"""
    time.sleep(random.uniform(min_sec, max_sec))


def _to_sina_symbol(code):
    """6 位代码 → 新浪/akshare symbol（带市场前缀）。

    规则（与 utils/data_router._symbol_to_tencent 对齐并补充北交所）：
      6 / 9 开头      → sh
      688 开头        → sh（科创板，6 开头已覆盖）
      0 / 3 开头      → sz（深主板 / 创业板）
      4 / 8 开头      → bj（北交所 / 老三板）
    """
    code = str(code).strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    # 兜底：按深市处理
    return f"sz{code}"


def _parse_ak_date(value):
    """解析 akshare 各种日期字段 → 'YYYY-MM-DD' 字符串。"""
    if value is None:
        return None
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)[:10]


def _normalize_ak_hist(df, code, adjust):
    """把 akshare 东财 ``stock_zh_a_hist`` 结果规整为标准列 DataFrame。

    东财中文列：日期/开盘/收盘/最高/最低/成交量/成交额
    返回列：date/open/high/low/close/volume/amount，升序，date 为 datetime。
    """
    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
        "最低": "low", "成交量": "volume", "成交额": "amount",
    }
    out = pd.DataFrame()
    for src, dst in col_map.items():
        if src in df.columns:
            out[dst] = df[src]
    if "date" not in out.columns or "close" not in out.columns:
        return None
    out["date"] = pd.to_datetime(out["date"])
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c not in out.columns:
            out[c] = pd.NA
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out[STD_COLUMNS].dropna(subset=["close"]).sort_values("date")
    out = out.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    return out if len(out) else None


def _normalize_ak_daily(df, code, adjust):
    """把 akshare 新浪 ``stock_zh_a_daily`` 结果规整为标准列 DataFrame。

    新浪英文列：date/open/high/low/close/volume/amount（可选 outstanding_share/turnover）
    """
    if df is None or len(df) == 0:
        return None
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c not in out.columns:
            out[c] = pd.NA
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out[STD_COLUMNS].dropna(subset=["close"]).sort_values("date")
    out = out.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    return out if len(out) else None


# ===================== 本地缓存读写（沿用既有表结构，不改 schema） =====================

def _db_conn():
    """连接 kline_cache.db（表结构由 utils/kline_cache.py 初始化）。"""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        # 二次确认表存在
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='klines'")
        if cur.fetchone() is None:
            conn.close()
            return None
        return conn
    except Exception:
        return None


def load_cached(code, adjust=DEFAULT_ADJUST, start=None, end=None):
    """从本地 DB 读取某只代码的缓存K线（标准列，升序）。

    表 klines 的 symbol 列为 6 位代码（如 '600519'），adjustment 为 'qfq'/'raw'。
    过滤条件 start/end（'YYYY-MM-DD'）可直接下发到 SQL。
    """
    conn = _db_conn()
    if conn is None:
        return None
    code = str(code).strip().zfill(6)
    sql = ("SELECT date, open, high, low, close, volume, amount "
           "FROM klines WHERE symbol=? AND adjustment=?")
    params = [code, adjust]
    if start:
        sql += " AND date >= ?"
        params.append(str(start)[:10])
    if end:
        sql += " AND date <= ?"
        params.append(str(end)[:10])
    sql += " ORDER BY date"
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        conn.close()
        return None
    conn.close()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=STD_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)


def save_to_cache(code, df, adjust=DEFAULT_ADJUST, source="akshare_sina"):
    """把标准列 DataFrame 回写本地 DB（按既有 schema，``INSERT OR REPLACE`` 去重）。

    表结构（utils/kline_cache.py::SCHEMA，**不改动**）：
        symbol, date, adjustment, open, high, low, close, volume, amount, source, fetched_at
    返回写入行数。
    """
    if df is None or len(df) == 0:
        return 0
    # 确保表存在（首次运行时由 kline_cache 建表；此处兜底）
    try:
        from utils.kline_cache import _conn as _kc_conn
        _kc_conn().close()
    except Exception:
        pass

    conn = _db_conn()
    if conn is None:
        return 0
    code = str(code).strip().zfill(6)
    fetched = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append((
                code,
                _parse_ak_date(r["date"]),
                adjust,
                float(r["open"]) if pd.notna(r.get("open")) else None,
                float(r["high"]) if pd.notna(r.get("high")) else None,
                float(r["low"]) if pd.notna(r.get("low")) else None,
                float(r["close"]) if pd.notna(r.get("close")) else None,
                float(r["volume"]) if pd.notna(r.get("volume")) else None,
                float(r["amount"]) if pd.notna(r.get("amount")) else None,
                source,
                fetched,
            ))
        except Exception:
            continue
    if not rows:
        conn.close()
        return 0
    try:
        conn.executemany("""
            INSERT OR REPLACE INTO klines
            (symbol, date, adjustment, open, high, low, close, volume, amount, source, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        conn.commit()
    except Exception:
        conn.close()
        return 0
    conn.close()
    return len(rows)


# ===================== 单只长历史拉取 =====================

def _fetch_from_akshare(code, start, end, adjust):
    """从 akshare 拉取长历史。

    先试东财 ``stock_zh_a_hist``（若网络允许），失败再试新浪 ``stock_zh_a_daily``。
    两者都失败返回 None（不抛异常，交由上层回退/报错）。
    """
    try:
        import akshare as ak
    except Exception:
        return None, None

    sym = _to_sina_symbol(code)
    plain = str(code).strip().zfill(6)
    s = str(start).replace("-", "") if start else "20180101"
    e = str(end).replace("-", "") if end else pd.Timestamp.now().strftime("%Y%m%d")

    # --- 源 A: 东财 stock_zh_a_hist ---
    for attempt in range(_AK_RETRY):
        try:
            df = ak.stock_zh_a_hist(symbol=plain, period="daily",
                                    start_date=s, end_date=e, adjust=adjust)
            norm = _normalize_ak_hist(df, code, adjust)
            if norm is not None and len(norm) > 0:
                return norm, "akshare_em"
        except Exception:
            pass
        _random_delay(0.2, 0.4)

    # --- 源 B: 新浪 stock_zh_a_daily（代理环境下实测可用）---
    for attempt in range(_AK_RETRY):
        try:
            df = ak.stock_zh_a_daily(symbol=sym, start_date=s, end_date=e, adjust=adjust)
            norm = _normalize_ak_daily(df, code, adjust)
            if norm is not None and len(norm) > 0:
                return norm, "akshare_sina"
        except Exception:
            pass
        # 指数退避
        time.sleep((attempt + 1) * 1.0)

    return None, None


def get_hist(code, start=DEFAULT_START, end=None, adjust=DEFAULT_ADJUST,
             use_cache=True, write_cache=True):
    """获取单只标的的长历史日K。

    数据源优先级：
      1. 本地 ``kline_cache.db`` 命中且**覆盖足够** → 直接用；
      2. akshare（东财 → 新浪）拉取 → 回写缓存；
      3. 全失败 → None（**不伪造数据**）。

    Args:
        code: 6 位代码（如 '600519'）。
        start: 起始日 'YYYY-MM-DD'，默认 2018-01-01。
        end: 结束日 'YYYY-MM-DD'，None = 至今。
        adjust: 'qfq'（前复权，默认）/ 'hfq' / ''（不复权）。
        use_cache: 是否优先读本地缓存。
        write_cache: 拉取成功后是否回写本地缓存。

    Returns:
        标准列 DataFrame（date/open/high/low/close/volume/amount），升序，date 为 datetime；
        或 None（数据不可用）。
    """
    start = start or DEFAULT_START
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.now()

    # --- 1. 本地缓存优先（判"覆盖足够"：首日 <= start + 20 个自然日缓冲）---
    if use_cache:
        cached = load_cached(code, adjust=adjust, start=start, end=end)
        if cached is not None and len(cached) > 0:
            first = cached["date"].min()
            covered = first <= (start_ts + pd.Timedelta(days=20))
            enough_bars = len(cached) >= 120
            if covered and enough_bars:
                return cached

    # --- 2. 网络拉取 ---
    fetched, source = _fetch_from_akshare(code, start, end, adjust)

    # --- 3. 拉取失败时，回退到"不够长但存在"的本地缓存 ---
    if fetched is None or len(fetched) == 0:
        if use_cache:
            stale = load_cached(code, adjust=adjust, start=start, end=end)
            if stale is not None and len(stale) >= 120:
                return stale
        return None

    # --- 4. 回写缓存 ---
    if write_cache:
        try:
            save_to_cache(code, fetched, adjust=adjust,
                          source=source or "akshare")
        except Exception:
            pass

    # 按 start/end 精确裁剪（数据源可能多返回）
    fetched = fetched[(fetched["date"] >= start_ts) & (fetched["date"] <= end_ts)]
    return fetched.reset_index(drop=True) if len(fetched) else None


# ===================== 股票池（全市场 + 排除 ST） =====================

def _is_st_name(name):
    """判断名称是否为 ST / *ST / 退市 股。"""
    if not name:
        return False
    up = str(name).upper().replace(" ", "")
    for kw in ST_KEYWORDS:
        if kw.upper() in up:
            return True
    return False


def _classify_board(code):
    """按 6 位代码判板块：sh_main / sz_main / gem / star / bj / other。"""
    code = str(code).zfill(6)
    if code.startswith("688"):
        return "star"        # 科创板
    if code.startswith("60"):
        return "sh_main"     # 沪市主板
    if code.startswith("00"):
        return "sz_main"     # 深市主板
    if code.startswith("30"):
        return "gem"         # 创业板
    if code.startswith(("43", "83", "87", "88", "92", "82")):
        return "bj"          # 北交所
    return "other"


def _board_included(code, include_bj=False):
    """是否属于目标全市场（沪主板 + 深主板 + 创业板 + 科创板；北交所可选）。"""
    board = _classify_board(code)
    if board in ("sh_main", "sz_main", "gem", "star"):
        return True
    if board == "bj" and include_bj:
        return True
    return False


def get_code_name_map():
    """获取全市场 代码 → 名称 映射。

    主源：akshare ``stock_info_a_code_name``（新浪，代理环境实测可用，5561 只）。
    返回 {code6: name}；失败返回 {}（**不伪造**）。
    """
    try:
        import akshare as ak
        for attempt in range(_AK_RETRY):
            try:
                df = ak.stock_info_a_code_name()
                if df is not None and len(df) > 100:
                    col_code = "code" if "code" in df.columns else df.columns[0]
                    col_name = "name" if "name" in df.columns else df.columns[1]
                    out = {}
                    for _, row in df.iterrows():
                        c = str(row[col_code]).strip().zfill(6)
                        if len(c) != 6:
                            continue
                        out[c] = str(row[col_name]).strip()
                    if out:
                        return out
            except Exception:
                pass
            time.sleep((attempt + 1) * 1.0)
    except Exception:
        pass
    return {}


def get_universe_full(market="all", exclude_st=True, include_bj=False):
    """构建全市场股票池。

    Args:
        market: "all" = 全市场（沪主板 60 + 深主板 00 + 创业板 30 + 科创板 688，含双创）。
                其他值当前等价 "all"（预留扩展）。
        exclude_st: 是否排除 ST / *ST / 退市 股（默认 True）。
        include_bj: 是否纳入北交所（8/4 开头）。**默认 False**
                    （北交所数据质量与流动性问题，经 team-lead 确认先不纳入）。

    Returns:
        6 位代码列表（如 ['000001', '600519', ...]）。
        名称来源：akshare ``stock_info_a_code_name``（新浪）。
        若名称源不可用 → 返回空列表（**不伪造数据**），并打印告警。
    """
    code2name = get_code_name_map()
    if not code2name:
        print("[backtest_data] ⚠️ 股票名称清单获取失败（akshare 不可用），"
              "get_universe_full 返回空列表。不伪造数据。")
        return []

    total_before = 0
    total_after = 0
    board_stats = {"sh_main": 0, "sz_main": 0, "gem": 0, "star": 0, "bj": 0, "other": 0}
    excluded_st = 0
    excluded_bj = 0

    result = []
    for code, name in code2name.items():
        board = _classify_board(code)
        if board not in board_stats:
            board_stats[board] = 0

        # 板块过滤：仅纳入目标板块
        if not _board_included(code, include_bj=include_bj):
            if board == "bj":
                excluded_bj += 1
            continue

        total_before += 1
        # ST 过滤
        if exclude_st and _is_st_name(name):
            excluded_st += 1
            continue

        result.append(code)
        board_stats[board] = board_stats.get(board, 0) + 1
        total_after += 1

    result = sorted(set(result))

    # 统计输出（便于报工）
    print(f"[backtest_data] 全市场股票池: 纳入 {len(result)} 只 "
          f"(板块: 沪主板 {board_stats.get('sh_main', 0)} / "
          f"深主板 {board_stats.get('sz_main', 0)} / "
          f"创业板 {board_stats.get('gem', 0)} / "
          f"科创板 {board_stats.get('star', 0)})")
    print(f"[backtest_data]   板块过滤: 排除北交所 {excluded_bj} 只 "
          f"(include_bj={include_bj}); 排除 ST/退市 {excluded_st} 只 "
          f"(exclude_st={exclude_st}); 过滤前目标板块 {total_before} 只")

    return result


def get_universe_names(codes):
    """批量取代码 → 名称（用于回测报告展示）。失败项名称回退为代码本身。"""
    code2name = get_code_name_map()
    return {c: code2name.get(str(c).zfill(6), str(c)) for c in codes}


# ===================== 基准（沪深300） =====================

def get_benchmark(start=DEFAULT_START, end=None):
    """沪深300 基准日线。

    主源：akshare ``stock_zh_index_daily('sh000300')``（新浪，实测 5990 根，2002 起）。
    返回标准列 DataFrame（date/open/high/low/close/volume/amount），升序；
    失败返回空 DataFrame（**不伪造**）。
    """
    import akshare as ak
    start_ts = pd.Timestamp(start or DEFAULT_START)
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.now()

    for attempt in range(_AK_RETRY):
        try:
            df = ak.stock_zh_index_daily(symbol="sh000300")
            if df is not None and len(df):
                out = df.copy()
                out["date"] = pd.to_datetime(out["date"])
                for c in ["open", "high", "low", "close", "volume", "amount"]:
                    if c not in out.columns:
                        out[c] = pd.NA
                    out[c] = pd.to_numeric(out[c], errors="coerce")
                out = out[STD_COLUMNS].dropna(subset=["close"]).sort_values("date")
                out = out[(out["date"] >= start_ts) & (out["date"] <= end_ts)]
                return out.reset_index(drop=True)
        except Exception:
            pass
        time.sleep((attempt + 1) * 1.0)
    return pd.DataFrame(columns=STD_COLUMNS)


# ===================== 批量加载 / 缓存体检 =====================

def load_universe_klines(codes, start=DEFAULT_START, end=None,
                         adjust=DEFAULT_ADJUST, workers=4, refresh=False):
    """批量加载多只标的的长历史。

    本地 DB 优先（覆盖足够）→ 缺失部分 akshare 拉取 → 回写 DB。
    ``refresh=True`` 时强制重拉（跳过本地缓存读取，但仍会回写）。

    Args:
        codes: 代码列表。
        start/end/adjust: 同 get_hist。
        workers: 并发线程数（**注意 akshare 限流，建议 <= 4**）。
        refresh: 强制重拉。

    Returns:
        {code: DataFrame}，仅含成功（>=120 根）的标的。
    """
    codes = [str(c).zfill(6) for c in codes]
    result = {}

    def _one(code):
        return code, get_hist(code, start=start, end=end, adjust=adjust,
                              use_cache=not refresh, write_cache=True)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        done = 0
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                c, df = fut.result()
                if df is not None and len(df) >= 120:
                    result[c] = df
            except Exception:
                pass
            done += 1

    return result


def ensure_cached(codes, start=DEFAULT_START, end=None,
                  adjust=DEFAULT_ADJUST, workers=4):
    """确保 codes 的本地缓存覆盖 [start, end]，返回新写入的根数。

    等价于批量调用 ``get_hist(...)`` 并累计 ``save_to_cache`` 的写入量。
    """
    codes = [str(c).zfill(6) for c in codes]
    stats = {"written": 0, "ok": 0, "fail": 0}

    def _one(code):
        df = get_hist(code, start=start, end=end, adjust=adjust,
                      use_cache=True, write_cache=True)
        if df is None or len(df) < 120:
            return code, 0, False
        return code, len(df), True

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for fut in as_completed(futs):
            try:
                code, n, ok = fut.result()
                if ok:
                    stats["ok"] += 1
                    stats["written"] += n
                else:
                    stats["fail"] += 1
            except Exception:
                stats["fail"] += 1

    print(f"[backtest_data] ensure_cached: 成功 {stats['ok']} / 失败 {stats['fail']} "
          f"/ 共处理 {len(codes)} 只；累计覆盖 {stats['written']} 根")
    return stats["written"]


def coverage_report(codes, start=DEFAULT_START, adjust=DEFAULT_ADJUST):
    """开工前体检：每只的 bars / 起始日 / 是否满足 start（读本地缓存）。

    Returns:
        DataFrame[symbol, bars, first_date, last_date, meets_start]
    """
    start_ts = pd.Timestamp(start or DEFAULT_START)
    rows = []
    for c in codes:
        code = str(c).zfill(6)
        df = load_cached(code, adjust=adjust)
        if df is None or len(df) == 0:
            rows.append({"symbol": code, "bars": 0, "first_date": None,
                         "last_date": None, "meets_start": False})
            continue
        first = df["date"].min()
        rows.append({
            "symbol": code,
            "bars": int(len(df)),
            "first_date": first.strftime("%Y-%m-%d"),
            "last_date": df["date"].max().strftime("%Y-%m-%d"),
            "meets_start": bool(first <= (start_ts + pd.Timedelta(days=20))),
        })
    return pd.DataFrame(rows)


# ===================== 实测记录（供后人参考，勿删） =====================
# 2026-09 实测（Windows，HTTP(S)_PROXY=http://127.0.0.1:4578）:
#   ❌ ak.stock_zh_a_hist(东财)          → ProxyError（经代理）/ RemoteDisconnected（直连）
#   ❌ ak.stock_zh_a_spot_em(东财)       → ProxyError（82.push2.eastmoney.com）
#   ❌ ak.index_zh_a_hist(东财)          → ProxyError（80.push2.eastmoney.com）
#   ✅ ak.stock_zh_a_daily(新浪)         → 600519 @2018 → 2110 根（2018-01-02~2026-09-10）
#                                          sz300750 → 2005 根 / sh688111 → 1655 根
#   ✅ ak.stock_info_a_code_name(新浪)   → 5561 只 (code, name)（~43s）
#   ✅ ak.stock_zh_index_daily(sh000300) → 5990 根（2002-01-04~2026-09-10）
#   ❌ 腾讯 web.ifzq.gtimg.cn 日K        → 上限 ~641 根（1000/2000 请求均返回 641）
#   ✅ 新浪 getKLineData datalen=3000    → 3000 根（2014 起，不复权）
# 结论：**主源改用新浪 stock_zh_a_daily**，东财作为"网络允许时"的优先尝试。

if __name__ == "__main__":
    # 自检
    print("=" * 70)
    print("backtest_data 自检")
    print("=" * 70)

    df = get_hist("600519")
    if df is not None:
        print(f"[1] get_hist('600519'): {len(df)} 根, "
              f"{df['date'].min().date()} ~ {df['date'].max().date()}")
    else:
        print("[1] get_hist('600519'): 失败（数据源不可用）")

    uni = get_universe_full()
    print(f"[2] get_universe_full(): {len(uni)} 只, 前5={uni[:5]}")

    bench = get_benchmark()
    print(f"[3] get_benchmark(): {len(bench)} 根, "
          f"{bench['date'].min().date() if len(bench) else 'N/A'}")
