# data-trace-agent (POC)

A LangGraph `create_react_agent` that answers data-lineage / data-support /
ETL-bug questions against a tiny simulated warehouse, by combining two MCP
servers:

- [`sqllite-mcp-server`](./mcp/filesystem_mcp_server.py) — SQL access to the warehouse
- [`filesystem-mcp-server`](./mcp/sqllite_mcp_server.py) — read raw upstream
  files on disk

You need both to find ETL bugs: querying the DB tells you "what's there"; reading
the source file tells you "what was supposed to be there". The discrepancy is
the bug.

## Scenario

Many upstream feeds (S3, server logs, several customer integrations) ship daily
files; ETL loaders import them into raw tables; an agg job rolls everything up
into `daily_metrics`. When a downstream metric moves or looks suspicious, the
on-call engineer asks the agent.

We plant **two real ETL bugs for today (2026-04-26)** so the agent has
something to find:

1. **Customer B currency-filter bug** —
   `data/sources/customer_b/2026-04-26.csv` has 80 orders (75 EUR + 5 USD).
   The loader silently filters out non-USD rows, so the DB only has 5.
   ⇒ `daily_metrics.total_revenue` drops ~60% today.
   ⇒ Visible only by **reading the source file** and comparing row counts.

2. **Customer A precision bug** —
   `data/sources/customer_a/2026-04-26.csv` stores amounts as `119.06`,
   `94.21`, etc. The loader does `int(amount)` instead of `float(amount)`,
   silently truncating cents. DB has `119.0`, `94.0`.
   ⇒ Subtle "file says 119.06, DB says 119" — *the* canonical question this
     POC is built to answer.

The other two sources (S3 clickstream, app logs) are clean — file row count
matches DB row count. Useful as a control: the agent should **not** flag them.

## Files

| File                         | Purpose                                                        |
| ---------------------------- | -------------------------------------------------------------- |
| `setup_warehouse.py`         | Build `data/warehouse.db` + write upstream files for today.    |
| `trace_agent.py`             | LangGraph `create_react_agent` driver + REPL (2 MCP servers).  |
| `tests/test_lineage_qa.py`   | End-to-end pytest suite hitting the real LLM.                  |

After `python3 setup_warehouse.py`:

```
data/
├── warehouse.db
└── sources/
    ├── s3_clickstream/2026-04-26.json   # 1000 NDJSON events  (clean)
    ├── app_logs/2026-04-26.log          # 500 log lines       (clean)
    ├── customer_a/2026-04-26.csv        # 51 rows, 99.99-style amounts  (precision bug in loader)
    └── customer_b/2026-04-26.csv        # 80 rows, mixed USD/EUR        (currency-filter bug in loader)
```

## Quickstart

```bash
# 1) Deps
pip install langgraph langchain-openai langchain-mcp-adapters "mcp[cli]" faker pytest pytest-asyncio

# 2) Build the warehouse + write today's upstream files
python3 setup_warehouse.py

# 3) Configure an LLM endpoint (OpenRouter or OpenAI compatible)
export OPENROUTER_API_KEY=...
# optional:
# export LLM_BASE_URL=https://openrouter.ai/api/v1
# export LLM_MODEL=anthropic/claude-sonnet-4.5

# 4) REPL
python3 trace_agent.py

# 5) Run the e2e tests (slow, ~2 min, hits real LLM)
python3 -m pytest -s
```

## Sample questions

- *Today's total_revenue is much lower than last month — by how much, and what's
  the root cause? Check the upstream raw source file too.*
- *For today's customer_a_orders_raw rows, do the DB amounts match the upstream
  source file exactly? If they differ, name the loader and explain the bug.*
- *Where does daily_metrics.total_events come from upstream?*

## How the agent finds bugs

```
user question
    │
    ▼
react agent  ──► sqlite-mcp::execute_query   _field_lineage / _source_registry / raw tables
    │
    ├──────────► filesystem-mcp::read_file    data/sources/<source>/<date>.<ext>
    │
    ▼
compare DB rows to file rows → name the loader → answer
```

Lineage metadata lives in two SQL tables inside `warehouse.db`:

- `_field_lineage(target_table, target_field, source_table, source_field, transform, etl_job)`
- `_source_registry(source_table, source_uri, file_dir, loader, schema_note)`

The agent reads `_source_registry` to learn where each raw source's files
live on disk, then uses the filesystem MCP to read them.
