# -*- coding: utf-8 -*-
"""
完美图形案例绘图器（⚠️ 仅供形态示意，禁止用于形态匹配）
==========================================================
用**真实行情数据**绘制「图形买点体系 10 张经典图形」的**形态示意** K 线图。

输出：data/kb_manual/charts/*.png
每个案例一张图，含 4 个面板：
  ① 黄白线主图（K线 + 黄线 + 白线 + J值大负区高亮）
  ② 成交量（含均量线，用于判断「缩半量」）
  ③ KDJ（标注 J 值大负值区）
  ④ 说明文字（图形定义 + 要点 + 用途限制）

═══════════════════════════════════════════════════════════════
⚠️ 用途与限制（务必阅读）
═══════════════════════════════════════════════════════════════
【本图是什么】
  K 线、成交量、黄白线、KDJ 数值均为**真实行情实拉实算**（腾讯源）。
  图形定义（4 大类）来自公开文字版《图形买点体系 10 张经典图形》。

【本图不是什么】
  ✗ **不是原案例的原始截图**。原案例图源（雪球/公众号/ima）均不可得。
  ✗ **案例时点未经验证**。CASES 里的 case_date 为**推测填充值**（按 4 大类
    顺序摊在 2025-05~11），**无任何原始出处**。相关紫竖线与角标已全部移除。
  ✗ **不能用作「形态匹配」的模板**。经实测，图中紫线位置（现已移除）的行情
    特征多数不符合对应图形判据（如 J 值不在大负区、收盘不在白/黄线上方）。
    用它去比对模拟盘标的，得到的"相似度"无意义。

【正确用途】
  ✓ 作为**形态教学示意**：直观展示「什么叫缩半量」「什么叫沿白线运行」
    「J 大负区长什么样」——这些特征本身是真实的。
  ✓ 作为**判据代码化的验证辅助**：T04 实现 patterns_ten.py 后，可用本图
    人工核对部分判据的呈现效果。

【若要真正的形态模板】
  路径一：从 trade_journal.json 提取用户**实盘盈利的 B1 买点**，构建自有模板库。
  路径二：把 4 类图形写成**数值判据**（见 docs/），对候选票输出"命中几条"。
  路径三：从 ima 知识库检索 图形买点体系课件的**原始图片**（非文字描述）。

数据：utils.data_router.get_daily_bars（腾讯源，无需 token，硬上限约 261 条）
⚠️ 数据窗口：腾讯源仅回溯约 1 年（2025-08-18 起），早于该窗口的案例无法展示。
"""
import sys
import os
import json
import datetime
import warnings

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from utils.data_router import get_daily_bars

# ---------------------------------------------------------------- 中文字体
def _setup_font():
    """Windows 下配置中文字体，避免方块"""
    candidates = ["Microsoft YaHei", "SimHei", "SimSun", "KaiTi", "DengXian"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name]
            plt.rcParams["axes.unicode_minus"] = False
            return name
    for path in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]:
        if os.path.exists(path):
            font_manager.fontManager.addfont(path)
            name = font_manager.FontProperties(fname=path).get_name()
            plt.rcParams["font.sans-serif"] = [name]
            plt.rcParams["axes.unicode_minus"] = False
            return name
    return None


FONT = _setup_font()


