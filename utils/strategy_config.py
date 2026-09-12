# -*- coding: utf-8 -*-
"""
图形买点体系唯一参数源。方向仓位体系参数不在此文件，勿合并。
================================================================
本模块是 图形买点体系(图形买点体系 v1.0 / pattern 课件)在本项目中**唯一**的
参数定义中枢。所有与 图形买点体系相关的止损/止盈/仓位/形态阈值，都必须从
此处读取，禁止在其它模块内硬编码，以避免「同一体系多个互相矛盾口径」的
历史问题。

【边界声明】
    方向仓位体系(三不原则、认知变现、总仓位≤50%、五日线战法 -20%)是**另一套
    独立体系**，其参数**不属于本文件**，严禁合并到 StrategyConfig 中。

【口径来源】
    数值以「图形买点体系原版课件原话」为准，实战落地值时在字段注释中单独标注。

设计约束:
    - 只用标准库(dataclasses)，零外部依赖，避免循环 import。
    - 模块级导出单例 CONFIG，全局共享同一实例。
"""
from dataclasses import dataclass, field


@dataclass
class StrategyConfig:
    """图形买点体系参数配置(唯一参数源)。

    所有字段均带默认值，可直接 `StrategyConfig()` 实例化；项目统一使用
    模块级单例 `CONFIG`，不要另行构造，以保证全局口径一致。
    """

    # ── 执行层 · 止损 ──────────────────────────────────────────────
    # 图形买点体系原版：「买入 K 线最低点或 3-5 个价位」为止损参考；
    # 本项目实战落地取 -4% 盘中硬止损(与 sim_portfolio 原有 0.96 等价)。
    STOP_LOSS_INTRADAY_PCT: float = 4.0
    # 图形买点体系原版：收盘前不破位方可持有；本项目实战取收盘前 -3% 止损(比盘中更严)。
    STOP_LOSS_CLOSE_PCT: float = 3.0
    # 图形买点体系原版：买入次日跌破成本即减弱；本项目实战取 -2% T1 预警线。
    T1_LOSS_PCT: float = 2.0

    # ── 形态阈值 · 砖型图 ─────────────────────────────────────────
    # 图形买点体系原版：红砖高度达到前绿柱的 2/3 视为「绿翻强红」信号。
    BRICK_RATIO: float = 2.0 / 3.0

    # ── 形态阈值 · 单针下30 ────────────────────────────────────────
    # 图形买点体系原版：单针下30 公式判据——红线(长期线)达 85 视为超买。
    NEEDLE_OVERBOUGHT_FORMULA: int = 85
    # 图形买点体系原版：实战修正的宽容取值，红线 80 即提示超买(落地实盘)。
    NEEDLE_OVERBOUGHT_PRACTICAL: int = 80

    # ── 形态阈值 · 单针下20（「白线下20买」）长期线门槛 ─────────────
    # 2026-09-11 用户确认启用。来源：全市场 4935 只入场门槛扫描结果
    # （长期≥85 → ALL 52.9% / IS 53.5% / OOS 50.7%，首个 IS+OOS 双 ≥50% 配置）。
    # ⚠ 无知识库原文依据（知识库口径为「长期≥60」），属统计扫参；已扫描 80/85/90
    #   多阈值后择优，存在多重比较风险，OOS 50.7% 可能被高估。待独立样本段复核。
    NEEDLE_LONG_MIN: int = 85
    # 知识库原始口径（《知行深V.txt》「白线下20买:=IF(短期<=20 AND 长期>=60,...)」）：
    # 仅作对照/回滚基准，不参与生产判定。若独立样本验证（2018-2023 单独测 85）
    # 显示样本外明显低于 50%，则将 NEEDLE_LONG_MIN 回滚至此值。
    NEEDLE_LONG_MIN_KB: int = 60

    # ── 战法内定 · 持有期（2026-09-12 用户定稿）──────────────────────
    # 用户裁决原话：「B3 中继，咱们就确定敲为 10 日吧。其他的战法日子感觉不规定，
    # 可以把日子全部去掉，然后写进战法内就行。」
    # → 不再使用全局「5日 / 10日 / 20日」网格去"规定"持有期；持有期属于**战法自身
    #   的定义**。取值语义：
    #     int  = 该战法固定持有 N 个交易日（战法内定参数）
    #     None = **规则驱动**：不人为规定日数，由该战法自身的离场规则（止损/止盈/
    #            放飞/红翻绿等）决定实际持有天数。回测不得再用固定 N 日近似。
    # ⚠ 事件研究法脚本（utils/event_study.py）的固定 N 日仅作「信号质量测量」用途，
    #   不代表战法真实持有期；对外汇报一律以规则驱动结果为准。
    HOLD_DAYS_BY_STRAT: dict = field(default_factory=lambda: {
        "B1": None,    # 规则驱动：等 B2 出现 / 结构破位离场，不规定固定日数
        "B2": None,    # 规则驱动：跟随 B1 主仓（加仓位），不规定固定日数
        "B3": 10,      # ★用户敲定：中继K线 固定持有 10 个交易日
        "砖型": None,   # 规则驱动：红翻绿止盈 / 白线破黄线止损
        "单针": None,   # 规则驱动：红线跌破 60 止损 / 白线高位拐头离场
    })

    # ── 模拟盘规则版本（2026-09-12 引入 v2；v1 完整保留，见 docs/模拟盘规则_v1_v2.md）
    # 回滚：把 SIM_RULES_VERSION 置为 1 即恢复 v1 行为（B2 可独立买入、加仓无差别加倍），
    #       无需改动 git；v1 的完整代码基线另有 git tag `v1-sim`。
    SIM_RULES_VERSION: int = 2

    # ── v2 变更项（SIM_RULES_VERSION >= 2 时生效）───────────────────
    # 变更1：B3 中继固定持有 10 个交易日 → 见 HOLD_DAYS_BY_STRAT["B3"] = 10
    # 变更2：B2 不再作独立买入信号，只作 B1 的加仓位
    #   依据：全市场回测 2026-09-12，B2 独立口径交易胜率仅 31.5%
    B2_AS_INDEPENDENT_BUY: bool = False
    # 变更3：加仓 = 半仓追加（B1 建仓额 × 50%）
    #   依据：全量 3737 笔加仓腿 +1.604%/笔(胜率52.8%)；加仓后整笔 +4.279%
    #          vs 不加仓 +3.798% → 增利 +0.481pp
    #   ⚠ 代价：整笔胜率 82.5% → 63.8%，必须配合既有单票敞口上限，不得放宽敞口
    ADD_ON_RATIO: float = 0.50
    # 加仓腿跟随主仓离场（不另配 B2 短周期规则——实测另配反而最差：+0.993%/中位 −4.68%）
    ADD_ON_FOLLOW_MAIN_EXIT: bool = True
    # 不设"仅盈利后加仓"：实测加仓时 B1 腿浮盈中位 +9.57%、>0 占比 100%，该条件天然失效
    ADD_ON_REQUIRE_PROFIT: bool = False

    # ── v1 遗留值（保留以备回滚，勿删）──────────────────────────────
    V1_B2_AS_INDEPENDENT_BUY: bool = True     # v1：B2 可独立买入
    V1_ADD_ON_RATIO: float = 1.00             # v1：见大阳线即加倍
    V1_ADD_ON_FOLLOW_MAIN_EXIT: bool = True

    # ── 仓位管理 · 模拟盘分组 ──────────────────────────────────────
    # 图形买点体系战法分组：每组 25 万预算，共 4 组 = 100 万总资金(2026-08-20 用户确认)。
    GROUP_BUDGET: float = 250000
    # 图形买点体系原版：单只买入占组预算 50%(约 12.5 万)，单组最多同时持有 2 只。
    BUY_RATIO: float = 0.50
    # 实战约束：全抡让渡加仓后需保留约 2 万元现金(按手取整残差缓冲)。
    RESERVE_CASH: float = 20000

    # ── 风控护栏 · 单票敞口上限 ────────────────────────────────────
    # 单票持仓金额 ≤ 组预算 × 1.5(25万 → 37.5万)。
    # 用户 2026-09-11 确认新增，对治事故：2026-09-02 全抡让渡把 300191 潜能恒信
    # 加仓到约 60.9 万(组预算 25 万的 2.4 倍)，现金被压到 11.1 万致连续 5 日无法建仓。
    MAX_SINGLE_POSITION_BUDGET_RATIO: float = 1.5
    # 单票持仓金额 ≤ 总资产 × 15%。
    # 用户 2026-09-11 确认新增，同一事故对治：该笔 300191 于 2026-09-11 止损 @34.13，
    # 单笔亏损 -35,179.20 元，占账户累计亏损近一半。双口径取 min，防止资金过度集中。
    MAX_SINGLE_POSITION_TOTAL_RATIO: float = 0.15

    def stop_price_for(self, buy_price: float, mode: str = "intraday") -> float:
        """按买入价与止损模式计算止损价。

        Args:
            buy_price: 买入成本价(正整数/浮点数)。
            mode: 止损模式，取值:
                - "intraday": 盘中硬止损 -4%(默认)
                - "close"   : 收盘前止损 -3%
                - "t1"      : T1 预警线 -2%

        Returns:
            float: 止损价，保留 3 位小数。

        Raises:
            ValueError: 当 mode 非法或 buy_price 非正时。
        """
        pct_map = {
            "intraday": self.STOP_LOSS_INTRADAY_PCT,
            "close": self.STOP_LOSS_CLOSE_PCT,
            "t1": self.T1_LOSS_PCT,
        }
        if mode not in pct_map:
            raise ValueError(f"未知止损模式: {mode!r}, 可选: {list(pct_map)}")
        price = float(buy_price)
        if price <= 0:
            raise ValueError(f"买入价必须为正: {buy_price!r}")
        pct = pct_map[mode]
        return round(price * (1 - pct / 100.0), 3)

    def scale_in_amount(self, group_budget: float = None) -> float:
        """计算单只买入/加仓金额(默认组预算 × BUY_RATIO)。

        Args:
            group_budget: 组预算，默认取 self.GROUP_BUDGET。

        Returns:
            float: 单只买入金额(未按手取整，调用方自行取整)。
        """
        budget = self.GROUP_BUDGET if group_budget is None else float(group_budget)
        return budget * self.BUY_RATIO

    def hold_days_for(self, strat: str):
        """取某战法的持有期（战法内定）。

        Args:
            strat: 战法名，取值 "B1" / "B2" / "B3" / "砖型" / "单针"。

        Returns:
            int | None: 固定持有的交易日数；**None 表示规则驱动**——由该战法
            自身的离场规则决定实际持有天数，调用方不得再用固定 N 日近似。

        Raises:
            KeyError: 战法名不在 HOLD_DAYS_BY_STRAT 中。
        """
        if strat not in self.HOLD_DAYS_BY_STRAT:
            raise KeyError(f"未知战法: {strat!r}, 可选: {list(self.HOLD_DAYS_BY_STRAT)}")
        return self.HOLD_DAYS_BY_STRAT[strat]

    def sim_rules(self) -> dict:
        """返回当前生效的模拟盘规则集（按 SIM_RULES_VERSION 切换 v1 / v2）。

        用途：让"规则版本"成为**唯一可查的显式对象**，避免调用方各自硬编码分歧。
        v2 依据见 docs/模拟盘规则_v1_v2.md（全市场回测 2026-09-12）。

        Returns:
            dict: 含 version / b2_independent_buy / b3_hold_days / add_on_ratio /
                  add_on_follow_main_exit / add_on_require_profit 六个键。
        """
        if self.SIM_RULES_VERSION >= 2:
            return {
                "version": 2,
                "b2_independent_buy": self.B2_AS_INDEPENDENT_BUY,
                "b3_hold_days": self.HOLD_DAYS_BY_STRAT.get("B3"),
                "add_on_ratio": self.ADD_ON_RATIO,
                "add_on_follow_main_exit": self.ADD_ON_FOLLOW_MAIN_EXIT,
                "add_on_require_profit": self.ADD_ON_REQUIRE_PROFIT,
            }
        return {
            "version": 1,
            "b2_independent_buy": self.V1_B2_AS_INDEPENDENT_BUY,
            "b3_hold_days": None,
            "add_on_ratio": self.V1_ADD_ON_RATIO,
            "add_on_follow_main_exit": self.V1_ADD_ON_FOLLOW_MAIN_EXIT,
            "add_on_require_profit": False,
        }


# 全局唯一参数源单例(全项目共享，勿另行构造 StrategyConfig)
CONFIG = StrategyConfig()


if __name__ == "__main__":
    # 自测: 核对三大止损口径与加仓金额
    print("盘中 -4%:", CONFIG.stop_price_for(10.0, "intraday"))   # 9.6
    print("收盘 -3%:", CONFIG.stop_price_for(10.0, "close"))      # 9.7
    print("T1  -2%:", CONFIG.stop_price_for(10.0, "t1"))          # 9.8
    print("单只金额:", CONFIG.scale_in_amount())                  # 125000.0
    for _s in CONFIG.HOLD_DAYS_BY_STRAT:
        _h = CONFIG.hold_days_for(_s)
        print(f"持有期 {_s}: {'固定 %d 日' % _h if _h else '规则驱动'}")
    print("模拟盘规则版本:", CONFIG.sim_rules())
