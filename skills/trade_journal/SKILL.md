# 交易记录技能（trade_journal）
- 触发词：记录交易、记一笔、买入、卖出、我买了、我卖了、今天操作
- 功能：记录用户实际交易（操作流水 + 行为观察），供盘后复盘六项评分使用
- 闭环位置：交易记录 → 盘后复盘逐笔评分 → 行为观察 → 画像更新（7天3次）

## 记录流程
1. 确认要素：股票名称或代码、买入/卖出、数量、成交均价、成交时间、操作理由、原计划止损/止盈
2. 记录（utils/trade_journal.py add）
3. 确认记录成功，提示盘后复盘会用到

## ⚠ ETF单位铁律（2026-08-01 踩坑）
- ETF单位是「份」，不是「手」！1手=100份，按"手"记录金额会差100倍
- ETF代码特征：5开头（沪）、15/16开头（深）、56/58开头（科创）
- 记录ETF必须用 --unit 份：python3 utils/trade_journal.py add --code 588060 --name 科创50ETF --side buy --shares 800 --unit 份 --price 1.040
- 股票默认 --unit 手（1手=100股）
- 拿不准时直接问用户："您买的是多少份？还是多少手？"

## 支持语义
- 「记录交易：588060 买入 800份 @1.04 理由=回踩黄线 止损1.02」→ 直接落库
- 「我昨天卖了半导体设备ETF」→ 追问数量和价格后记录
- 「今天没操作」→ 不记录，盘后复盘会显示"今日无操作"

## 关键命令
- python3 utils/trade_journal.py add --code X --name 名称 --side buy|sell --shares N --unit 手|份 --price P --reason "理由" --stop S --target T
- python3 utils/trade_journal.py today     # 今日记录
- python3 utils/trade_journal.py list      # 全部记录
- python3 utils/trade_journal.py observe "追高买入"   # 行为观察（单次不改画像）
- python3 utils/trade_journal.py stats     # 错误模式统计

## 行为观察规则
- 单日观察只记 trade_observations.jsonl，不直接改 trader_profile.md
- 画像更新条件：同一行为近7天≥3次 / 用户明确确认 / 多笔统计稳定倾向
- 典型观察类别：提前入场/追高/仓位过大/止损迟疑/计划外交易/过度做T/过早卖出/报复性交易

## 数据规范
- 代码必须用户确认（ETF同名不同基金公司，代码完全不同，禁止猜）
- 成本/止损以用户提供为准；止损优先人工输入，禁止自动生成
