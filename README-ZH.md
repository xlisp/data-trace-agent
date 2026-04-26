# data-trace-agent (POC)

一个基于 LangGraph `create_react_agent` 的智能体，通过组合两个 MCP 服务器，针对一个小型的模拟数据仓库回答数据血缘 / 数据支持 / ETL bug 相关的问题：

- [`sqllite-mcp-server`](../sqllite-mcp-server) — 提供对数据仓库的 SQL 访问能力
- [`filesystem-mcp-server`](../filesystem-mcp-server-github) — 读取磁盘上的原始上游文件

要找出 ETL bug，两个工具缺一不可：查询数据库告诉你"现在有什么"；读取源文件告诉你"本来应该有什么"。两者之间的差异，就是 bug。

## 场景

许多上游数据源（S3、服务器日志、若干客户对接系统）每天都会上传文件；ETL 加载器把它们导入原始表；一个聚合任务再把所有数据汇总到 `daily_metrics` 中。当下游某个指标发生波动或看起来异常时，值班工程师就会向智能体提问。

我们故意在**今天（2026-04-26）埋了两个真实的 ETL bug**，让智能体有东西可查：

1. **客户 B 的币种过滤 bug** —
   `data/sources/customer_b/2026-04-26.csv` 中有 80 笔订单（75 笔 EUR + 5 笔 USD）。
   加载器静默地过滤掉了非 USD 的行，所以数据库里只有 5 条。
   ⇒ 今天的 `daily_metrics.total_revenue` 下跌了约 60%。
   ⇒ 只有通过**读取源文件**并对比行数才能发现。

2. **客户 A 的精度 bug** —
   `data/sources/customer_a/2026-04-26.csv` 中金额存储为 `119.06`、`94.21` 等。
   加载器使用了 `int(amount)` 而不是 `float(amount)`，静默地截断了小数部分。数据库里变成了 `119.0`、`94.0`。
   ⇒ 微妙的"文件里写 119.06，数据库里却是 119"——*这正是* 本 POC 要回答的核心问题。

另外两个数据源（S3 点击流、应用日志）是干净的——文件行数与数据库行数完全匹配。它们作为对照组，智能体**不应该**误报。

## 文件说明

| 文件                         | 用途                                                              |
| ---------------------------- | ----------------------------------------------------------------- |
| `setup_warehouse.py`         | 构建 `data/warehouse.db` 并写入今天的上游文件。                    |
| `trace_agent.py`             | LangGraph `create_react_agent` 驱动 + REPL（连接 2 个 MCP 服务器）。 |
| `tests/test_lineage_qa.py`   | 端到端 pytest 测试套件，会真实调用 LLM。                            |

执行 `python3 setup_warehouse.py` 后：

```
data/
├── warehouse.db
└── sources/
    ├── s3_clickstream/2026-04-26.json   # 1000 条 NDJSON 事件   (干净)
    ├── app_logs/2026-04-26.log          # 500 行日志            (干净)
    ├── customer_a/2026-04-26.csv        # 51 行, 99.99 类型的金额   (加载器有精度 bug)
    └── customer_b/2026-04-26.csv        # 80 行, 混合的 USD/EUR     (加载器有币种过滤 bug)
```

## 快速开始

```bash
# 1) 安装依赖
pip install langgraph langchain-openai langchain-mcp-adapters "mcp[cli]" faker pytest pytest-asyncio

# 2) 构建数据仓库 + 写入今天的上游文件
python3 setup_warehouse.py

# 3) 配置一个 LLM 端点（OpenRouter 或 OpenAI 兼容接口）
export OPENROUTER_API_KEY=...
# 可选:
# export LLM_BASE_URL=https://openrouter.ai/api/v1
# export LLM_MODEL=anthropic/claude-sonnet-4.5

# 4) 进入 REPL 交互
python3 trace_agent.py

# 5) 运行端到端测试（较慢，约 2 分钟，会真实调用 LLM）
python3 -m pytest -s
```

## 示例问题

- *今天的 total_revenue 比上个月低很多——具体低了多少，根本原因是什么？也请检查一下上游原始源文件。*
- *对于今天 customer_a_orders_raw 中的行，数据库里的金额和上游源文件里的完全一致吗？如果不一致，请指出是哪个加载器的问题，并解释这个 bug。*
- *daily_metrics.total_events 在上游来源是哪里？*

## 智能体如何定位 bug

```
用户问题
    │
    ▼
react agent  ──► sqlite-mcp::execute_query   _field_lineage / _source_registry / 原始表
    │
    ├──────────► filesystem-mcp::read_file    data/sources/<source>/<date>.<ext>
    │
    ▼
对比数据库行与文件行 → 指出加载器 → 给出答案
```

血缘元数据保存在 `warehouse.db` 中的两张 SQL 表里：

- `_field_lineage(target_table, target_field, source_table, source_field, transform, etl_job)`
- `_source_registry(source_table, source_uri, file_dir, loader, schema_note)`

智能体先读 `_source_registry` 来了解每个原始数据源的文件存在磁盘的什么位置，然后通过 filesystem MCP 把文件读出来。
