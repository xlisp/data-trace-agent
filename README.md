# data-trace-agent (POC)

A LangGraph `create_react_agent` that answers data-lineage / data-support
questions against a tiny simulated warehouse. It uses the existing
[`sqllite-mcp-server`](../sqllite-mcp-server) for SQL access via MCP/stdio.

## Scenario

Many upstream feeds (S3, server logs, several customer integrations) are
ingested into raw tables; an ETL job aggregates them into a single
`daily_metrics` table. When a downstream metric moves, an on-call engineer
needs to ask:

- "Today's `total_revenue` is low compared to last month — by how much, and why?"
- "Where does `daily_metrics.total_events` come from upstream?"

This POC wires up:

1. A simulated warehouse (`data/warehouse.db`) with 4 raw source tables, one
   aggregate (`daily_metrics`), and a `_field_lineage` metadata table.
2. A LangGraph ReAct agent that talks to the SQLite MCP server via stdio and
   answers those questions by running SQL and reading lineage rows.

## Files

| File                  | Purpose                                                        |
| --------------------- | -------------------------------------------------------------- |
| `setup_warehouse.py`  | One-shot: build `data/warehouse.db` with raw + agg + lineage.  |
| `trace_agent.py`      | LangGraph `create_react_agent` driver + REPL.                  |

## What's in the simulated data

- 30 days of "normal" history.
- **Today (2026-04-26)** is deliberately broken so the agent has something to find:
  - `customer_b_orders_raw` feed shipped only 5 rows (broken `api://customer-b/orders`).
  - `app_logs_raw` spam filter mis-applied → log volume ~3.5x normal.

## Quickstart

```bash
# 1) Ensure deps are present (langgraph, langchain-openai, langchain-mcp-adapters,
#    mcp, faker — faker is needed by the MCP server)
pip install langgraph langchain-openai langchain-mcp-adapters "mcp[cli]" faker

# 2) Build the warehouse
python3 setup_warehouse.py

# 3) Configure an LLM endpoint
export OPENROUTER_API_KEY=...           # or OPENAI_API_KEY
# optional overrides:
# export LLM_BASE_URL=https://openrouter.ai/api/v1
# export LLM_MODEL=anthropic/claude-sonnet-4.5

# 4) Chat with the agent
python3 trace_agent.py
```

Sample questions:

- *Today's total_revenue in daily_metrics looks low compared to last month. By how much, and why?*
- *Where does daily_metrics.total_events come from upstream? Show me the lineage.*
- *Compared with the prior 30 days, which fields in daily_metrics moved most today, and which upstream source is responsible for each?*

## Notes

- The MCP server's in-memory `add_field_lineage` / `trace_field_lineage` tools
  don't persist across stdio sessions (langchain-mcp-adapters opens a fresh
  session per tool call), so we keep lineage in a SQL `_field_lineage` table
  that the agent reads with the standard `execute_query` tool.
