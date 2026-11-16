# 能源抄表异常更正的账期锁定采集

这是资料接收程序。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。重复观察会分别保留，原始内容按摘要复用。在原始归档之上另有**读数结构化接入层**：把表计身份、实测/估算质量、倍率生效区间与读数修订组织成只追加事实，冻结月度签发快照并维护调整建议链，供财务只读查询。该层不回改历史账单，也不实现计费。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q energy_meter tests
python3 -m energy_meter demo
```

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取及归档，并输出原样记录；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m energy_meter serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m energy_meter capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释与原始接收独立演进。

## 读数结构化接入与财务查询

原始归档之后，结构化层把报文中的供方记录接入独立的读数库（默认不随采集自动执行）：

```sh
python3 -m energy_meter ingest --archive .runtime/raw.sqlite3 --store .runtime/readings.sqlite3
python3 -m energy_meter issue --store .runtime/readings.sqlite3 --reading R10 --provider grid-a
python3 -m energy_meter finance status --store .runtime/readings.sqlite3
python3 -m energy_meter finance difference --store .runtime/readings.sqlite3 --snapshot-id 1
```

设计与不变量见 `docs/structured-ingestion.md`：表计序列按（供方、设备编号、读数标识）界定，换表与编号复用不拼接累计曲线；未知倍率保持待核、绝不默认成一；补传更正乱序到达而事实只追加，当前解释取最高修订号；已签发快照及当时依据冻结，重复投递不产生第二笔调整建议；调整被接受后的再次更正通过 `prior_suggestion_id` 链接，并同时给出总差额与相对前次调整后口径的新增差额。财务可并列查看已签发读数、当前解释与待核影响，并从每个差异回到供方报文摘要、适用倍率区间与版本化规则。

## 开发资料

来源协议见 `docs/source-protocol.md`，结构化接入设计见 `docs/structured-ingestion.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径，对应回归断言在 `tests/test_structured_ingestion.py` 与 `tests/test_business_cases.py`。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `energy_meter` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m energy_meter demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