# ---------------------------------------------------------------- 指标计算
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """黄线、白线、KDJ —— 与项目 utils/indicators.py 口径一致"""
    c, h, l = df["close"], df["high"], df["low"]

    # 白线 = EMA(EMA(C,10),10)  —— 短期趋势线（牵牛绳）
    df["white"] = ema(ema(c, 10), 10)

    # 黄线 = (MA14+MA28+MA57+MA114)/4 —— 中期趋势线（大哥线/知行多空线）
    df["yellow"] = (c.rolling(14).mean() + c.rolling(28).mean()
                    + c.rolling(57).mean() + c.rolling(114).mean()) / 4

    # KDJ(9,3,3)
    low_n = l.rolling(9).min()
    high_n = h.rolling(9).max()
    rsv = (c - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    df["K"] = rsv.ewm(com=2, adjust=False).mean()
    df["D"] = df["K"].ewm(com=2, adjust=False).mean()
    df["J"] = 3 * df["K"] - 2 * df["D"]

    df["vma5"] = df["volume"].rolling(5).mean()
    df["vma10"] = df["volume"].rolling(10).mean()
    return df


# ---------------------------------------------------------------- 案例定义
# ⚠️ case_date 为【推测填充值】，无原始出处，仅用于历史判断数据窗口，
#    【不得】在图中标注、【不得】作为案例时点引用。已于 2026-09-10 停用标注。
CASES = [
    dict(code="688799", name="华纳药厂", cat="完美缩量图形", win=120,
         case_date="2025-05",
         theme="创新药（当时最热主题）",
         note=("① 前期放量阳线突破黄白线，黄白线金叉\n"
               "② 缩半量回调，J 值探至 -12 附近\n"
               "③ 当日成交量极低 → 主力充分锁仓、高度控盘\n"
               "④ 白/黄线上方盘整 + 创新药主题 → 完美 B1")),
    dict(code="688321", name="微芯生物", cat="完美缩量图形", win=120,
         case_date="2025-06",
         theme="创新药",
         note=("① 同华纳药厂：放量建仓 → 缩量洗盘\n"
               "② 量价关系完美\n"
               "③ 创新药题材共振")),
    dict(code="605378", name="野马电池", cat="完美缩量图形", win=120,
         case_date="2025-07",
         theme="固态电池",
         note=("① 缩量进行盘整\n"
               "② 拉升前的震仓是不容易的持仓点")),
    dict(code="300907", name="康平科技", cat="完美缩量图形", win=120,
         case_date="2025-08",
         theme="新能源车 / 机器人",
         note=("① 放量建仓后缩量回调\n"
               "② 在黄白线上方盘整，缩半量")),
    dict(code="300811", name="铂科新材", cat="完美缩量图形", win=120,
         case_date="2025-08",
         theme="新材料 / 算力",
         note=("① 同类型完美缩量图形\n"
               "② 缩半量 + 黄白线支撑")),
    dict(code="600601", name="方正科技", cat="N型结构回调买点", win=160,
         case_date="2025-08",
         theme="国产软件 / 算力",
         note=("① 有节奏的 N 型结构上涨\n"
               "② 沿着白线不断上涨，未打破 N 型节奏\n"
               "③ 回调日即买点\n"
               "④ 「这就是股票的基因，本质是庄家操盘手法」")),
    dict(code="600366", name="宁波韵升", cat="N型结构回调买点", win=160,
         case_date="2025-09",
         theme="化工（反内卷）",
         note=("① 多个完美的 N 型结构上升买点\n"
               "② 一直在黄白线上运行，非常强势")),
    dict(code="600184", name="光电股份", cat="N型结构回调买点", win=160,
         case_date="2025-09",
         theme="军工 / 光电",
         note=("① 同宁波韵升：N 型结构 + 黄白线上方运行\n"
               "② 回调不破白线")),
    dict(code="002940", name="昂利康", cat="一直拉升的回调买点", win=160,
         case_date="2025-10",
         theme="医药",
         note=("① 连续拉升至 70% 以上 = 一波流拉升\n"
               "② 拉升无量，主力高度控盘，没有出货\n"
               "③ 回调白线的买点\n"
               "④ 「看起来危险的标的，反而是最安全的」")),
    dict(code="603516", name="淳中科技", cat="一直拉升的回调买点", win=160,
         case_date="2025-10",
         theme="算力 / 显示控制",
         note=("① 同昂利康：一波流拉升后回调白线\n"
               "② 顶部不放量 → 未出货")),
    dict(code="603117", name="澄天伟业", cat="长期盘整消化洗盘", win=200,
         case_date="2025-11",
         theme="智能卡 / 芯片",
         note=("① 前期涨幅较大，曾出现顶部大风车量\n"
               "② 经过较长时间盘整，形态没有破坏\n"
               "③ 量价符合要求")),
    dict(code="002074", name="国轩高科", cat="长期盘整消化洗盘", win=200,
         case_date="2025-11",
         theme="固态电池",
         note=("① 同澄天伟业：长期盘整消化洗盘\n"
               "② 形态未破坏 + 量价符合")),
]


# ---------------------------------------------------------------- 绘图
def plot_case(case: dict, out_dir: str):
    code, name = case["code"], case["name"]
    win = case["win"]

    resp = get_daily_bars(code, count=win + 140)
    if resp.get("status") != "OK" or not resp.get("data"):
        print(f"  [跳过] {code} {name}: {resp.get('status')}")
        return None

    df = pd.DataFrame(resp["data"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df = calc_indicators(df)

    avail = len(df)
    if avail < 140:
        print(f"  [跳过] {code} {name}: 数据不足（{avail}）")
        return None

    df_plot = df.tail(min(win, avail)).reset_index(drop=True)
    n = len(df_plot)
    x = np.arange(n)

    # 案例时点是否落在数据窗口内
    case_dt = pd.to_datetime(case["case_date"] + "-01")
    win_start, win_end = df_plot.loc[0, "date"], df_plot.loc[n - 1, "date"]
    in_window = win_start <= case_dt <= win_end

    fig = plt.figure(figsize=(17, 12), dpi=105)
    gs = fig.add_gridspec(4, 1, height_ratios=[4.2, 1.1, 1.3, 1.6], hspace=0.09)

    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    ax4 = fig.add_subplot(gs[3])

    # ---- ① 主图：K 线（A股习惯：涨红跌绿）----
    for i in range(n):
        o, h, l, c = (df_plot.loc[i, k] for k in ("open", "high", "low", "close"))
        prev_c = df_plot.loc[i - 1, "close"] if i > 0 else o
        color = "#d62728" if c >= prev_c else "#2ca02c"
        ax1.plot([i, i], [l, h], color=color, linewidth=0.7, zorder=2)
        ax1.add_patch(plt.Rectangle((i - 0.32, min(o, c)), 0.64,
                                    max(abs(c - o), 1e-6),
                                    facecolor=color, edgecolor=color,
                                    linewidth=0.4, zorder=3))

    ax1.plot(x, df_plot["yellow"], color="#e6a800", linewidth=2.3,
             label="黄线（大哥线／知行多空线）", zorder=5)
    ax1.plot(x, df_plot["white"], color="#1f77b4", linewidth=2.1,
             label="白线（牵牛绳／短期趋势线）", zorder=6)

    # J 值大负区高亮
    neg_mask = df_plot["J"] < -10
    if neg_mask.any():
        for i in np.where(neg_mask)[0]:
            ax1.axvspan(i - 0.5, i + 0.5, color="#ffcccc", alpha=0.32, zorder=1)

    # ⚠️ 本图是「形态示意」，不标注案例时点（原 case_date 为推测值，已废弃）
    # 案例时点竖线已移除 —— 见文件头「用途与限制」章节

    title = (f"【{case['cat']}】{name}（{code}）　示例区间　"
             f"{win_start.strftime('%Y-%m-%d')} ~ {win_end.strftime('%Y-%m-%d')}　"
             f"主题：{case['theme']}")
    ax1.set_title(title, fontsize=15, fontweight="bold", pad=12)
    ax1.set_ylabel("价格", fontsize=11)
    ax1.legend(loc="upper left", fontsize=10, framealpha=0.92)
    ax1.grid(alpha=0.22, linestyle="--")
    ax1.set_facecolor("#fbfbfb")

    # ---- 禁用警示框（必须保留，防止误用为形态匹配模板）----
    ax1.text(0.985, 0.03,
             "⚠ 示意用 · 禁止用于形态匹配\n"
             "   本图行情区间与图形买点体系原案例无对应关系\n"
             "   案例时点未经验证，不构成模板依据",
             transform=ax1.transAxes, fontsize=10, color="#b71c1c",
             ha="right", va="bottom", linespacing=1.6,
             bbox=dict(boxstyle="round,pad=0.55", facecolor="#ffebee",
                       edgecolor="#c62828", linewidth=1.4, alpha=0.96), zorder=10)

    # ---- ② 成交量（判断「缩半量」的关键）----
    for i in range(n):
        prev_c = df_plot.loc[i - 1, "close"] if i > 0 else df_plot.loc[i, "open"]
        up = df_plot.loc[i, "close"] >= prev_c
        color = "#d62728" if up else "#2ca02c"
        ax2.bar(i, df_plot.loc[i, "volume"], width=0.66, color=color, alpha=0.85)
    ax2.plot(x, df_plot["vma5"], color="#ff7f0e", linewidth=1.2, label="MA5")
    ax2.plot(x, df_plot["vma10"], color="#9467bd", linewidth=1.1, label="MA10")
    ax2.set_ylabel("成交量", fontsize=10)
    ax2.legend(loc="upper left", fontsize=8.5, ncol=2, framealpha=0.9)
    ax2.grid(alpha=0.2, linestyle="--")
    ax2.set_facecolor("#fbfbfb")
    plt.setp(ax2.get_xticklabels(), visible=False)

    # ---- ③ KDJ ----
    ax3.plot(x, df_plot["K"], color="#1f77b4", linewidth=1.2, label="K")
    ax3.plot(x, df_plot["D"], color="#e6a800", linewidth=1.2, label="D")
    ax3.plot(x, df_plot["J"], color="#d62728", linewidth=1.5, label="J")
    ax3.axhline(0, color="#999999", linewidth=0.9)
    ax3.axhline(-10, color="#ff6600", linewidth=1.0, linestyle="--", alpha=0.85)
    ax3.axhline(85, color="#0088cc", linewidth=0.9, linestyle="--", alpha=0.6)
    ax3.text(0.5, -10, " J<-10 大负区（B1 候选）", color="#ff6600",
             fontsize=8.5, va="bottom")
    ax3.text(0.5, 85, " J>85 超涨区", color="#0088cc",
             fontsize=8.5, va="bottom")
    ax3.set_ylabel("KDJ", fontsize=10)
    ax3.legend(loc="upper left", fontsize=8.5, ncol=3, framealpha=0.9)
    ax3.grid(alpha=0.2, linestyle="--")
    ax3.set_facecolor("#fbfbfb")
    ax3.set_ylim(min(-35, df_plot["J"].min() * 1.15),
                 max(115, df_plot["J"].max() * 1.15))
    plt.setp(ax3.get_xticklabels(), visible=False)

    # ---- x 轴刻度 ----
    ticks = np.linspace(0, n - 1, min(14, n)).astype(int)
    ax1.set_xticks(ticks)
    ax1.set_xticklabels([df_plot.loc[i, "date"].strftime("%m-%d") for i in ticks],
                        fontsize=9)

    # ---- ④ 文字面板 ----
    ax4.axis("off")
    last = df_plot.loc[n - 1]
    txt = (f"图形定义 ／ 形态特征（来源：公开文字版《图形买点体系 10 张经典图形》）\n"
           f"{'─' * 118}\n"
           f"{case['note']}\n\n"
           f"本图数据窗口末值　收盘 {last['close']:.2f}　"
           f"区间涨跌 {(last['close'] / df_plot.loc[0, 'close'] - 1) * 100:+.1f}%　"
           f"黄线 {last['yellow']:.2f}　"
           f"白线 {last['white']:.2f}　"
           f"J {last['J']:.1f}　"
           f"白线{'在' if last['white'] > last['yellow'] else '低于'}黄线"
           f"{'上（多头）' if last['white'] > last['yellow'] else '下（空头）'}\n"
           f"{'─' * 118}\n"
           f"⚠ 上列文字为图形定义转述，不构成本图行情的判定结论；\n"
           f"   案例时点未经验证，本图禁止用于形态匹配。")
    ax4.text(0.01, 0.92, txt, transform=ax4.transAxes, fontsize=11,
             va="top", ha="left",
             bbox=dict(boxstyle="round,pad=0.7", facecolor="#f5f5f5",
                       edgecolor="#cccccc", linewidth=1))

    os.makedirs(out_dir, exist_ok=True)
    safe_cat = case["cat"].replace("/", "_")
    out = os.path.join(out_dir, f"{code}_{name}_{safe_cat}.png")
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [OK] {out}")
    return out


def main():
    print(f"中文字体: {FONT}")
    out_dir = os.path.join(ROOT, "data", "kb_manual", "charts")
    print(f"输出目录: {out_dir}\n")

    made = []
    for case in CASES:
        p = plot_case(case, out_dir)
        if p:
            made.append(p)

    print(f"\n完成：{len(made)}/{len(CASES)} 张")

    idx = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "utils.data_router.get_daily_bars（腾讯，约261条上限）",
        "note": "用真实行情重绘「图形买点体系10张经典图形」案例，含黄白线+KDJ+量能",
        "charts": made,
    }
    with open(os.path.join(out_dir, "_index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    print("索引已写 _index.json")


if __name__ == "__main__":
    main()
