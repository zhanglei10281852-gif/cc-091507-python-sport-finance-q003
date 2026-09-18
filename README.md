# 家庭训练储蓄编排器

把铁人三项训练周期、装备/报名/差旅目标、比赛旅行、家庭固定支出与储蓄账户自动转账编排到同一时间轴的 Python 后端服务。
收入减少、比赛延期、临时医疗支出等事件按优先级重排**未来**扣款；已执行转账在 append-only 账本中永不回算。

## 核心规则

- **月度现金流瀑布**：每月每个账户 `收入 − 固定支出 − 临时支出` 后，按目标优先级
  （默认 应急储备 10 < 报名费 20 < 差旅 30 < 装备 40，可在 config 覆盖）依次补给；
  高优先级未满足时，低优先级目标当月扣款转入 `paused` 并记录结构化恢复条件。
- **已执行不可变**：银行流水/手工登记写入 `ledger`（append-only）；重排只作用于
  `scheduled`/`paused` 的未来实例，`executed`/`cancelled` 永不被修改。
- **银行价值日**：业务时间按来源时区解释，超过当日截止时间（默认 17:00）顺延至下一工作日
  （周末/登记假期顺延），按价值日所在年月归入月度瀑布——月末深夜跨时区交易会正确进入次月。
- **幂等导入**：`账户 + external_id` 去重；无 external_id 时用行内容哈希。重复导入整批跳过、不报错、不产生新版本。
- **审计与版本**：每次实际重排生成 adjustment（触发类型/原因/受影响目标/新旧 ETA），
  `plan_version` 递增并导出不可变快照 `.runtime/exports/plan_vN.json`（含 SHA-256）。
- 金额内部为整数最小货币单位（6 位小数精度，见 `reference/domain.json`），杜绝浮点误差。
- 瀑布假设**同一资金账户内目标币种一致**；跨币种账户请分别配置。

## 运行

需要 Python 3.11+（仅标准库）：

```bash
python3 src/index.py          # 默认 0.0.0.0:8000，RUNTIME_DIR 可改持久化目录
python3 -m unittest discover -s tests
docker compose up --build
```

## API 一览（均为 JSON）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` `/reference` | 健康检查、领域枚举 |
| PUT | `/config` | 计划区间、`as_of_date`、转账日、截止时间、假期、优先级覆盖 |
| POST/GET | `/goals` | 目标（race_fee/equipment/travel/emergency_reserve） |
| GET | `/goals/{id}/funding` | **资金来源**（已入账+计划中+按账户汇总）、暂停扣款与恢复条件 |
| PUT | `/income?replan=true&reason=` | 登记/调整月收入（同 月+账户 覆盖），可直接触发重排 |
| POST | `/fixed-expenses` | 房租、保险、信用卡分期等 |
| POST | `/one-off-expenses` | 临时支出（如医疗）；按价值日自动归入月份并重排 |
| POST | `/training-phases` `/races` | 训练周期与比赛 |
| POST | `/races/{id}/postpone` | **比赛延期**：顺延关联目标截止日并重排 |
| POST | `/bank-imports` | **幂等**银行流水导入（`entries[]`，含 external_id/business_time/tz） |
| POST | `/transfers` | 手工登记已执行转账 |
| GET | `/deductions?status=paused` | 扣款实例（scheduled/executed/paused/cancelled） |
| POST | `/deductions/{id}/pause` `/resume` `/pin` | 手动暂停（只按条件恢复）/恢复并锁定/锁定保护 |
| POST | `/replan` | 手动触发重排 |
| GET | `/timeline?from=&to=` | **统一时间轴**：训练/比赛/目标截止/收入/支出/扣款/入账 |
| GET | `/adjustments` `/adjustments/{id}` | 重排审计（原因、受影响目标、ETA 变化） |
| GET | `/exports` `/exports/{version}` | 版本化计划列表 / 某版不可变快照（供理财顾问复核） |

### 典型流程

```bash
curl -XPUT localhost:8000/config -d '{"plan_start_month":"2026-09","plan_end_month":"2027-06","as_of_date":"2026-09-18"}'
curl -XPOST localhost:8000/races -d '{"name":"香港半铁","race_date":"2027-03-20","tz":"Asia/Hong_Kong"}'
curl -XPOST localhost:8000/goals -d '{"name":"应急金","type":"emergency_reserve","target_amount":"30000","deadline":"2027-02-28","account":"checking"}'
curl -XPUT  'localhost:8000/income?replan=true&reason=基线' -d '{"month":"2026-09","account":"checking","amount":"20000"}'
curl -XPOST localhost:8000/bank-imports -d '{"entries":[{"account":"checking","goal_id":"goal_xxx","amount":"300","business_time":"2026-10-05T10:00:00+08:00","external_id":"TX-1"}]}'
```

业务数据写入 `.runtime/`（已在 .gitignore）。接口扩展保持 JSON 响应。
