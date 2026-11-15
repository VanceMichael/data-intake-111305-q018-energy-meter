# 能源抄表异常更正的账期锁定采集

这是资料接收程序。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。原始归档只追加、不覆盖；在此之上提供读数结构化台账，把表计身份、实测/估算质量、倍率生效区间与读数修订连起来。台账不生成费用、不自动执行门户提交，也不重写已签发账：月账签发后的供方更正只形成有依据、可去重、可溯源的调整建议，供财务查询与决定。

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

演示从空的临时目录启动本地来源，真实完成多次 HTTP 获取及归档，并跑完结构化接入、签发、乱序更正、调整建议接受与再更正、财务并列查询与溯源的完整生命周期；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m energy_meter serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m energy_meter capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释与原始接收独立演进。

## 结构化接入与财务查询

`rules.py` 版本化管理表计安装区间与倍率生效区间；`ingest.py` 把供方记录规范化为结构化读数（未知倍率保持空值，绝不默认成一）；`ledger.py` 在独立台账中维护只追加的读数消息、按最大修订号收敛的当前解释、不可变的已签发快照以及幂等成链的调整建议；`finance.py` 提供只读并列视图与报文/规则双向溯源；`cli.py` 暴露 `publish-rules`、`ingest`、`issue-snapshot`、`adjust`、`finance` 子命令。模型细节见 `docs/ledger-model.md`。

关键约束：

- 跨月换表与设备编号复用按内部安装身份区分累计曲线，旧表尾数和新表首数不相接；
- 补传与更正可乱序，当前解释按修订号更新，已签发快照及当时依据由数据库触发器固化；
- 调整建议按“快照 + 读数 + 解释摘要”去重，重复投递不产生第二笔；财务接受后的新更正通过 `prior_suggestion_id` 与差额字段保持链式关系；
- 财务侧只查询接入状态：已签发读数、当前解释、待核影响并列，并能从每个差异回到供方报文和适用规则，不另建计费系统。

## 开发资料

来源协议见 `docs/source-protocol.md`，台账模型见 `docs/ledger-model.md`，领域观察关系见 `tests/test_source_contract.py`，业务规则的可执行口径见 `tests/test_ledger.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径；`fixtures/ledger-rules.json` 是演示用规则版本，`fixtures/staged-feed.json` 是乱序补传与更正的场景资料。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `energy_meter` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m energy_meter demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
