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
import json
import os
import sys

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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
            "cwd": MCP_DIR,
        },
        "filesystem": {
            "command": sys.executable,
            "args": [FS_MCP_MAIN],
            "transport": "stdio",
            "cwd": MCP_DIR,
        },
    })
    tools = await client.get_tools()
    print(f"[ok] loaded {len(tools)} MCP tools: {[t.name for t in tools]}", file=sys.stderr)
    agent = create_react_agent(_make_llm(), tools, prompt=SYSTEM_PROMPT)
    return client, agent


# Args we want to surface prominently in the tool-call log (the actual SQL,
# the shell command, the file path being read, etc.). Order matters: first
# match wins.
_PRIMARY_ARG_KEYS = (
    "query", "sql", "command", "file_path", "directory_path",
    "path", "expression", "table", "table_name",
)


def _truncate(text, n: int = 800) -> str:
    text = str(text)
    if len(text) <= n:
        return text
    return text[:n] + f"... [+{len(text) - n} chars truncated]"


def _format_tool_call(name: str, args) -> str:
    if not isinstance(args, dict):
        return f"[tool-call] {name}({args!r})"

    primary_key = next((k for k in _PRIMARY_ARG_KEYS if k in args), None)
    rest = {k: v for k, v in args.items() if k != primary_key}
    rest_blob = json.dumps(rest, ensure_ascii=False, default=str) if rest else ""

    lines = [f"[tool-call] {name}"]
    if primary_key is not None:
        primary_val = str(args[primary_key])
        if "\n" in primary_val or len(primary_val) > 80:
            lines.append(f"  {primary_key}:")
            for ln in primary_val.splitlines() or [primary_val]:
                lines.append(f"    {ln}")
        else:
            lines.append(f"  {primary_key}: {primary_val}")
    if rest_blob:
        lines.append(f"  args: {rest_blob}")
    return "\n".join(lines)


def _print_message_event(m) -> None:
    if isinstance(m, AIMessage):
        for tc in getattr(m, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "?")
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            print("\n" + _format_tool_call(name, args), file=sys.stderr, flush=True)
    elif isinstance(m, ToolMessage):
        name = getattr(m, "name", "?") or "?"
        print(f"[tool-result:{name}] {_truncate(m.content)}", file=sys.stderr, flush=True)


async def ask(agent, history: list, user_text: str, recursion_limit: int = 60) -> str:
    history.append(HumanMessage(content=user_text))
    new_messages: list = []
    async for chunk in agent.astream(
        {"messages": history},
        config={"recursion_limit": recursion_limit},
        stream_mode="updates",
    ):
        if not isinstance(chunk, dict):
            continue
        for update in chunk.values():
            if not isinstance(update, dict):
                continue
            for m in update.get("messages", []) or []:
                new_messages.append(m)
                _print_message_event(m)
    history.extend(new_messages)
    for m in reversed(new_messages):
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
