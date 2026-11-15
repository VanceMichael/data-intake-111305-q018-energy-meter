# 读数结构化台账模型

原始归档（`archive.py`）只追加供方报文，不解释业务。结构化层在其之上新增
独立台账（默认 `ledger.sqlite3`），把表计身份、读数质量、倍率生效区间与
读数修订连起来，并向财务提供只读查询。它不是计费系统：不产生费用、不
执行门户提交、不重写任何已签发数据。

## 分层与依据链

```
供方报文 HTTP 响应
  └─ archive.captures / bodies（原始字节、状态、头部、摘要，只追加）
       └─ reading_messages（结构化读数，回链 capture_id、record_index、payload_digest）
            └─ interpretation_versions（每次解释，不可变；引用规则版本）
                 └─ interpretations（每个读数的当前解释，可被新修订取代）
                      ├─ snapshots / snapshot_readings（签发时刻冻结，触发器禁改禁删）
                      └─ adjustment_suggestions（调整建议，幂等且成链）
```

每个差异都能沿链回到两类依据：

* **供方报文依据**：`reading_messages.capture_id` 指向原始响应，`payload_digest`
  校验报文内容，`record_index` 定位在报文中的位置；
* **适用规则依据**：解释与快照保存 `rule_basis_id`（如 `default@2`），
  `rule_versions` 保存规则全文与摘要，规则换版不影响历史解释的依据。

## 表计身份与累计曲线

供方声明的 `meter` 只是来源编号，不等于业务实体。规则中的 `meters` 区间把
“供方 + 编号 + 时刻”解析为内部安装身份 `install_id`：

* 区间半开（`valid_from <= 时刻 < valid_to`），同一编号的重叠区间在发布时拒绝；
* 跨月换表：旧表尾数与新表首数分属两个 `install_id`，
  `finance series` 给出 `boundaries`，两侧分别累计；
* 设备编号被复用：复用前后解析为不同 `install_id`，
  `reused_label=true` 且每条序列 `join_allowed_with=[]`，不可能接成同一条
  累计曲线；
* 规则中查不到安装身份时 `identity_status=identity_pending`，不做猜测。

## 质量与倍率

* 质量只接受 `measured`（实测）与 `estimated`（估算），随修订保留；
* 倍率由规则的 `multipliers` 生效区间确认，并与供方申报值核对：
  - 无确认区间，或申报值与规则区间不一致 → `multiplier_status=pending`、
    `factor=NULL`、`gross_value=NULL`，原因写入 `multiplier_reason`；
  - 代码中不存在“未知倍率按 1 处理”的分支；
* 待核读数进入 `finance pending` 清单，金额影响标记为受阻，
  规则补录后用 `reinterpret_all` 按新版本重算，已签发快照仍引用旧版本。

## 修订、乱序与当前解释

* 报文只追加：`(provider, meter, reading, revision)` 唯一。重复投递内容相同
  计为 `duplicates` 且不产生新版本；同修订号内容不同抛 `ReadingConflict`，
  整批不落库；
* 当前解释与到达顺序无关，始终取最大修订号；迟到的旧修订不会回退当前值；
* 解释摘要变化才追加 `interpretation_versions`，相同解释重放复用同一版本；
  `interpretations.superseded_by` 记录被哪个版本取代。

## 签发快照与调整建议

* `issue-snapshot` 复制当时的解释副本，并对快照内容计算摘要；同一账期同一
  状态重复签发返回同一快照；
* `snapshots`、`snapshot_readings` 由 SQLite 触发器禁止 UPDATE/DELETE，
  历史账在数据库层面不可改；
* 调整建议的唯一键是 `(snapshot_id, reading_key, interpretation_digest)`，
  因此重复投递、重复生成都不会产生第二笔；
* 财务 `accept` 后建议状态冻结，不可二次决定；此后新更正生成的建议带
  `prior_suggestion_id`，并同时给出：
  - `register_delta / gross_delta`：对**已签发口径**的差额；
  - `delta_vs_prior_*`：对**前次调整后口径**的新增差额，
    便于看清前次调整与本次差额的关系。

## 财务查询（只读）

| 命令 | 用途 |
| --- | --- |
| `finance comparison --snapshot-id` | 并列已签发读数、当前解释与待核影响 |
| `finance pending [--period]` | 身份/倍率待核、金额影响受阻清单 |
| `finance series --provider --meter` | 按安装身份的累计曲线与换表边界 |
| `finance trace --reading-key` 或 `--suggestion-id` | 回到每条供方报文、解释版本、规则全文与调整链 |

## 命令一览

```sh
python3 -m energy_meter publish-rules --ledger 台账 --rules 规则.json
python3 -m energy_meter ingest --ledger 台账 --archive 原始归档 \
        --base-url 来源 --source 来源名 --path /feed
python3 -m energy_meter issue-snapshot --ledger 台账 --period 2026-10
python3 -m energy_meter adjust --ledger 台账 --snapshot-id 1
python3 -m energy_meter adjust --ledger 台账 --accept 1 --note "财务接受"
python3 -m energy_meter finance comparison --ledger 台账 --snapshot-id 1
python3 -m energy_meter finance trace --ledger 台账 --reading-key R10 --archive 原始归档
```

`python3 -m energy_meter demo` 在临时目录跑完整生命周期：估算读数签发 →
实测修订乱序补传 → 调整建议去重与接受 → 换表新表倍率待核 → 接受后再次
更正形成链式差额 → 财务并列视图与溯源。
