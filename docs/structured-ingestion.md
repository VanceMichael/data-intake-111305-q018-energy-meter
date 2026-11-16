# 读数结构化接入与财务查询

本层在原始报文归档（`archive.py`，只追加、不可变）之上工作，**不回改历史账单，也不实现计费**。它把供方报文结构化为表计读数事实，维护当前解释、冻结签发快照与调整建议链，并向财务提供只读查询。

## 分层边界

| 层 | 模块 | 可变性 | 内容 |
| --- | --- | --- | --- |
| 原始归档 | `archive.py` | 只追加 | HTTP 状态、头部、原始字节、报文摘要、接收时间 |
| 结构化接入 | `domain.py` `store.py` `intake.py` | 事实只追加；当前解释可更新；快照与接受决策冻结 | 表计序列、读数事实、倍率区间、签发快照、调整建议 |
| 财务查询 | `store.py` 的 `series_overview` / `difference_detail` | 只读 | 并列视图与差异回溯 |

结构化库与归档库是两个独立的 SQLite 文件。每条 `reading_facts` 都回链归档 `capture_id` 与 `capture_digest`，财务可经摘要在归档库取回原始报文。

## 核心不变量

1. **序列边界**：表计序列由（供方 `provider`、设备编号 `meter`、读数标识 `reading`）共同界定。跨月换表产生新序列，旧表尾数、新表首数各自保留；设备编号被复用时读数标识不同，仍为不同序列，禁止接成同一条累计曲线。
2. **质量保留**：`measured`/`estimated` 随事实保存；当前解释取同序列最高修订号事实，估算被实测更正的质量变化可在差异中查看（`quality_changed`）。
3. **倍率区间与待核**：每条事实携带供方声明的倍率，按观测时间生成 `multiplier_intervals` 生效区间。倍率缺失即为待核（`multiplier_pending=1`），账单口径读数为 `NULL`、影响挂起，**任何路径都不默认倍率为一**。
4. **乱序与只追加**：补传、更正允许乱序。`reading_facts` 按（序列、修订号）去重后只追加，重复投递标记 `duplicated` 而不新增；当前解释始终由最高修订号派生。
5. **签发冻结**：`issue_snapshot` 固化当时的事实修订、读数、质量、倍率、账单口径、报文摘要与规则版本。一个序列只能签发一次；之后的任何投递都不改写快照及其依据。
6. **建议幂等**：每个已签发快照同时最多存在一笔 `proposed` 建议（数据库部分唯一索引保证）。无差异不生成；同一更正重复投递返回既有建议；更新的更正改写同一笔未决建议而不是新增。
7. **调整链**：建议被财务 `accept` 后冻结并登记决策台账。此后再次更正产生的新建议以 `prior_suggestion_id` 链接前次建议，同时给出对冻结快照的总差额（`billable_delta`）与相对前次调整后口径的新增差额（`incremental_delta_vs_prior`）。

规则目录在 `domain.py` 的 `RULE_CATALOG` 中版本化；快照与建议固化当时的规则版本号，规则升级不改变历史依据。

## 命令

```sh
# 归档（原有，不变）
python3 -m energy_meter capture --base-url URL --archive raw.sqlite3 --source local-source

# 把已归档报文结构化接入；--all-captures 重放全部归档，默认只处理最新一次
python3 -m energy_meter ingest --archive raw.sqlite3 --store readings.sqlite3 [--all-captures]

# 月度签发：按当前解释冻结快照
python3 -m energy_meter issue --store readings.sqlite3 --reading R10 --provider grid-a

# 财务接受一笔调整建议
python3 -m energy_meter accept --store readings.sqlite3 --suggestion-id 1 --note "确认十月更正"

# 财务查询：并列视图（已签发 / 当前解释 / 待核影响 / 建议）
python3 -m energy_meter finance status --store readings.sqlite3

# 财务查询：从一笔差异回到报文摘要、适用区间、事实链与规则
python3 -m energy_meter finance difference --store readings.sqlite3 --snapshot-id 1
```

接入时无法规范化的记录进入返回结果的 `rejected`（附原因），不阻断同批其他记录，也不写入事实表。

## 财务可观察口径

`finance status` 对每个序列并列给出：

- `issued`：冻结的签发读数、质量、倍率、账单口径，以及与当前解释的差异；
- `current`：当前解释的修订、读数、质量、账单口径与 `impact_pending`；
- `boundary`：序列首尾读数（换表尾数/首数）与 `reused_meter_code` 标记；
- `suggestions`：该序列全部建议及其状态。

`finance difference --snapshot-id` 进一步给出冻结依据、当前事实原文、适用倍率区间、完整事实修订链、调整建议链与适用规则条目，形成「差异 → 供方报文（摘要 + capture_id）→ 适用规则」的闭环。
