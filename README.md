# 家庭训练储蓄编排器

面向训练目标、家庭现金流和自动储蓄安排的 Python 后端服务。

把训练周期、装备目标、比赛报名、家庭固定支出与储蓄账户的自动转入排在
同一时间轴上；当收入减少、比赛延期或临时医疗支出发生时，系统按优先级
重新分配未来扣款——已执行的转账永不被回算覆盖，月末跨时区入账按银行
价值日处理。每次调整都记录触发原因、受影响的目标和新的预计完成日，
银行流水重复导入保持幂等，计划可版本化导出供理财顾问复核。

仅使用 Python 标准库，需要 Python 3.11+。

## 运行

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口，`GET /health` 确认进程状态。持久化写入
`.runtime/planner.db`（SQLite，可用环境变量 `PLANNER_DB` 覆盖）。
执行测试：

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。

## 核心概念

- **目标（goal）**：`race_fee` 报名、`equipment` 装备、`travel` 旅行、
  `emergency_reserve` 应急储备。目标有截止日与优先级（数字越小越优先，
  默认应急储备=1、报名=2、装备=3、旅行=4）。
- **扣款（deduction）**：为达成目标而排定的自动转入，状态机为
  `pending → paused / executed / cancelled`。已执行的扣款是历史事实，
  重排只改写 `pending`/`paused`。
- **价值日（value date）**：扣款按银行时区结算日记账，周末/节假日顺延。
  账期归属、容量分配与预计完成日一律以价值日为准；未用完的月度额度
  结转后续账期，顺延到次月的扣款由上月结余自然承接。
- **事件（event）**：`income_change` / `race_postponed` / `emergency` /
  `goal_change` / `note`。每个事件先应用载荷（改收入、改截止日、补充
  应急储备……），再触发一次重排并落库一个新的计划版本。
- **计划版本（plan version）**：每次重排的不可变快照，记录触发原因、
  受影响目标、扣款变更（新建/更新/暂停/恢复/取消）与新的预计完成日。

## API 一览

所有接口返回 JSON；金额可传数字或字符串，内部按 6 位小数精度存储。

### 家庭画像与固定支出

```bash
curl -X PUT localhost:8000/household/profile -d '{
  "monthly_income": "30000", "currency": "CNY",
  "local_tz": "Asia/Shanghai", "bank_tz": "+12:00", "transfer_time": "21:00"}'
curl -X POST localhost:8000/household/fixed-expenses -d '{"name": "信用卡分期", "amount": "3000", "day_of_month": 15}'
curl    localhost:8000/household/fixed-expenses
curl -X DELETE localhost:8000/household/fixed-expenses/1
```

### 目标与资金来源

```bash
curl -X POST localhost:8000/goals -d '{
  "goal_type": "emergency_reserve", "name": "应急储备",
  "target_amount": "60000", "deadline": "2027-03-31"}'
curl localhost:8000/goals
curl localhost:8000/goals/1
curl localhost:8000/goals/1/funding   # 资金来源 + 被暂停扣款 + 恢复条件
```

### 事件（触发重排）

```bash
curl -X POST localhost:8000/events -d '{
  "event_type": "income_change",
  "payload": {"new_monthly_income": "24000"},
  "occurred_on": "2026-10-05", "reason": "收入结构调整"}'
curl -X POST localhost:8000/events -d '{
  "event_type": "race_postponed",
  "payload": {"goal_id": 2, "new_deadline": "2027-03-31"}}'
curl -X POST localhost:8000/events -d '{
  "event_type": "emergency",
  "payload": {"amount": "8000", "description": "门诊医疗"}}'
curl localhost:8000/events
```

### 扣款

```bash
curl "localhost:8000/deductions?status=paused&goal_id=3"
curl -X POST localhost:8000/deductions/11/resume            # 手动恢复（下次重排仍可能再暂停）
curl -X POST localhost:8000/deductions/11/execute -d '{"amount": "1000"}'  # 线下执行登记
```

### 银行流水（幂等导入）

```bash
curl -X POST localhost:8000/bank/import -d '{
  "account": "main",
  "transactions": [{"external_id": "tx-001", "amount": "8571.428571",
                    "posted_at": "2026-09-30T13:00:00+00:00", "deduction_id": 1}]}'
```

同一文件重复导入返回 `duplicates` 计数，不会重复入账；带 `deduction_id`
的流水会把对应扣款标记为已执行，带 `goal_id` 的流水计为直接注资。

### 计划版本与导出

```bash
curl localhost:8000/plans/versions
curl localhost:8000/plans/versions/3
curl localhost:8000/plans/versions/3/export   # 附校验和，供理财顾问复核
curl -X POST localhost:8000/replan -d '{"reason": "手动重排"}'
```

### 时间轴与训练周期

```bash
curl -X POST localhost:8000/training-blocks -d '{"name": "基础期", "start_date": "2026-09-01", "end_date": "2026-11-30", "goal_ids": [2]}'
curl "localhost:8000/timeline?from=2026-09-01&to=2027-04-30"
```

## 设计说明

详见 `docs/domain.md`；公开词汇表（目标类型、扣款状态、触发类型、
日历精度等）见 `reference/domain.json`。
