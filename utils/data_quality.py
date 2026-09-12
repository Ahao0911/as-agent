"""
数据质量检查 — 任何行情进入指标层前必须通过
状态码: OK / EMPTY_DATA / STALE_DATA / SCHEMA_CHANGED / RATE_LIMITED /
        NETWORK_ERROR / UNSUPPORTED_SYMBOL / DATA_VALIDATION_FAILED / QUOTA_EXHAUSTED
规则: HTTP 200 ≠ 数据成功；验证失败不得编造指标
"""
import pandas as pd


def validate_kline(df, symbol=None, adjustment=None, expect_last_date=None):
    """
    校验K线DataFrame
    返回: (status, issues列表)
    """
    if df is None:
        return "EMPTY_DATA", ["空数据"]
    if len(df) == 0:
        return "EMPTY_DATA", ["零行数据"]

    issues = []
    try:
        df = df.copy()
        for col in ["open", "high", "low", "close"]:
            if col not in df.columns:
                return "SCHEMA_CHANGED", [f"缺列 {col}"]

        o = df["open"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        v = df["volume"].astype(float) if "volume" in df else None

        # 1. 数值有效
        if not (o.notna().all() and h.notna().all() and l.notna().all() and c.notna().all()):
            issues.append("存在NaN价格")
        # 2. high >= max(open, close, low)
        bad_high = (h < o) | (h < c) | (h < l)
        if bad_high.any():
            issues.append(f"{int(bad_high.sum())}根K线 high 异常")
        # 3. low <= min(open, close, high)
        bad_low = (l > o) | (l > c) | (l > h)
        if bad_low.any():
            issues.append(f"{int(bad_low.sum())}根K线 low 异常")
        # 4. volume >= 0
        if v is not None and (v < 0).any():
            issues.append("存在负成交量")
        # 5. 日期有效且递增无重复
        if isinstance(df.index, pd.DatetimeIndex):
            if df.index.has_duplicates:
                issues.append("日期重复")
            if not df.index.is_monotonic_increasing:
                issues.append("日期未递增")
            if str(df.index[-1])[:10] > pd.Timestamp.today().strftime("%Y-%m-%d"):
                issues.append("最后日期是未来日期")
        # 6. 数量级跳变（价格突然10倍/百分之一）
        if len(df) > 2:
            pct = c.pct_change().abs().dropna()
            jumps = pct[pct > 1.0]  # >100%跳变
            if len(jumps) > 0 and not (len(df) < 10):  # 短序列忽略
                # 检查是否是真实涨停（限制在20%内）
                big = pct[pct > 0.25]
                if len(big) > 0:
                    issues.append(f"{len(big)}处>25%价格跳变（检查停牌/复权问题）")
    except Exception as e:
        return "DATA_VALIDATION_FAILED", [str(e)[:100]]

    if issues:
        return "DATA_VALIDATION_FAILED", issues
    return "OK", []


def merge_check(symbol, adjustment, expected):
    """请求与返回一致性检查"""
    if symbol and expected:
        prefix = symbol[:2]
        if prefix in ("sh", "sz") and expected[:2] != prefix:
            return False, f"代码不匹配: 请求{symbol} 返回{expected}"
    return True, ""
