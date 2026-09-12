"""
图形买点体系B1战法引擎（原六节点战法） — B1B2B3（三买）/ S1S2S3（三卖）
+ 红肥绿瘦量价结构 + J值分级

核心: B1战法以B1(超跌首买)为核心标尺，配合三买三卖六节点定位当前阶段
B1=超跌首买(J大负值) B2=突破确认(破黄线/前高) B3=加速主升(J钝化)
S1=爆发冲顶(J大正值钝化) S2=M顶构筑(双头) S3=顶背离出货(新高指标不新高)

灵活原则: 各信号独立触发、独立参考，叠加越多胜率倾向越高，不搞死板共振；
唯一硬性前提是大趋势（黄白线多头）。

用法:
    from utils.battle import analyze_nodes
    r = analyze_nodes(df, dual, brick, needle)
"""
import numpy as np
import pandas as pd


def analyze_red_fat_green_thin(df, lookback=14):
    """
    红肥绿瘦量价结构判断
    - 红肥: 阳量总和/阴量总和 > 1.65（主力吸筹）
    - 绿瘦: 回调段缩量（下跌日量 < 前5日均量50%占比）
    返回: dict {红肥: bool, 阳阴量比, 缩量回调占比}
    """
    c = df["close"].astype(float)
    o = df["open"].astype(float)
    vol = df["volume"].astype(float)

    yang = c >= o
    yin = c < o

    vol_yang = vol.where(yang, 0).rolling(lookback, min_periods=1).sum()
    vol_yin = vol.where(yin, 0).rolling(lookback, min_periods=1).sum() + 1e-9
    ratio = vol_yang.iloc[-1] / vol_yin.iloc[-1]

    # 绿瘦: 回调日（阴线）成交量萎缩至前5日均量50%以下的比例
    vol5 = vol.rolling(5, min_periods=1).mean()
    thin_days = (yin & (vol < vol5 * 0.5)).iloc[-lookback:].sum()
    yin_days = max(1, int(yin.iloc[-lookback:].sum()))
    thin_ratio = float(thin_days) / yin_days

    return {
        "红肥": bool(ratio > 1.65),
        "阳阴量比": round(ratio, 2),
        "缩量回调占比": round(thin_ratio, 2),
        "结论": "红肥绿瘦（主力吸筹）" if ratio > 1.65 and thin_ratio > 0.3
               else "红瘦绿肥（警惕见顶）" if ratio < 1.0
               else "量价中性",
    }


def j_level(j_val):
    """J值分级"""
    if j_val <= -10:
        return "深度超跌(J<-10)，极端恐慌，黄金坑概率大但警惕下跌中继"
    if j_val <= -5:
        return "标准B1区间(J -5~-10)，典型超跌买点"
    if j_val < 0:
        return "轻度超跌(J 0~-5)，普通回调可观察"
    if j_val >= 100:
        return "J大正值钝化(J≥100)，S1冲顶信号，短期情绪极致亢奋"
    if j_val >= 80:
        return "高位(J 80~100)，注意冲高回落"
    return f"中性(J {j_val:.0f})"


