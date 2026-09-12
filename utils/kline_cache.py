"""
本地行情缓存层 — SQLite
键: symbol + period + adjustment（如 sz002594_day_qfq）
策略:
- 查询本地最后交易日，只补缺失区间
- 回刷最近5个交易日（处理数据源盘后修正/复权变化）
- 按 symbol/date/adjustment 去重
- 同时保存 raw（不复权，用于操作复盘成本对比）和 qfq（前复权，用于技术指标）
"""
import os
import sys
import sqlite3
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "kline_cache.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    adjustment TEXT NOT NULL,      -- raw / qfq
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (symbol, date, adjustment)
);
CREATE INDEX IF NOT EXISTS idx_symbol_date ON klines(symbol, date);
"""


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            adjustment TEXT NOT NULL,      -- raw / qfq
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL,
            source TEXT,
            fetched_at TEXT,
            PRIMARY KEY (symbol, date, adjustment)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_date ON klines(symbol, date)")
    return conn


def save_klines(symbol, df, adjustment, source="tencent"):
    """保存K线（去重，按 symbol+date+adjustment）"""
    if df is None or len(df) == 0:
        return 0
    conn = _conn()
    fetched = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for date, row in df.iterrows():
        rows.append((symbol, str(date)[:10], adjustment,
                     float(row["open"]), float(row["high"]),
                     float(row["low"]), float(row["close"]),
                     float(row.get("volume", 0)),
                     float(row["amount"]) if row.get("amount") is not None else None,
                     source, fetched))
    conn.executemany("""
        INSERT OR REPLACE INTO klines
        (symbol, date, adjustment, open, high, low, close, volume, amount, source, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    conn.close()
    return len(rows)


def load_klines(symbol, adjustment="qfq", limit=None):
    """读取缓存K线（升序，limit取最新N根）"""
    conn = _conn()
    if limit:
        # 先取最新的 N 根（倒序limit），再正序返回
        sql = ("SELECT date, open, high, low, close, volume, amount, source "
               "FROM klines WHERE symbol=? AND adjustment=? "
               "ORDER BY date DESC LIMIT ?")
        cur = conn.execute(sql, (symbol, adjustment, limit))
        rows = cur.fetchall()
        conn.close()
        rows = rows[::-1]  # 反转为升序
    else:
        sql = "SELECT date, open, high, low, close, volume, amount, source FROM klines WHERE symbol=? AND adjustment=? ORDER BY date"
        cur = conn.execute(sql, (symbol, adjustment))
        rows = cur.fetchall()
        conn.close()
    if not rows:
        return None
    import pandas as pd
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount", "source"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def last_trade_date(symbol, adjustment="qfq"):
    """本地最后交易日"""
    conn = _conn()
    cur = conn.execute("SELECT MAX(date) FROM klines WHERE symbol=? AND adjustment=?",
                       (symbol, adjustment))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def cache_stats():
    """缓存统计"""
    conn = _conn()
    cur = conn.execute("SELECT symbol, adjustment, COUNT(*) FROM klines GROUP BY symbol, adjustment")
    rows = cur.fetchall()
    conn.close()
    return {f"{s}/{a}": c for s, a, c in rows}


if __name__ == "__main__":
    stats = cache_stats()
    print(f"缓存统计: {len(stats)} 个标的, 共{sum(stats.values())}根K线")
    for k, v in sorted(stats.items())[:20]:
        print(f"  {k}: {v}根")
