"""
新闻情绪量化管道（模仿"高手看板"的新闻情绪分析）
==============================================
流程: 抓新浪7x24快讯 → 关键词规则三分类(利好/利空/中性) → 板块映射 → 板块情绪聚合排序

设计原则:
- 数据源: 新浪财经 7x24 直播(免费、结构化、无需 key, 对方看板同源)
- 情绪分类: 本地关键词规则(不逐条过 LLM, 成本低、速度快、可离线)
- 板块映射: 关键词→板块词典(覆盖 A 股核心板块)
- 聚合: 每板块累计 🟢利好/🔴利空/⚪中性 计数 + 净分, 按净分排序

用法:
    from utils.news_sentiment import analyze_news
    result = analyze_news(page_size=200)
"""
import re
import json
import time
import urllib.request

# ===================== 数据抓取 =====================

def fetch_news(page_size=200, timeout=15):
    """抓取新浪 7x24 快讯, 返回 [{"时间","内容","关联股票"}...]"""
    url = (
        "https://zhibo.sina.com.cn/api/zhibo/feed"
        f"?page=1&page_size={page_size}&zhibo_id=152&tag_id=0&dire=f&dpc=1"
    )
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e), "news": []}

    lst = data.get("result", {}).get("data", {}).get("feed", {}).get("list", [])
    news = []
    for x in lst:
        text = (x.get("rich_text") or "").strip()
        if not text:
            continue
        # 关联股票(ext.stocks)
        stocks = []
        try:
            ext = json.loads(x.get("ext") or "{}")
            stocks = ext.get("stocks", []) or []
        except Exception:
            pass
        news.append({
            "id": x.get("id"),
            "时间": x.get("create_time", ""),
            "内容": text,
            "关联股票": stocks,
        })
    return {"ok": True, "news": news}


