import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from utils.stock_data import get_index_data, get_sector_data, get_market_amount, get_market_sentiment

def run():
    index_data = get_index_data()
    sector_data = get_sector_data()
    amount_data = get_market_amount()
    sentiment = get_market_sentiment()

    # 指数表格
    idx_rows = ""
    for d in index_data:
        sign = "+" if d['涨跌幅'] > 0 else ""
        arrow = "▲" if d['涨跌幅'] > 0 else "▼" if d['涨跌幅'] < 0 else "━"
        idx_rows += f"  {d['名称']:　<5} {d['收盘']:>10}  {sign}{d['涨跌幅']}%{arrow}  振幅{d['振幅']}%\n"

    # 成交额
    amt = amount_data
    amt_text = f"{amt['沪深合计']:.0f}亿（沪{amt['沪市']:.0f} / 深{amt['深市']:.0f}）"

    # 涨跌家数
    up_cnt = sentiment['上涨家数']
    down_cnt = sentiment['下跌家数']
    flat_cnt = sentiment['平盘']
    zt = sentiment['涨停']
    dt = sentiment['跌停']
    total = up_cnt + down_cnt + flat_cnt
    up_pct = round(up_cnt / total * 100, 1) if total > 0 else 0

    # 判断市场结构
    if up_pct > 65:
        market_type = "普涨行情"
    elif up_pct > 50:
        market_type = "结构性偏强"
    elif up_pct > 40:
        market_type = "结构性分化"
    else:
        market_type = "个股普跌、权重护盘"

    # 行业板块
    ind_up = "\n".join([f"  ▸ {s['板块']}  +{s['涨幅']}%  ({s['成交额']:.0f}亿)" for s in sector_data.get('行业涨幅', [])[:6]])
    ind_down = "\n".join([f"  ▸ {s['板块']}  {s['跌幅']}%  ({s['成交额']:.0f}亿)" for s in sector_data.get('行业跌幅', [])[:5]])

    # 概念板块
    con_up = "\n".join([f"  ▸ {s['板块']}  +{s['涨幅']}%  ({s['成交额']:.0f}亿)" for s in sector_data.get('概念涨幅', [])[:6]])
    con_down = "\n".join([f"  ▸ {s['板块']}  {s['跌幅']}%  ({s['成交额']:.0f}亿)" for s in sector_data.get('概念跌幅', [])[:4]])

    prompt = f"""
你是专业A股复盘分析师。根据以下真实行情数据，生成收盘复盘报告。

要求：
- 语言精炼，券商研报风格
- 用符号(▸ ▲ ▼ ★)和列表呈现，方便快速扫读
- 每个模块用分隔线隔开
- 分析必须基于数据，不编造
- 严格按6个模块输出

---

【今日行情数据】

指数：
{idx_rows}
成交额：{amt_text}

涨跌家数：上涨{up_cnt}只 / 下跌{down_cnt}只 / 平盘{flat_cnt}只
涨停{zt}只 / 跌停{dt}只
上涨占比：{up_pct}%
市场结构：{market_type}

行业涨幅TOP：
{ind_up}

行业跌幅TOP：
{ind_down}

概念涨幅TOP（核心主线）：
{con_up}

概念跌幅TOP：
{con_down}

---

请严格按以下结构输出，每个部分之间用 ─── 分隔线隔开：

## 一、大盘概览
表格展示四大指数，然后写成交额和市场定性（1-2句）。
必须明确说明是普涨还是结构性行情，引用涨跌家数数据。

## 二、板块轮动
分两层：
1. 概念主线：列出涨幅前5的概念板块，标注核心主线（成交额最大+涨幅最高的方向）
2. 行业板块：分红盘和弱势，标注资金流入流出方向
最后用1句话总结板块剪刀差

## 三、盘面节奏
分早盘、午盘、尾盘，每个时段2句话概括。

## 四、多维分析
趋势/量能/风格/情绪四个维度，每个维度1-2句。
情绪维度必须结合涨跌家数，区分"全面亢奋"和"结构性亢奋"。

## 五、明日策略
三种情景表格（看涨/震荡/回调），每种写概率、关键位、操作建议。
最后加一条"风险提示"：基于涨跌家数和板块分化情况。

## 六、核心结论
用框线(╔══╗)包裹，一句话总结核心判断。
    """

    return prompt
