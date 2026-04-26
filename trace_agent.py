"""
data-trace-agent — POC

A LangGraph ReAct agent that answers data-lineage / data-support questions
against a tiny simulated warehouse.

It connects to the existing `sqllite-mcp-server` over stdio to get its tools
(execute_query, describe_table, add_field_lineage, trace_field_lineage, ...),
seeds the in-memory lineage tracker on startup with the warehouse's known
field lineage, then runs a REPL.

Run:
    python3 setup_warehouse.py        # one-time: build data/warehouse.db
    export OPENROUTER_API_KEY=...     # or OPENAI_API_KEY
    python3 trace_agent.py
"""
from __future__ import annotations

import asyncio
import os
import sys

from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from setup_warehouse import DB_PATH, LINEAGE

_HERE = os.path.dirname(os.path.abspath(__file__))
SQLITE_MCP_DIR = "/Users/xlisp/PyPro/sqllite-mcp-server"
SQLITE_MCP_MAIN = os.path.join(SQLITE_MCP_DIR, "main.py")


SYSTEM_PROMPT = f"""\
You are a Data Lineage / Data Support agent.

You help engineers answer two kinds of questions about a data warehouse:

  1. **Anomaly explanation** — "today's value of field X dropped vs last month, why?"
     Approach: pull the recent series for the aggregated field, identify the
     anomalous date, then trace the field's lineage to its upstream sources and
     query each source for the same date to localise the change.

  2. **Lineage / provenance** — "where does field X come from upstream?"
     Approach: call `trace_field_lineage` first; if no entry, infer from
     describe_table + execute_query on the relevant ETL/source tables.

# The warehouse you operate on

The SQLite database is at: `{DB_PATH}`

You MUST pass that exact path as the `db_path` argument to every SQL tool
(`execute_query`, `describe_table`, etc.).

Tables:
  - `s3_clickstream_raw`       (events from `s3://events/clickstream`)
  - `app_logs_raw`             (events from `fluentd://app-logs`)
  - `customer_a_orders_raw`    (orders feed from `sftp://customer-a/orders`)
  - `customer_b_orders_raw`    (orders feed from `api://customer-b/orders`)
  - `daily_metrics`            (downstream aggregate: report_date, total_events,
                                active_users, total_orders, total_revenue)

Today's date is 2026-04-26. "Last month" means the prior 30 days for this POC.

# How to work

- Be concrete. Run SQL. Cite numbers and the row(s) you got them from.
- When you suspect a drop in an aggregate, COMPARE today vs the average / median
  of the prior window, then drill into each upstream source for the same day.
- When you've identified the upstream cause, name the source path
  (e.g. `api://customer-b/orders`) so the user knows who to page.
- Keep replies short. Numbers > prose.
"""


def _make_llm() -> ChatOpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENROUTER_API_KEY or OPENAI_API_KEY before running.")
    base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    model_name = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4.5")
    return ChatOpenAI(model=model_name, base_url=base_url, api_key=api_key, temperature=0)


async def _seed_lineage(tools) -> None:
    """Populate the MCP server's in-memory FieldTracker with our known lineage."""
    by_name = {t.name: t for t in tools}
    add = by_name.get("add_field_lineage")
    if add is None:
        print("[warn] add_field_lineage tool not exposed by MCP server; skipping seed.", file=sys.stderr)
        return
    for tgt_t, tgt_f, src_t, src_f, note in LINEAGE:
        await add.ainvoke({
            "target_table": tgt_t,
            "target_field": tgt_f,
            "source_tables": src_t,
            "source_fields": src_f,
            "join_condition": note,
        })
    print(f"[ok] seeded {len(LINEAGE)} lineage entries.", file=sys.stderr)


async def _build_agent():
    client = MultiServerMCPClient({
        "sqlite-db": {
            "command": sys.executable,
            "args": [SQLITE_MCP_MAIN],
            "transport": "stdio",
            "cwd": SQLITE_MCP_DIR,
        },
    })
    tools = await client.get_tools()
    print(f"[ok] loaded {len(tools)} MCP tools: {[t.name for t in tools]}", file=sys.stderr)
    await _seed_lineage(tools)
    agent = create_react_agent(_make_llm(), tools, prompt=SYSTEM_PROMPT)
    return client, agent


async def _ask(agent, history: list, user_text: str) -> str:
    history.append(HumanMessage(content=user_text))
    result = await agent.ainvoke({"messages": history})
    msgs = result["messages"]
    history.clear()
    history.extend(msgs)
    for m in reversed(msgs):
        if isinstance(m, AIMessage) and m.content:
            return m.content
    return "(no reply)"


SAMPLE_QUESTIONS = [
    "Today's total_revenue in daily_metrics looks low compared to last month. By how much, and why?",
    "Where does daily_metrics.total_events come from upstream? Show me the lineage.",
    "Compared with the prior 30 days, which fields in daily_metrics moved most today, and which upstream source is responsible for each?",
]


async def main() -> None:
    client, agent = await _build_agent()
    print("\n=== data-trace-agent ready ===")
    print("Sample questions you can try:")
    for q in SAMPLE_QUESTIONS:
        print(f"  - {q}")
    print("Type your question (blank line / Ctrl-D to quit).\n")

    history: list = []
    try:
        while True:
            try:
                q = input("you> ").strip()
            except EOFError:
                break
            if not q:
                break
            try:
                ans = await _ask(agent, history, q)
            except Exception as e:
                print(f"[error] {e}", file=sys.stderr)
                continue
            print(f"\nagent> {ans}\n")
    finally:
        # MultiServerMCPClient currently doesn't expose an explicit close;
        # processes are cleaned up when the parent exits.
        pass


if __name__ == "__main__":
    asyncio.run(main())