def analyze_nodes(df, dual=None, brick=None, needle=None, turnover=None, needle_pattern=None):
    """
    B1战法分析
    df: K线DataFrame (open/high/low/close/volume)
    dual: analyze_dual_line()结果（可选）
    brick: brick_signal()结果（可选）
    needle: needle20_signal()结果（可选）
    turnover: turnover_signal()结果（可选，换手率过滤器）
    返回: dict
    """
    from utils.indicators import kdj_j

    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    vol = df["volume"].astype(float)

    # 基础指标
    _, _, j = kdj_j(h, l, c)
    j_series = j
    j_now = float(j_series.iloc[-1])
    j_prev = float(j_series.iloc[-2]) if len(j_series) > 1 else j_now
    # J值钝化检测（连续多日在100以上）
    j_over100 = (j_series.iloc[-5:] >= 100).sum() if len(j_series) >= 5 else 0

    # 价格结构
    c_now = float(c.iloc[-1])
    c_prev = float(c.iloc[-2]) if len(c) > 1 else c_now
    c_5ago = float(c.iloc[-6]) if len(c) > 6 else c_now
    c_20ago = float(c.iloc[-21]) if len(c) > 21 else c_now
    c_60ago = float(c.iloc[-61]) if len(c) > 61 else c_now

    # 前高 / 颈线（20日高点）
    hh20 = float(h.iloc[-21:-1].max()) if len(h) > 21 else float(h.iloc[:-1].max())
    neckline = hh20  # 颈线=20日区间高点

    # 量比（当日/前日）
    vol_now = float(vol.iloc[-1])
    vol_prev = float(vol.iloc[-2]) if len(vol) > 1 else vol_now
    vol_ratio = vol_now / vol_prev if vol_prev > 0 else 0

    # 当日阴阳
    is_yang = bool(c.iloc[-1] >= o.iloc[-1])

    # 黄白线
    wl_val = dual.get("白线", 0) if dual else 0
    yl_val = dual.get("黄线", 0) if dual else 0
    bull = dual.get("多头区间", False) if dual else False

    # ===== 节点判断（每个信号独立触发，共振只是加分项） =====
    nodes = []
    detections = []
    b2_strong = False  # 初始化，供B2放量突破使用

    # --- B1: 超跌首买（J大负值） ---
    if j_now <= -5:
        msg = "★ B1信号：J大负值超跌"
        if bull:
            msg += " + 大趋势多头 → 顺大势逆小势，试错建仓(10-20%)"
        else:
            msg += "（大趋势非多头，按体系应降低预期或放弃）"
        detections.append(msg)
        nodes.append("B1")

    # --- B1盈亏比测算（2026-08-03补：前高目标+止损+盈亏比，铁律≥2:1最优3:1） ---
    ll20 = float(l.iloc[-21:-1].min()) if len(l) > 21 else float(l.iloc[:-1].min())
    stop_b1 = round(ll20 * 0.97, 3)          # 止损参考=信号低点下方3%
    rr = round((hh20 - c_now) / (c_now - stop_b1), 2) if c_now > stop_b1 > 0 else 0.0
    # 保守目标口径：黄线/MA20 就近压力（前高过远时实际第一目标）
    ma20 = float(c.iloc[-20:].mean()) if len(c) >= 20 else c_now
    cons_target = max(yl_val, ma20) if yl_val > 0 else ma20
    rr_cons = round((cons_target - c_now) / (c_now - stop_b1), 2) if cons_target > c_now > stop_b1 > 0 else 0.0
    if rr >= 3:
        rr_verdict = f"✅ 盈亏比{rr}:1 最优(≥3:1)"
    elif rr >= 2:
        rr_verdict = f"✅ 盈亏比{rr}:1 达标(≥2:1)"
    else:
        rr_verdict = f"❌ 盈亏比{rr}:1 不足2:1，按体系放弃"
    if rr >= 2 and rr_cons < 2:
        rr_verdict += f"（⚠前高过远，就近压力{round(cons_target,3)}口径仅{rr_cons}:1，按黄线/MA20保守测算）"
    rr_info = {
        "前高目标": round(hh20, 3),
        "保守目标": round(cons_target, 3),
        "止损参考": stop_b1,
        "盈亏比": rr,
        "保守盈亏比": rr_cons,
        "判定": rr_verdict,
    }
    if "B1" in nodes:
        detections.append(f"止盈测算: 前高目标{rr_info['前高目标']} | 止损参考{rr_info['止损参考']} | {rr_verdict}")

    # --- B2: 突破确认（股价突破黄线/前高/颈线，需放量阳线） ---
    broke_yellow = (dual and yl_val > 0 and c_now > yl_val)
    broke_high = c_now > hh20
    # 放量突破颈线（通达信版B2：CROSS(C,颈线)+放量1.8倍+阳线）
    broke_neckline = (c_prev <= neckline and c_now > neckline)  # CROSS(C,颈线)
    vol_break = vol_ratio >= 1.8 and is_yang  # 放量阳线
    b2_strong = broke_neckline and vol_break  # 严格版B2
    if broke_yellow or broke_high:
        msg = "★ B2信号：突破压力位" + ("(黄线)" if broke_yellow else "") + ("(前高)" if broke_high else "")
        if b2_strong:
            msg += " + 放量阳线突破颈线(严格版B2确认) ✓"
        elif vol_break:
            msg += " + 放量阳线(有量) ✓"
        else:
            msg += "（无放量配合，有效性待确认）"
        if j_now > j_prev:
            msg += " + J值上行 → 右侧加仓点"
        detections.append(msg)
        nodes.append("B2")

    # --- 四分之三阴量校验（突破阳线后次日阴线量判断真假突破） ---
    # 场景：放量突破阳线之后，次日收阴线，看阴线量/阳线量比值
    # 3/4附近(50%-80%)=抛压大，大概率假突破；远小于3/4(如1/3)=良性回踩
    if len(c) >= 3:
        c_yest = float(c.iloc[-2])
        c_2ago = float(c.iloc[-3])
        o_yest = float(o.iloc[-2])
        o_2ago = float(o.iloc[-3])
        v_yest = float(vol.iloc[-2])
        v_2ago = float(vol.iloc[-3])
        # 昨日是放量阳线(突破阳线)且今日是阴线
        yang_yest = c_yest >= o_yest
        vol_break_yest = (v_yest / v_2ago >= 1.8) if v_2ago > 0 else False
        if yang_yest and vol_break_yest and not is_yang:
            vol_ratio_34 = v_2ago / v_yest if v_yest > 0 else 0  # 阴线量/阳线量
            if vol_ratio_34 >= 0.5 and vol_ratio_34 <= 0.8:
                detections.append(
                    f"⚠ 四分之三阴量警示：昨日放量阳线{v_yest:.0f}，今日阴线量{v_2ago:.0f}"
                    f"=昨日量×{vol_ratio_34:.0%} → 抛压重，假突破概率大，止损离场"
                )
            elif vol_ratio_34 < 0.5:
                detections.append(
                    f"✅ 突破后阴线缩量良性：今日阴线量{v_2ago:.0f}=昨日量×{vol_ratio_34:.0%}"
                    f" < 50% → 抛压小，良性回踩，突破有效"
                )

    # --- B3: 加速主升（快速拉升，J值跟不上=钝化） ---
    if j_now >= 80 and c_now > c_5ago * 1.08:
        detections.append("★ B3信号：加速主升，J值高位钝化 → 主升浪持有段")
        nodes.append("B3")

    # --- BBI线上两根中大阳线减半仓（卤煮规则） ---
    # 531系列：BBI(黄线)之上连续两根中大阳线(实体≥3%) → 减仓一半(放飞一半)
    if len(c) >= 3:
        yl_val_num = yl_val if yl_val > 0 else float(c.iloc[-20:].mean()) if len(c) >= 20 else 0
        # 检查最近两根K线
        if yl_val_num > 0:
            r1 = df.iloc[-1]  # 今日
            r2 = df.iloc[-2]  # 昨日
            e1 = float(r1['close']) - float(r1['open'])
            e2 = float(r2['close']) - float(r2['open'])
            ep1 = abs(e1) / float(r1['open']) * 100
            ep2 = abs(e2) / float(r2['open']) * 100
            both_yang = float(r1['close']) >= float(r1['open']) and float(r2['close']) >= float(r2['open'])
            both_above_bbi = float(r1['close']) > yl_val_num and float(r2['close']) > yl_val_num
            both_mid_large = ep1 >= 3 and ep2 >= 3
            if both_yang and both_above_bbi and both_mid_large:
                detections.append(
                    f"★ 卤煮信号：BBI线上连续两根中大阳线(实体{ep2:.1f}%+{ep1:.1f}%)"
                    f" → 减仓一半/放飞一半锁利"
                )
                nodes.append("卤煮")

    # --- S1: 爆发冲顶（J大正值持续钝化） ---
    if j_over100 >= 3 and j_now >= 90:
        detections.append("★ S1信号：J值大正值持续钝化(5日内3+天≥100) → 第一波减仓锁利")
        nodes.append("S1")
    elif j_now > 100:
        # 简单版S1：J>100即预警
        detections.append("★ S1预警：J值>100（冲顶钝化）→ 短线减仓锁定利润")
        nodes.append("S1")

    # ===== 图形买点体系止盈三信号：顶部放量大阴线 / 顶部大风车 / 倍量柱 =====
    # 高位参考：20日高点均值
    high_mean_20 = float(h.iloc[-21:-1].mean()) if len(h) > 21 else float(h.mean())
    in_s1 = ("S1" in nodes) or (j_now >= 80)

    # 最近5根K线逐个检查
    for i in range(-5, 0):
        if abs(i) > len(df) - 2:
            continue
        ri = df.iloc[i]
        prev_v = float(df.iloc[i - 1]['volume']) if i > -len(df) else 0
        vol_ratio = float(ri['volume']) / prev_v if prev_v > 0 else 0
        entity = abs(float(ri['close']) - float(ri['open']))
        entity_pct = entity / float(ri['open']) * 100 if float(ri['open']) > 0 else 0
        is_high = float(ri['high']) > high_mean_20

        # --- 1. 顶部放量大阴线 ---
        # 条件：阴线+实体跌幅>=3%+放量>=1.8倍+高位
        if float(ri['close']) < float(ri['open']) and entity_pct >= 3 and vol_ratio >= 1.8 and is_high:
            days_ago = abs(i)
            detections.append(
                f"⚠ 顶部放量大阴线({days_ago}天前)：实体跌{entity_pct:.1f}%+放量{vol_ratio:.1f}倍"
                f"→高位出货，3日不收复清仓"
            )
            break

        # --- 2. 顶部大风车（高位螺旋桨） ---
        # 条件：实体小+上下影都长(>=实体1.5倍)+高位+放量
        upper = float(ri['high']) - max(float(ri['open']), float(ri['close']))
        lower = min(float(ri['open']), float(ri['close'])) - float(ri['low'])
        if entity > 0.01 and upper >= entity * 1.5 and lower >= entity * 1.5 and is_high and vol_ratio >= 1.5:
            days_ago = abs(i)
            detections.append(
                f"⚠ 顶部大风车({days_ago}天前)：长上下影+实体小+放量{vol_ratio:.1f}倍"
                f"→多空分歧极大，见顶预警"
            )
            break

        # --- 3. 倍量柱（红色大哥来/绿色大哥走） ---
        if vol_ratio >= 1.8:
            is_red = float(ri['close']) >= float(ri['open'])
            days_ago = abs(i)
            if is_red:
                # 红色倍量柱
                if is_high and in_s1:
                    detections.append(
                        f"⚠ 高位倍量红柱({days_ago}天前)：放量{vol_ratio:.1f}倍但股价不创新高"
                        f"→高位放量滞涨，注意风险"
                    )
                else:
                    detections.append(
                        f"✅ 低位倍量红柱({days_ago}天前)：放量{vol_ratio:.1f}倍"
                        f"→红色大哥来，主力进场"
                    )
            else:
                # 绿色倍量柱
                if is_high and in_s1:
                    detections.append(
                        f"⚠ 高位倍量绿柱({days_ago}天前)：放量{vol_ratio:.1f}倍"
                        f"→绿色大哥走，主力出货，优先减仓"
                    )
                else:
                    # 上涨中途的倍量绿柱，检查次日是否反包
                    next_idx = i + 1
                    if next_idx < 0:
                        nr = df.iloc[next_idx]
                        if float(nr['close']) > float(ri['close']):
                            detections.append(
                                f"倍量绿柱({days_ago}天前)+次日反包→洗盘概率大，非出货"
                            )
                        else:
                            detections.append(
                                f"⚠ 倍量绿柱({days_ago}天前)：放量{vol_ratio:.1f}倍"
                                f"→关注是否破位"
                            )
                    else:
                        detections.append(
                            f"⚠ 倍量绿柱({days_ago}天前)：放量{vol_ratio:.1f}倍"
                            f"→关注是否破位"
                        )
            break  # 只报最近一次倍量柱

    # --- 917新增：阶梯量出货检测（新高之后连续阶梯放量下跌） ---
    # 检查最近10根K线是否有阶梯放量特征
    if len(df) > 15:
        avg_v_60 = float(vol.rolling(60, min_periods=10).mean().iloc[-2]) if len(vol) > 60 else 0
        if avg_v_60 > 0:
            step_volumes = []
            for i in range(-5, 0):
                if abs(i) > len(df) - 1:
                    continue
                ri = df.iloc[i]
                if float(ri['close']) < float(ri['open']):
                    vol_i = float(ri['volume'])
                    vr_60 = vol_i / avg_v_60 if avg_v_60 > 0 else 0
                    step_volumes.append(vr_60)
            if len(step_volumes) >= 3:
                is_step_up = all(step_volumes[i] < step_volumes[i+1] for i in range(len(step_volumes)-1))
                if is_step_up and step_volumes[-1] >= 1.5:
                    detections.append(
                        f"⚠ 阶梯量出货预警：连续{len(step_volumes)}根阴线量逐步放大"
                        f"(量比{step_volumes[0]:.1f}x→{step_volumes[-1]:.1f}x)"
                        f"→ 新高后阶梯放量下跌，麒麟会长庄出货特征"
                    )

    # --- 917新增：次高点巨量成因检测 ---
    if len(h) > 20:
        hh_highest = float(h.iloc[-21:-1].max())
        h_sorted = sorted(h.iloc[-21:-1].unique(), reverse=True)
        if len(h_sorted) >= 2:
            hh_second = h_sorted[1]
            if hh_second >= hh_highest * 0.95:
                for i in range(-10, 0):
                    if abs(i) > len(df) - 1:
                        continue
                    ri = df.iloc[i]
                    if float(ri['high']) >= hh_second * 0.98:
                        prev_v = float(df.iloc[i-1]['volume']) if i > -len(df) else 0
                        vr = float(ri['volume']) / prev_v if prev_v > 0 else 0
                        if float(ri['close']) < float(ri['open']) and vr >= 2.0:
                            days_ago = abs(i)
                            ep = abs(float(ri['close']) - float(ri['open'])) / float(ri['open']) * 100
                            detections.append(
                                f"⚠ 次高点巨量成因({days_ago}天前)：次高点放量{vr:.1f}倍阴线"
                                f"实体{ep:.1f}%→ 宁可信其有不可信其无，减仓"
                            )
                            break

    # --- S2: M顶构筑（二次冲高无力） ---
    if len(h) > 40:
        hhA = float(h.iloc[-41:-21].max()) if len(h) > 41 else hh20  # 第一头
        hhB = float(h.iloc[-21:-1].max())                            # 第二头
        second_low = c_now < hhB * 0.97 and hhB <= hhA * 1.01 and hhB >= hhA * 0.97
        if second_low:
            detections.append("★ S2信号：M顶构筑（二次冲高无力，双头≈等高）→ 第二波减仓")
            nodes.append("S2")

    # --- S3: 顶背离出货（股价新高但指标不创新高） ---
    if len(c) > 40 and len(j_series) > 40:
        price_new_high = c_now > float(c.iloc[-41:-1].max())
        j_peak_now = float(j_series.iloc[-10:].max())
        j_peak_prev = float(j_series.iloc[-41:-11].max()) if len(j_series) > 41 else j_peak_now
        if price_new_high and j_peak_now < j_peak_prev:
            detections.append("★ S3信号：顶背离（股价新高但J不创新高）→ 清仓离场信号")
            nodes.append("S3")

    # ===== 量价结构（先算，供加分项使用） =====
    rf = analyze_red_fat_green_thin(df)
    jl = j_level(j_now)

    # ===== 加分项（不构成硬性条件，仅提示信号强度） =====
    bonuses = []
    if brick:
        if brick.get("绿翻强红"):
            bonuses.append("砖型图绿翻强红（动量反转）")
        elif brick.get("今日翻红"):
            bonuses.append("砖型图翻红（动量拐点）")
        if brick.get("连续红砖", 0) >= 4:
            bonuses.append(f"已连红{brick['连续红砖']}砖（注意减仓纪律）")
    if needle:
        if needle.get("白线下20"):
            bonuses.append("单针下20回调买点")
        if needle.get("四线归零"):
            bonuses.append("单针下20四线归零（大底）")
        if needle.get("白穿红线"):
            bonuses.append("单针下20白穿红线（底部反转）")
    if rf["红肥"]:
        bonuses.append("红肥绿瘦（主力吸筹量价）")
    if turnover and "error" not in turnover:
        for s in turnover.get("信号", [])[:2]:
            bonuses.append(f"换手率: {s}")
        if turnover.get("当前换手率", 0) > 15:
            bonuses.append(f"⚠ 换手{turnover['当前换手率']}%超高 → 高位出货风险区")
        if turnover.get("相对换手", 1) < 0.5:
            bonuses.append("回调缩量（良性洗盘）")
    if needle_pattern:
        if needle_pattern.get("今日信号"):
            bonuses.append("★ 今日单针下20信号！关注次日确认")
        elif needle_pattern.get("昨日信号"):
            bonuses.append("★ 昨日单针下20信号，今日确认中")
        elif needle_pattern.get("近N日信号次数", 0) > 0:
            bonuses.append(f"近{needle_pattern.get('近N日信号次数', 0)}日出现单针下20信号（{needle_pattern.get('最近信号日', '')}）")
        # 接近信号的条件
        near_sigs = [s for s in needle_pattern.get("信号", []) if s.startswith("近信号条件")]
        if near_sigs:
            bonuses.append(near_sigs[0])

    if bonuses:
        detections.append("加分项: " + " | ".join(bonuses))

    # ===== 当前所处阶段推断 =====
    stage = "未知"
    if "S3" in nodes:
        stage = "S3 清仓离场阶段"
    elif "S2" in nodes:
        stage = "S2 M顶构筑阶段"
    elif "S1" in nodes:
        stage = "S1 冲顶减仓阶段"
    elif "卤煮" in nodes:
        stage = "卤煮 减半仓锁利阶段"
    elif "B3" in nodes:
        stage = "B3 加速主升阶段"
    elif "B2" in nodes:
        stage = "B2 突破确认阶段"
    elif "B1" in nodes:
        stage = "B1 超跌首买阶段"

    return {
        "节点": nodes,
        "当前阶段": stage,
        "J值": round(j_now, 1),
        "J值判断": jl,
        "量价": rf,
        "盈亏比": rr_info,
        "颈线": round(neckline, 2) if neckline > 0 else 0,
        "B2放量突破": b2_strong,
        "检测": detections,
    }
