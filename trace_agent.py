"""
data-trace-agent — POC

A LangGraph ReAct agent that answers data-lineage / data-support questions
against a tiny simulated warehouse.

It connects to two MCP servers over stdio:
  - `sqllite-mcp-server`         — execute_query, describe_table, ...
  - `filesystem-mcp-server`      — read_file, list_directory, search_files_ag, ...

The DB tools answer "what's in the warehouse"; the filesystem tools answer
"what was actually in the upstream file the loader saw" — without both, the
agent cannot find ETL bugs that change the value between source and DB
(e.g. file says 99.99, DB says 99).

Lineage metadata lives in `_field_lineage` and `_source_registry` tables
inside the warehouse db.

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

from setup_warehouse import DB_PATH

_HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = "/Users/xlisp/PyPro/data-trace-agent/mcp"
SQLITE_MCP_MAIN = os.path.join(MCP_DIR, "sqllite_mcp_server.py")
FS_MCP_MAIN = os.path.join(MCP_DIR, "filesystem_mcp_server.py")
SOURCES_DIR = os.path.join(_HERE, "data", "sources")


SYSTEM_PROMPT = f"""\
You are a Data Lineage / Data Support agent.

You answer three kinds of questions about a data warehouse:

  1. **Anomaly explanation** — "today's value of field X dropped vs last month, why?"
  2. **Lineage / provenance** — "where does field X come from upstream?"
  3. **ETL discrepancy hunt** — "the DB value for X looks wrong; does it match
     what the upstream file actually contained?"

You MUST be willing to read both the database and the raw source files on disk
to answer (3) — a value that is correct in the upstream file but wrong in the
DB means the loader / ETL has a bug. You cannot find that by querying SQL alone.

# Warehouse SQLite database

Path: `{DB_PATH}`
Always pass this exact path as the `db_path` arg to SQL tools.

Tables:
  - `s3_clickstream_raw`       (events from `s3://events/clickstream`)
  - `app_logs_raw`             (events from `fluentd://app-logs`)
  - `customer_a_orders_raw`    (orders feed from `sftp://customer-a/orders`)
  - `customer_b_orders_raw`    (orders feed from `api://customer-b/orders`)
  - `daily_metrics`            (downstream aggregate; columns: report_date,
                                total_events, active_users, total_orders,
                                total_revenue)

Metadata tables — your authoritative lineage / source map:
  - `_field_lineage(target_table, target_field, source_table, source_field,
                    transform, etl_job)`
      One row per (target_field, source_field) edge. Read this FIRST for any
      lineage question.
  - `_source_registry(source_table, source_uri, file_dir, loader, schema_note)`
      For each raw source table, where its physical file lives on disk and what
      the file format is. Read this when you need to compare DB rows against
      the upstream source file.

# Upstream source files on disk

Root directory: `{SOURCES_DIR}`
Files for a given day are at `<file_dir>/<YYYY-MM-DD>.<ext>` — e.g.
`{SOURCES_DIR}/customer_b/2026-04-26.csv`. Use the filesystem tools
(`read_file`, `list_directory`, `search_files_ag`, `execute_command`) to read
them. CSVs have a header row.

Today's date is 2026-04-26. "Last month" means the prior 30 days.

# How to work

- Numbers > prose. Run SQL. Cite the rows you got numbers from.

- Anomaly playbook: query the recent series, compute today vs prior-30d
  average, look up lineage in `_field_lineage`, then drill into each upstream
  raw table for the same day. If a raw table looks short, *also* look up its
  file in `_source_registry` and read the file with the filesystem tools — the
  file may contain rows the loader silently dropped.

- Discrepancy playbook: when comparing DB and source, pick a small set of
  primary keys, read the source file, parse the matching rows, and contrast
  the values column-by-column. Name the loader (from `_source_registry`) when
  you accuse one of being buggy.

- When you identify the offending feed or loader, name BOTH the source URI
  (e.g. `api://customer-b/orders`) and the loader (e.g. `load_customer_b_orders`).
"""


def _make_llm() -> ChatOpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENROUTER_API_KEY or OPENAI_API_KEY before running.")
    base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    model_name = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4.5")
    return ChatOpenAI(model=model_name, base_url=base_url, api_key=api_key, temperature=0)


async def build_agent():
    client = MultiServerMCPClient({
        "sqlite-db": {
            "command": sys.executable,
            "args": [SQLITE_MCP_MAIN],
            "transport": "stdio",
            "cwd": SQLITE_MCP_DIR,
        },
        "filesystem": {
            "command": sys.executable,
            "args": [FS_MCP_MAIN],
            "transport": "stdio",
            "cwd": FS_MCP_DIR,
        },
    })
    tools = await client.get_tools()
    print(f"[ok] loaded {len(tools)} MCP tools: {[t.name for t in tools]}", file=sys.stderr)
    agent = create_react_agent(_make_llm(), tools, prompt=SYSTEM_PROMPT)
    return client, agent


async def ask(agent, history: list, user_text: str, recursion_limit: int = 60) -> str:
    history.append(HumanMessage(content=user_text))
    result = await agent.ainvoke(
        {"messages": history},
        config={"recursion_limit": recursion_limit},
    )
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
    "For today's customer_b_orders_raw rows, do the DB amounts match the upstream source file? If they differ, why?",
    "For today's customer_a_orders_raw rows, do the DB amounts match the upstream source file exactly? Pick a few primary keys and compare.",
]


async def main() -> None:
    _client, agent = await build_agent()
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
                ans = await ask(agent, history, q)
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
