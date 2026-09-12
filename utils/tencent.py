"""
腾讯行情源 — 自选股/持仓/候选池日线主力
接口: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
⚠ 未公开文档化的网页接口，视为可能变化的非正式依赖：
   - 不用于全市场高并发扫描
   - 必须限速、缓存、熔断
   - 返回结构不硬编码单一路径

解析规则（GPT建议落地）:
- 返回路径: data[代码].qfqday / hfqday / day（候选回退）
- K线行: 6列=日期,开盘,收盘,最高,最低,成交量；7列及以上才读成交额
- 成交量单位: 手；不复权 day 用 raw，前复权 qfqday 用 qfq
"""
import time
import random
import requests
import pandas as pd

BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
MIN_INTERVAL = 1.2       # 最小请求间隔（秒）
REQUEST_TIMEOUT = 12     # 单请求超时
MAX_RETRY = 2            # 最大重试
BREAKER_THRESHOLD = 3    # 连续失败熔断阈值
BREAKER_COOLDOWN = 1800  # 熔断冷却 30分钟


class TencentSource:
    """腾讯行情源（带限速+熔断）"""

    def __init__(self):
        self._last_ts = 0.0
        self._consec_fail = 0
        self._breaker_until = 0.0

    def _throttle(self):
        """限速：至少间隔 MIN_INTERVAL + 随机抖动"""
        elapsed = time.time() - self._last_ts
        wait = MIN_INTERVAL - elapsed + random.uniform(0, 0.3)
        if wait > 0:
            time.sleep(wait)
        self._last_ts = time.time()

    def _breaker_open(self):
        return time.time() < self._breaker_until

    def _record_fail(self):
        self._consec_fail += 1
        if self._consec_fail >= BREAKER_THRESHOLD:
            self._breaker_until = time.time() + BREAKER_COOLDOWN
            print(f"[腾讯] 连续{self._consec_fail}次失败，熔断{int(BREAKER_COOLDOWN/60)}分钟")
            self._consec_fail = 0

    def _record_ok(self):
        self._consec_fail = 0

    def fetch(self, symbol, count=120, adjustment="qfq"):
        """
        获取日线
        symbol: sh600519 / sz002594 / sh000001
        adjustment: qfq前复权 / 空=不复权
        返回: DataFrame(date/open/high/low/close/volume/amount?) 或 None
        """
        if self._breaker_open():
            return {"status": "RATE_LIMITED", "msg": "腾讯源熔断中"}

        self._throttle()
        param = f"{symbol},day,,,{count},{adjustment}"
        for attempt in range(MAX_RETRY + 1):
            try:
                r = requests.get(BASE, params={"param": param},
                                 timeout=REQUEST_TIMEOUT)
                js = r.json()
                if not isinstance(js, dict) or not isinstance(js.get("data"), dict):
                    if attempt < MAX_RETRY:
                        time.sleep(2 ** attempt)
                        continue
                    self._record_fail()
                    return {"status": "SCHEMA_CHANGED", "msg": f"返回结构异常: {str(js)[:80]}"}
                node = js["data"].get(symbol, {})
                if not isinstance(node, dict):
                    if attempt < MAX_RETRY:
                        time.sleep(2 ** attempt)
                        continue
                    self._record_fail()
                    return {"status": "SCHEMA_CHANGED", "msg": f"节点结构异常: {str(node)[:80]}"}

                # 候选路径解析（不硬编码）
                klines = None
                if adjustment == "qfq":
                    klines = node.get("qfqday") or node.get("day")
                else:
                    klines = node.get("day") or node.get("hfqday") or node.get("qfqday")
                klines = klines or node.get("klinedata")

                if not klines:
                    if attempt < MAX_RETRY:
                        time.sleep(2 ** attempt)
                        continue
                    self._record_fail()
                    return {"status": "EMPTY_DATA", "msg": f"{symbol} 空数据"}

                rows = []
                for row in klines:
                    # 除权日行可能附加分红信息dict（如 {"nd":..,"fh_sh":..,"FHcontent":"10派280元"}），需剔除
                    row = [x for x in row if not isinstance(x, dict)]
                    # 兼容6列/7列
                    if len(row) >= 7:
                        date, o, c, h, l, vol, amt = row[:7]
                    elif len(row) == 6:
                        date, o, c, h, l, vol = row
                        amt = None
                    else:
                        continue
                    rows.append({
                        "date": date, "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c),
                        "volume": float(vol),
                        "amount": float(amt) if amt else None,
                    })

                if not rows:
                    self._record_fail()
                    return {"status": "EMPTY_DATA", "msg": "解析后无数据"}

                df = pd.DataFrame(rows).set_index("date")
                self._record_ok()
                return {"status": "OK", "df": df, "source": "tencent",
                        "adjustment": adjustment or "raw",
                        "volume_unit": "lot", "amount_unit": "yuan"}

            except requests.RequestException as e:
                if attempt < MAX_RETRY:
                    time.sleep(2 ** attempt)  # 指数退避
                    continue
                self._record_fail()
                return {"status": "NETWORK_ERROR", "msg": str(e)[:100]}
            except (ValueError, KeyError, TypeError) as e:
                if attempt < MAX_RETRY:
                    time.sleep(2 ** attempt)
                    continue
                self._record_fail()
                return {"status": "SCHEMA_CHANGED", "msg": f"结构变化: {str(e)[:80]}"}

        self._record_fail()
        return {"status": "NETWORK_ERROR", "msg": "重试耗尽"}


_tencent = None


def get_tencent():
    """单例"""
    global _tencent
    if _tencent is None:
        _tencent = TencentSource()
    return _tencent