# ===================== 板块关键词词典 =====================
# 板块 → 触发关键词(命中任一即归入该板块)
SECTOR_KEYWORDS = {
    "AI算力": ["算力", "英伟达", "英伟达模型", "GPU", "数据中心", "服务器", "大模型", "AI", "人工智能", "算力期货", "液冷"],
    "光模块": ["光模块", "CPO", "光通信", "光芯片", "800G", "1.6T", "光模块CPO"],
    "半导体": ["半导体", "芯片", "晶圆", "封测", "国产芯片", "集成电路", "化合物半导体", "半导体设备", "半导体封测"],
    "存储芯片": ["存储", "存储芯片", "DRAM", "NAND", "海力士", "美光", "三星", "长鑫", "长存"],
    "消费电子": ["消费电子", "苹果", "iPhone", "果链", "华为", "手机", "智能穿戴", "耳机"],
    "计算机": ["软件", "计算机", "信创", "操作系统", "网络安全", "工业软件", "办公软件"],
    "通信": ["通信", "5G", "6G", "运营商", "通信设备", "卫星互联网", "商业航天", "低轨"],
    "新能源车": ["新能源车", "电动车", "比亚迪", "特斯拉", "智能驾驶", "智驾", "充电桩", "锂电池", "锂电", "固态电池", "电池"],
    "光伏": ["光伏", "硅料", "硅片", "组件", "多晶硅", "钙钛矿", "光伏储能"],
    "储能": ["储能", "储能系统", "抽水蓄能"],
    "风电": ["风电", "海上风电", "风电设备"],
    "机器人": ["机器人", "人形机器人", "具身智能", "减速器", "机器视觉", "工业机器人"],
    "军工": ["军工", "国防", "导弹", "航母", "航天", "航空发动机", "无人机", "军贸", "船舶"],
    "创新药": ["创新药", "医药", "CXO", "生物医药", "疫苗", "医疗器械", "医疗", "药企", "AI制药", "IVD"],
    "银行": ["银行", "息差", "存款", "贷款", "DR贷款", "净息差", "不良率"],
    "券商": ["券商", "证券", "基金", "非银", "两融", "融资余额", "ETF资金"],
    "保险": ["保险", "保费", "险资"],
    "房地产": ["房地产", "地产", "楼市", "房价", "土拍", "公积金", "购房", "建材", "家居", "城中村"],
    "有色金属": ["有色", "铜", "铝", "锂", "钴", "镍", "稀土", "黄金", "贵金属", "白银", "锡", "锌"],
    "煤炭": ["煤炭", "煤", "焦煤", "焦炭", "动力煤"],
    "石油石化": ["石油", "原油", "油价", "油气", "石化", "炼化", "天然气", "油服", "油运", "霍尔木兹"],
    "电力": ["电力", "电网", "火电", "水电", "核电", "绿电", "用电", "负荷"],
    "传媒游戏": ["传媒", "游戏", "影视", "电影", "票房", "院线", "短视频", "出版"],
    "食品饮料": ["食品", "饮料", "白酒", "啤酒", "乳业", "调味品", "消费"],
    "家电": ["家电", "空调", "冰箱", "洗衣机", "厨电"],
    "交运物流": ["物流", "港口", "航运", "航空", "铁路", "快递", "集装箱", "航运港口"],
    "汽车零部件": ["汽车零部件", "汽车", "整车", "车用芯片", "毫米波雷达", "激光雷达"],
    "农业养殖": ["农业", "养殖", "种业", "生猪", "猪肉", "粮食", "农产品", "饲料"],
    "化工": ["化工", "化肥", "农药", "新材料", "POE", "PTA", "纯碱", "甲醇", "生物柴油"],
    "建筑基建": ["基建", "建筑", "工程", "管网", "城市更新", "一带一路", "轨交"],
    "钢铁": ["钢铁", "钢", "钢铝"],
    "环保": ["环保", "碳中和", "碳交易", "绿色", "绿氢", "CCUS"],
    "数字货币": ["数字货币", "区块链", "金融科技", "跨境支付", "稳定币"],
    "港股科技": ["港股", "恒生科技", "中概", "恒生"],
    "美股映射": ["美股", "纳斯达克", "标普", "道指", "美联储", "降息", "加息", "美债"],
    "低空经济": ["低空经济", "低空", "eVTOL", "无人机物流"],
    "军工船舶": ["船舶", "造船"],
}

# 板块 → 所属大组(用于前端分组矩阵)
SECTOR_GROUP = {
    "银行": "金融", "券商": "金融", "保险": "金融", "数字货币": "金融",
    "AI算力": "科技", "光模块": "科技", "半导体": "科技", "存储芯片": "科技",
    "消费电子": "科技", "计算机": "科技", "通信": "科技", "港股科技": "科技", "美股映射": "科技",
    "新能源车": "消费", "食品饮料": "消费", "家电": "消费", "汽车零部件": "消费",
    "有色金属": "能源资源", "煤炭": "能源资源", "石油石化": "能源资源", "电力": "能源资源", "钢铁": "能源资源",
    "军工": "制造", "机器人": "制造", "光伏": "制造", "储能": "制造", "风电": "制造", "军工船舶": "制造", "低空经济": "制造",
    "创新药": "医药",
    "房地产": "其他", "传媒游戏": "其他", "交运物流": "其他", "农业养殖": "其他",
    "化工": "其他", "建筑基建": "其他", "环保": "其他",
}

# ===================== 情绪分类词典 =====================

POSITIVE_WORDS = [
    "利好", "大涨", "涨停", "突破", "中标", "签约", "预增", "回购", "增持", "获批",
    "超预期", "扭亏", "创新高", "提价", "降息", "扩产", "净买入", "增长", "盈利",
    "上涨", "升", "景气", "复苏", "回暖", "提速", "超额认购", "强劲", "新高",
    "落地", "开工", "投产", "商用", "量产", "融资", "加码", "业绩预喜", "上调", "流入",
]
NEGATIVE_WORDS = [
    "利空", "大跌", "跌停", "下滑", "亏损", "减持", "处罚", "调查", "退市", "违约",
    "暴雷", "解禁", "下调", "裁员", "召回", "终止", "净卖出", "下降", "下跌",
    "风险", "萎缩", "衰退", "恶化", "承压", "出清", "冻结", "制裁", "暴跌",
    "崩", "下调预期", "目标价被砍", "通胀", "加息", "流出", "赎回", "雷",
]

