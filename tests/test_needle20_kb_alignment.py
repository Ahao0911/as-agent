# -*- coding: utf-8 -*-
"""
QA 验证脚本 — 需求1：needle20 模块与 ima 知识库《知行深V.txt》一致性修正
验证人：严过关（software-qa-engineer）
日期：2026-09-10

知识库原文（硬事实基准）:
    四线归零买:=IF((短期<=6 AND 中期<=6 AND 中长期<=6 AND 长期<=6),-30,0);
    白线下20买:=IF(短期<=20 AND 长期>=60,-30,0);
    白穿红线买:=IF(((CROSS(短期,长期) AND 长期<20)),-30,0);
    白穿黄线买:=IF(((CROSS(短期,中期) AND 中期<30)),-30,0);
    参数：N1=3, N2=21
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from utils.needle20 import needle20_lines, needle20_signal

# ---------------------------------------------------------------------------
# 工具：构造 OHLC 数据，使指定周期的位置指标落到目标值
# pct_line 定义: pct = (close - LLV(low,n)) / (HHV(close,n) - LLV(low,n)) * 100
# ---------------------------------------------------------------------------
RESULTS = []


def _record(name, passed, detail):
    RESULTS.append((name, passed, detail))
    flag = "✅ PASS" if passed else "❌ FAIL"
    print(f"[{flag}] {name}\n         {detail}")


def build_df(close, high=None, low=None):
    close = np.asarray(close, dtype=float)
    if high is None:
        high = close + 0.5
    if low is None:
        low = close - 0.5
    return pd.DataFrame({
        "open": close,
        "high": np.asarray(high, dtype=float),
        "low": np.asarray(low, dtype=float),
        "close": close,
        "volume": np.full(len(close), 1e6),
    })


# ===========================================================================
# 用例1：四线归零 — 长期=5（落在 4<l<=6 的争议区间）
# 手工构造：让最后一根 短期/中期/中长期/长期 都 <=6，且 长期 恰好 =5
# ===========================================================================
def case_four_zero_l5():
    print("\n===== 用例1：四线归零（长期=5 边界）=====")
    # 设计：最近21根 close 全部相等，low 全部相等 → 所有周期 pct 都会是同一值
    # 让 3K/10K/20K 周期内 close 恒定为 100 → pct=100 不对。
    # 改为：用「前段高位、末端低位」构造，使各周期 pct 落到低位。
    # 直接验证 lines 算出的实际值，再手动代入期望公式对比。
    n = 40
    # 制造连续下跌，末端 close 创近期新低
    close = np.linspace(120, 100, n)
    low = close - 0.3
    high = close + 0.3
    df = build_df(close, high, low)
    lines = needle20_lines(df)
    last = lines.iloc[-1]
    print(f"         实际四线末值: 短期={last['短期']}, 中期={last['中期']}, "
          f"中长期={last['中长期']}, 长期={last['长期']}")
    # 断言：lines 计算与手工公式一致（短期周期=3）
    # 手工复算 长期 = (c - LLV(low,21))/(HHV(close,21)-LLV(low,21))*100
    c = float(close[-1])
    llv = float(low[-21:].min())
    hhv = float(close[-21:].max())
    expect_l = round((c - llv) / (hhv - llv) * 100, 2)
    _record("1a 长期线公式复算一致",
            abs(expect_l - float(last['长期'])) < 0.01,
            f"手工={expect_l}, 模块={float(last['长期'])}")

    # 直接测信号函数：构造四线均低（含长期=5）的 DataFrame，检查 四线归零 是否触发
    # 用「降低 close 使长期正好接近5」的方式：让 c 接近 LLV(low,21)
    # 构造：前20根 close 从 120 阶梯到 101，最后一根 close=100（=区间低点附近）
    c2 = np.concatenate([np.linspace(130, 101, n - 1), [100.0]])
    low2 = c2 - 0.5
    high2 = c2 + 0.5
    # 让 low 的第21周期最小值 = 100.0（最后一根low=99.5）→ LLV=99.5
    df2 = build_df(c2, high2, low2)
    lines2 = needle20_lines(df2)
    lv = float(lines2['长期'].iloc[-1])
    sv = float(lines2['短期'].iloc[-1])
    mv = float(lines2['中期'].iloc[-1])
    mlv = float(lines2['中长期'].iloc[-1])
    print(f"         构造后末值: 短期={sv}, 中期={mv}, 中长期={mlv}, 长期={lv}")
    res2 = needle20_signal(df2)
    # 若四线都 <=6，则修正后必须触发；修正前 长期=5 不满足 <=4 → 不触发
    if sv <= 6 and mv <= 6 and mlv <= 6 and 4 < lv <= 6:
        _record("1b 四线归零：长期 5 (4<long<=6) 应触发",
                res2["四线归零"] is True,
                f"长期={lv} ∈ (4,6]；期望触发=True, 实际={res2['四线归零']}")
    else:
        print(f"         ⚠ 未能精确构造 长期∈(4,6] 且四线<=6 的场景（四线={sv},{mv},{mlv},{lv}），"
              f"改用逻辑直接验证")


# ===========================================================================
# 用例2：白穿黄线 — 核心 bug 修复（穿越对象 长期→中期）
# ===========================================================================
def case_white_cross_yellow():
    print("\n===== 用例2：白穿黄线（核心 bug 修复）=====")
    # 逐行构造，直接用 needle20_lines 的返回值反推需要的 close 序列较复杂，
    # 这里用「拼接法」：让倒数第2天 短期<=中期，最后一天 短期>中期 且 中期<30，
    # 同时让 长期 处于高位（>30），以区分「穿黄线」与「穿红线」。
    # 关键：模块通过 rolling 计算，我们直接构造使 短期 由下向上穿 中期。
    #
    # 策略：先构造一段使 中期(10K) 稳定的横盘（中期值固定），
    # 再让最后一根 close 大幅拉升 → 短期(3K) 跳升穿过中期。
    n = 60
    base = np.full(n, 100.0)
    # 前段制造缓慢下跌，使末段低点确定
    close = base.copy()
    # 使最近 10 根在一个窄幅低位区，close 略低于区间中值 → 中期约 25 左右
    for i in range(n - 10, n):
        close[i] = 100.0
    low = close - 2.0     # LLV 会取到 close-2
    high = close + 2.0

    # 最后一天拉升
    close[-1] = 103.0
    high[-1] = 103.5
    low[-1] = 102.0

    df = build_df(close, high, low)
    lines = needle20_lines(df)
    li = lines.iloc[-1]
    lprev = lines.iloc[-2]
    print(f"         末日: 短={li['短期']}, 中={li['中期']}, 中长={li['中长期']}, 长={li['长期']}")
    print(f"         前日: 短={lprev['短期']}, 中={lprev['中期']}, 长={lprev['长期']}")
    res = needle20_signal(df)
    print(f"         信号字典: 白穿黄线={res['白穿黄线']}, 白穿红线={res['白穿红线']}")
    print(f"         信号文本: {res['信号']}")
    _record("2a 白穿黄线字典字段存在且为 bool",
            isinstance(res["白穿黄线"], bool),
            f"type={type(res['白穿黄线']).__name__}")


# ===========================================================================
# 用例3：白穿红线 长期=20 严格不等号
# 直接对信号判断做逻辑等价验证：用 monkeypatch 替换 needle20_lines 返回固定值
# ===========================================================================
def case_white_cross_red_strict():
    print("\n===== 用例3：白穿红线 长期=20 严格不等号 =====")
    import utils.needle20 as m

    def make_lines(s, m_, ml, l, sp, mp, lp):
        # 两行数据：前一行 = prev 值，后一行 = 当前值
        return pd.DataFrame({
            "短期": [sp, s],
            "中期": [mp, m_],
            "中长期": [ml, ml],
            "长期": [lp, l],
        })

    orig = m.needle20_lines

    # 构造：前日 短期<=长期，今日 短期>长期，长期=20 恰好等于
    m.needle20_lines = lambda df: make_lines(s=25, m_=10, ml=10, l=20, sp=19, mp=10, lp=20)
    df = build_df([100, 100])
    r20 = m.needle20_signal(df)
    m.needle20_lines = lambda df: make_lines(s=25, m_=10, ml=10, l=19.99, sp=19, mp=10, lp=19.99)
    r1999 = m.needle20_signal(df)
    m.needle20_lines = lambda df: make_lines(s=25, m_=10, ml=10, l=20.01, sp=19, mp=10, lp=20.01)
    r2001 = m.needle20_signal(df)
    m.needle20_lines = orig

    _record("3a 白穿红线 长期=20 → 不触发 (<20 严格)",
            r20["白穿红线"] is False,
            f"长期=20: 白穿红线={r20['白穿红线']} (期望 False)")
    _record("3b 白穿红线 长期=19.99 → 触发",
            r1999["白穿红线"] is True,
            f"长期=19.99: 白穿红线={r1999['白穿红线']} (期望 True)")
    _record("3c 白穿红线 长期=20.01 → 不触发",
            r2001["白穿红线"] is False,
            f"长期=20.01: 白穿红线={r2001['白穿红线']} (期望 False)")


# ===========================================================================
# 用例4：四线归零 阈值 <=6（含 长期=5 应触发，长期=7 不触发）
# 用 monkeypatch 精确验证阈值
# ===========================================================================
def case_four_zero_threshold():
    print("\n===== 用例4：四线归零阈值（长期5 vs 4 vs 6 vs 7）=====")
    import utils.needle20 as m

    def make_lines(lval):
        return pd.DataFrame({
            "短期": [5.0, 5.0],
            "中期": [5.0, 5.0],
            "中长期": [5.0, 5.0],
            "长期": [lval, lval],
        })

    orig = m.needle20_lines
    df = build_df([100, 100])
    out = {}
    for lv in [4.0, 5.0, 6.0, 6.01, 7.0]:
        m.needle20_lines = (lambda v: (lambda d: make_lines(v)))(lv)
        out[lv] = m.needle20_signal(df)["四线归零"]
    m.needle20_lines = orig

    _record("4a 四线归零 长期=4 → 触发",
            out[4.0] is True, f"长期=4: {out[4.0]} (期望 True)")
    _record("4b 四线归零 长期=5 → 触发（修正前≤4会漏，修正后≤6应触发）",
            out[5.0] is True,
            f"长期=5: {out[5.0]} (期望 True；旧逻辑 l<=4 会得 False)")
    _record("4c 四线归零 长期=6 → 触发",
            out[6.0] is True, f"长期=6: {out[6.0]} (期望 True)")
    _record("4d 四线归零 长期=6.01 → 不触发",
            out[6.01] is False, f"长期=6.01: {out[6.01]} (期望 False)")
    _record("4e 四线归零 长期=7 → 不触发",
            out[7.0] is False, f"长期=7: {out[7.0]} (期望 False)")


# ===========================================================================
# 用例5：白穿黄线 穿越对象 中期 vs 长期（区分性验证）
# ===========================================================================
def case_cross_yellow_vs_red_object():
    print("\n===== 用例5：白穿黄线 vs 白穿红线 穿越对象区分 =====")
    import utils.needle20 as m

    def make_lines(s, m_, ml, l, sp, mp, lp):
        return pd.DataFrame({
            "短期": [sp, s], "中期": [mp, m_],
            "中长期": [ml, ml], "长期": [lp, l],
        })

    orig = m.needle20_lines
    df = build_df([100, 100])

    # 场景A：白线上穿中期(黄线) 且 中期=25<30；但 短期/长期 并不交叉，长期=80（高）
    # 期望: 白穿黄线=True，白穿红线=False
    m.needle20_lines = lambda d: make_lines(s=26, m_=25, ml=50, l=80, sp=24, mp=25, lp=80)
    A = m.needle20_signal(df)
    # 场景B：白线上穿长期(红线) 但 中期远离低位(80) —— 旧逻辑误把「穿红线且长期<=30」当白穿黄线
    # 构造 短期穿越长期，长期=25<=30，中期=80
    # 期望: 白穿黄线=False（因为对象是中期，中期=80不满足<30），白穿红线=True（长期25<20? 否→False）
    m.needle20_lines = lambda d: make_lines(s=26, m_=80, ml=80, l=25, sp=24, mp=80, lp=25)
    B = m.needle20_signal(df)
    # 场景C：白线上穿长期 且 长期=25<30，且同时短期也上穿中期? 分别控制
    # 旧逻辑(l<=30)会在 B 场景误报白穿黄线；新逻辑(中期<30)应不报
    m.needle20_lines = orig

    _record("5a 白穿黄线：短上穿中期(25) & 长期=80 → 应触发",
            A["白穿黄线"] is True and A["白穿红线"] is False,
            f"白穿黄线={A['白穿黄线']}(期望True), 白穿红线={A['白穿红线']}(期望False)")
    _record("5b 白穿黄线：短上穿长期(25) 但中期=80 → 不应触发（旧逻辑会误报）",
            B["白穿黄线"] is False,
            f"白穿黄线={B['白穿黄线']} (期望 False；旧逻辑 s>l & l<=30 会得 True)")
    _record("5c 白穿红线：短上穿长期(25, 不减20) → 不触发",
            B["白穿红线"] is False,
            f"白穿红线={B['白穿红线']} (期望 False；长期=25 不满足 <20)")


# ===========================================================================
# 用例6：m_prev 边界初始化（数据仅1根时 last>0 为 False）
# ===========================================================================
def case_m_prev_boundary():
    print("\n===== 用例6：m_prev 边界初始化（单根数据）=====")
    df1 = build_df([100.0])
    try:
        r = needle20_signal(df1)
        _record("6a 单根K线调用不报错且返回 dict",
                isinstance(r, dict) and "白穿黄线" in r,
                f"keys={list(r.keys())}")
        _record("6b 单根K线 白穿黄线=False（无前值，不应触发）",
                r["白穿黄线"] is False,
                f"白穿黄线={r['白穿黄线']} (期望 False)")
    except Exception as e:
        _record("6a 单根K线调用不报错", False, f"异常: {e!r}")


# ===========================================================================
# 用例7：two-row / 空数据健壮性
# ===========================================================================
def case_robustness():
    print("\n===== 用例7：健壮性（两行数据）=====")
    df2 = build_df([100.0, 101.0])
    try:
        r = needle20_signal(df2)
        _record("7a 两行数据调用不报错", isinstance(r, dict), f"keys={list(r.keys())}")
    except Exception as e:
        _record("7a 两行数据调用不报错", False, f"异常: {e!r}")


# ===========================================================================
# 用例8：真实数据缓存回归（若存在）
# ===========================================================================
def case_real_data():
    print("\n===== 用例8：真实数据缓存回归 =====")
    import glob
    cands = []
    for pat in ["data/cache/*.csv", "data/*.csv", "data/cache/**/*.csv"]:
        cands.extend(glob.glob(pat, recursive=True))
    cands = [c for c in cands if os.path.isfile(c)][:3]
    if not cands:
        print("         (未找到 CSV 缓存，跳过)")
        return
    for fp in cands:
        try:
            d = pd.read_csv(fp)
            cols = {c.lower(): c for c in d.columns}
            need = ["open", "high", "low", "close"]
            if not all(k in cols for k in need):
                continue
            d2 = d.rename(columns={cols[k]: k for k in need})
            r = needle20_signal(d2)
            _record(f"8 真实数据 {os.path.basename(fp)} 调用成功",
                    isinstance(r, dict) and "短期" in r,
                    f"短期={r['短期']}, 长期={r['长期']}, "
                    f"四线归零={r['四线归零']}, 白穿黄线={r['白穿黄线']}")
            break
        except Exception as e:
            print(f"         {fp}: {e!r}")


def main():
    print("=" * 70)
    print("needle20 知识库对齐 QA 验证")
    print("=" * 70)
    case_four_zero_l5()
    case_white_cross_yellow()
    case_white_cross_red_strict()
    case_four_zero_threshold()
    case_cross_yellow_vs_red_object()
    case_m_prev_boundary()
    case_robustness()
    case_real_data()

    print("\n" + "=" * 70)
    total = len(RESULTS)
    passed = sum(1 for _, p, _ in RESULTS if p)
    print(f"汇总: {passed}/{total} 通过")
    fails = [(n, d) for n, p, d in RESULTS if not p]
    if fails:
        print("失败项:")
        for n, d in fails:
            print(f"  - {n}: {d}")
    else:
        print("全部通过 ✅")
    print("=" * 70)


if __name__ == "__main__":
    main()
