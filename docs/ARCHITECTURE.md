# 架构说明

## 分层

| 层 | 职责 | 主要模块 |
|---|---|---|
| 数据层 | 行情/资讯采集、K 线缓存、限流 | `utils/data_router.py`,`utils/ftshare_data.py`,`utils/tencent.py`,`utils/mx_data.py` |
| 信号层 | 10 个策略模块 + 纪律与风控 | `utils/experts/modules/*.py`,`utils/risk_engine.py` |
| 决策层 | 盘前/盘中/盘后流程编排 | `utils/pre_market_flow.py`,`utils/midday_sim.py`,`utils/review_flow.py`,`utils/judgment_loop.py` |
| 组合层 | 模拟盘账户与仓位 | `utils/sim_portfolio.py`,`utils/sim_screener.py`,`utils/portfolio/risk_*` |
| 复盘层 | 六维评分 + 认知回写 | `utils/trade_journal.py`,`utils/daily_review.py`,`utils/forecast_store.py` |
| 工具层 | 45+ 工具函数(可被 Agent 调用) | `utils/*.py` |
| 技能层 | 7 个可插拔 Skill | `skills/*/SKILL.md` |
| 交互层 | 数据桥 + 本地看板 | `dashboard_server.py` + `trading_workbench.html` |

## Agent 运行闭环

```
每日 09:15 ─┬─ pre_market_flow.py      盘前:三情景概率加权 → 写入研判
             │
每日 11:30 ─┼─ midday_sim.py           盘中:sim_screener 代码选股 → 风控 → 入单
             │                            └─ sim_portfolio 自动止损检查
             │
每日 21:00 ─┴─ review_flow.py           盘后:六维评分 + 认知回写 → judgment_loop
```

每次执行:
1. 调 `data_router` 拉取当日快照
2. 命中策略信号 → 调 `sim_screener` 精筛
3. 通过 `risk_engine` 校验仓位与止损
4. 落盘到 `trade_journal` 与 `report_store`
5. 触发次日 `judgment_loop` 修正

## 设计原则

- **规则代码化**:所有策略与风控规则均落到 Python 工具函数,杜绝自然语言主观判断
- **可插拔**:工具与技能按"职责单一 + 文件名即 ID"组织,新增能力不需改核心链路
- **可观测**:每一步写入 `data/`,异常以结构化日志记录,便于复盘
- **可复现**:回测框架使用固定随机种子与统一的成本模型,跨平台结果一致

## 关键算法

- **0AmV(活跃市值)预测** — `utils/amv_predict.py` 使用 AR(2) 自回归 + 滚动 1500 日重拟合,无未来函数逐日验证历史 MAPE ≈ 1.2%
- **策略回测口径** — `utils/backtest.py` 实现满仓轮动复利(资金空闲跳过信号),逐信号计算胜率/盈亏比/年化/最大回撤
- **交易成本模型** — 佣金万 1 免 5 + 印花税 0.05% + 过户费万 0.1,单笔往返 ≈ 0.072%