NEUTRAL_HINT = ["会议", "公告", "发布", "表示", "称", "消息", "报", "报道", "快讯", "关注"]


def _clean_text(text):
    """去掉【】标记和多余空白"""
    t = re.sub(r"【[^】]*】", "", text)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def classify(text):
    """三分类: 利好/利空/中性。返回 (类别, 命中词)"""
    t = _clean_text(text)
    pos_hits = [w for w in POSITIVE_WORDS if w in t]
    neg_hits = [w for w in NEGATIVE_WORDS if w in t]
    # 利好/利空词都可能命中时, 取命中更多的一方; 一样多则偏保守(中性/利空)
    if pos_hits and not neg_hits:
        return "利好", pos_hits
    if neg_hits and not pos_hits:
        return "利空", neg_hits
    if pos_hits and neg_hits:
        if len(pos_hits) > len(neg_hits):
            return "利好", pos_hits
        elif len(neg_hits) > len(pos_hits):
            return "利空", neg_hits
        return "中性", pos_hits + neg_hits
    return "中性", []


def map_sectors(text):
    """新闻 → 命中的板块列表(可多板块)"""
    t = text
    hits = []
    for sector, kws in SECTOR_KEYWORDS.items():
        if any(kw.lower() in t.lower() for kw in kws):
            hits.append(sector)
    return hits


def analyze_news(page_size=300):
    """主入口: 抓新闻 → 分类 → 板块映射 → 聚合"""
    r = fetch_news(page_size=page_size)
    if not r.get("ok"):
        return {"status": "ERROR", "msg": r.get("error", "抓取失败")}

    news = r["news"]
    total = {"利好": 0, "利空": 0, "中性": 0}
    sector_stat = {}  # 板块 -> {"利好":n, "利空":n, "中性":n}
    analyzed = []  # 带解读的新闻列表(重点新闻)

    for n in news:
        text = n["内容"]
        cat, hits = classify(text)
        sectors = map_sectors(text)
        total[cat] += 1
        for s in sectors:
            st = sector_stat.setdefault(s, {"利好": 0, "利空": 0, "中性": 0})
            st[cat] += 1
        # 重点新闻(非中性)记录
        if cat != "中性" and sectors:
            analyzed.append({
                "时间": n["时间"],
                "内容": text[:120],
                "类别": cat,
                "板块": sectors[:3],
                "命中词": hits[:5],
            })

    # 板块聚合排序: 净分 = 利好 - 利空
    sector_list = []
    for s, st in sector_stat.items():
        net = st["利好"] - st["利空"]
        sector_list.append({
            "板块": s,
            "大组": SECTOR_GROUP.get(s, "其他"),
            "利好": st["利好"], "利空": st["利空"], "中性": st["中性"],
            "净分": net,
        })
    sector_list.sort(key=lambda x: (-x["净分"], -x["利好"]))

    # 按大组归集(板块雷达用)
    groups = {}
    for s in sector_list:
        g = s["大组"]
        groups.setdefault(g, {"利好": 0, "利空": 0, "中性": 0, "板块": []})
        groups[g]["利好"] += s["利好"]
        groups[g]["利空"] += s["利空"]
        groups[g]["中性"] += s["中性"]
        groups[g]["板块"].append(s)

    return {
        "status": "OK",
        "抓取时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "新闻总数": len(news),
        "情绪统计": total,
        "板块情绪": sector_list,   # 按净分排序的板块列表
        "分组雷达": groups,         # 按大组归集
        "重点新闻": analyzed[:50],  # 利好/利空且有板块的新闻
    }


if __name__ == "__main__":
    result = analyze_news(page_size=100)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])
