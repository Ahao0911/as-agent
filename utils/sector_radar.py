"""
板块雷达分组矩阵（模仿"高手看板"的板块雷达）
==============================================
流程: 拉东财行业+概念板块 → 按7大组归类 → 拉领涨股 → 分组返回

7大组: 金融 / 科技 / 消费 / 能源资源 / 制造 / 医药 / 其他

用法:
    from utils.sector_radar import get_sector_radar
    result = get_sector_radar()
"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://quote.eastmoney.com/"}


def _fetch(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_sector_list():
    """拉行业板块(t:2) + 概念板块(t:3)全量, 返回 [{代码,名称,涨幅,成交额}]"""
    sectors = []
    for t in ("2", "3"):
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:{t}+f:!50&fields=f2,f3,f12,f14"
        )
        try:
            d = _fetch(url)
            diff = (d.get("data") or {}).get("diff") or []
            for x in diff:
                sectors.append({
                    "代码": x.get("f12"), "名称": x.get("f14"),
                    "涨幅": x.get("f3"), "成交额": x.get("f2"),
                })
        except Exception:
            continue
    return sectors


# 7大组 → 板块名关键词(命中即归组)
GROUP_RULES = [
    ("金融", ["银行", "证券", "保险", "多元金融", "互联网金融", "金融科技", "期货", "信托", "券商"]),
    ("科技", ["半导体", "芯片", "通信", "计算机", "软件", "光学", "电子", "元件", "印制电路", "PCB",
              "算力", "CPO", "光模块", "光通信", "存储", "消费电子", "互联网", "数据", "智能", "软件服务",
              "被动元件", "磁性材料", "模拟芯片", "集成电路", "人工智能", "云计算", "大数据", "信创"]),
    ("消费", ["酿酒", "白酒", "食品", "饮料", "家电", "零售", "旅游", "酒店", "汽车整车", "汽车零部件",
              "纺织", "服装", "家居", "美妆", "免税", "乳业", "调味品", "商业"]),
    ("能源资源", ["有色", "煤炭", "贵金属", "钢铁", "石油", "燃气", "电力", "稀土", "黄金", "白银",
                  "化工", "原油", "天然气", "锂", "铜", "铝", "钴", "镍", "锌", "水泥"]),
    ("制造", ["航天", "航空", "电池", "军工", "国防", "机器人", "光伏", "风电", "电网", "机械",
              "设备", "船舶", "汽车零部件", "通用设备", "专用设备", "电机", "电源", "轨道交通",
              "机床", "工程机械", "自动化", "船舶制造"]),
    ("医药", ["医药", "医疗", "生物", "化学制药", "中药", "创新药", "疫苗", "医疗器械", "医疗服务",
              "生物制品", "制药"]),
]

# 大组排序(固定顺序展示)
GROUP_ORDER = ["金融", "科技", "消费", "能源资源", "制造", "医药", "其他"]


def group_sector(name):
    for group, kws in GROUP_RULES:
        if any(kw in name for kw in kws):
            return group
    return "其他"


def fetch_leader(code):
    """拉某板块的领涨股(涨幅第1)"""
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f3&fs=b:{code}&fields=f2,f3,f12,f14"
    )
    try:
        d = _fetch(url)
        diff = (d.get("data") or {}).get("diff") or []
        if diff:
            x = diff[0]
            return {"名称": x.get("f14"), "涨幅": x.get("f3"), "代码": x.get("f12")}
    except Exception:
        pass
    return None


def get_sector_radar(max_per_group=12, leader_limit=8):
    """主入口: 拉板块 → 分组 → 领涨股 → 返回分组雷达"""
    sectors = fetch_sector_list()
    if not sectors:
        return {"status": "ERROR", "msg": "板块数据获取失败(可能非交易日或网络异常)", "分组": []}

    # 按涨幅排序后分组(每组取涨幅前列)
    sectors.sort(key=lambda x: (x.get("涨幅") is None, -(x.get("涨幅") or 0)))

    groups = {}
    for s in sectors:
        g = group_sector(s["名称"])
        if g not in groups:
            groups[g] = []
        if len(groups[g]) < max_per_group:
            groups[g].append(s)

    # 拉领涨股(并发, 只对每组前 leader_limit 个板块)
    def _with_leader(s):
        s["领涨股"] = fetch_leader(s["代码"])
        return s

    for g in groups:
        top = groups[g][:leader_limit]
        rest = groups[g][leader_limit:]
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_with_leader, s) for s in top]
            for f in as_completed(futs):
                pass  # 结果已原地写入 s
        # 补齐无领涨股的(保持结构完整)
        for s in rest:
            s["领涨股"] = None

    # 按固定顺序输出
    result = []
    for g in GROUP_ORDER:
        if g in groups:
            result.append({"大组": g, "板块": groups[g]})
    for g in groups:
        if g not in GROUP_ORDER:
            result.append({"大组": g, "板块": groups[g]})

    return {
        "status": "OK",
        "更新时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "板块总数": len(sectors),
        "分组": result,
    }


if __name__ == "__main__":
    r = get_sector_radar(max_per_group=6, leader_limit=4)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:2500])
