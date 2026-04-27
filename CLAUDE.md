# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A POC for a data-lineage / ETL-bug-hunting agent. A LangGraph `create_react_agent`
is wired to **two MCP servers over stdio** and asked questions about a tiny
simulated warehouse:

- `mcp/sqllite_mcp_server.py` — SQL access to `data/warehouse.db`
- `mcp/filesystem_mcp_server.py` — read raw upstream files under `data/sources/`

The whole point of running both servers is that some ETL bugs are only
detectable by **comparing what's in the DB to what was in the source file**
(e.g. file says `119.06`, DB says `119` because the loader did `int(amount)`).
SQL alone can't see this.

## Common commands

```bash
# Build the SQLite warehouse + write today's upstream source files.
# Re-run any time you want a clean state. TODAY is hardcoded to 2026-04-26
# in setup_warehouse.py — changing it requires re-running this script.
python3 setup_warehouse.py

# REPL. Requires an LLM endpoint:
export OPENROUTER_API_KEY=...        # or OPENAI_API_KEY
# optional: LLM_BASE_URL (default https://openrouter.ai/api/v1)
#           LLM_MODEL    (default anthropic/claude-sonnet-4.5)
python3 trace_agent.py

# End-to-end pytest suite (hits the real LLM; ~2 min; costs money).
# Auto-skips when no API key is in env.
python3 -m pytest -s
python3 -m pytest -s tests/test_lineage_qa.py::test_anomaly_finds_customer_b_currency_bug   # single test
```

`pytest.ini` sets `asyncio_mode = auto` and `testpaths = tests`. Both the
warehouse build and the agent are session-scoped fixtures in `tests/conftest.py`,
so the warehouse is rebuilt and the MCP servers are spun up once per run.

## Architecture

### The agent loop (`trace_agent.py`)

`build_agent()` instantiates a `MultiServerMCPClient` against the two stdio
MCP servers, fetches their tools, and hands them to `create_react_agent` with
`SYSTEM_PROMPT`. The system prompt is the *contract*: it tells the LLM where
the DB is, that it must always pass `db_path=DB_PATH` to SQL tools, where the
source files live, and gives an "anomaly playbook" + "discrepancy playbook" so
the model knows to cross-reference DB and disk.

`ask()` keeps a single rolling `history` list across turns in the REPL — the
agent shares conversation state between user questions.

### Lineage metadata is in the DB itself

`setup_warehouse.py` plants two metadata tables that the agent treats as the
source of truth:

- `_field_lineage(target_table, target_field, source_table, source_field, transform, etl_job)`
  — one row per (target field, source field) edge.
- `_source_registry(source_table, source_uri, file_dir, loader, schema_note)`
  — for each raw table, where its physical files live on disk and which loader
  ingests them.

The agent reads `_source_registry` to discover the file path for a raw table,
then uses the filesystem MCP to read it. **Don't move lineage out into prompt
text** — keeping it queryable in the DB is the design.

### Planted bugs (tests depend on these)

`setup_warehouse.py` deliberately runs *buggy* loaders for `TODAY = 2026-04-26`:

1. `load_customer_b_orders` silently filters out non-USD rows. File has 80
   orders (75 EUR + 5 USD), DB has 5. Causes a ~60% drop in
   `daily_metrics.total_revenue`. Detectable only by reading the CSV.
2. `load_customer_a_orders` does `int(amount)` instead of `float(amount)`,
   truncating cents. File says `119.06`, DB says `119`.

`s3_clickstream` and `app_logs` load cleanly and act as a control — the agent
should *not* flag them. The pytest assertions check both that the agent
identifies these specific bugs (loader name, EUR/currency keyword, 80 vs 5
counts) and that it doesn't hallucinate bugs into the clean feeds.

### MCP servers

Both servers use `FastMCP` and run over stdio. They are spawned by
`MultiServerMCPClient` with `cwd=MCP_DIR` (`/mcp`) — that working directory
matters: `sqllite_mcp_server.py` does `from database.connection_manager
import …`, which only resolves when cwd is the `mcp/` directory. If you add
new MCP entrypoints, keep them in `mcp/` and run them with that cwd, or
restructure the imports.

The SQL MCP exposes `connect_database`, `execute_query`, `describe_table`,
`add_field_lineage`, `trace_field_lineage`, `analyze_query_lineage`,
`generate_sample_data`, `import_csv`, `export_table_to_csv`. Every SQL tool
takes an explicit `db_path` arg — the agent must pass `data/warehouse.db`
each time. The filesystem MCP enforces an extension allowlist
(`ALLOWED_EXTENSIONS` in `filesystem_mcp_server.py`) and a command blocklist;
expand those sets if a new source format or admin command is needed.

## Conventions worth knowing

- **Today's date is fixed** at `2026-04-26` (`setup_warehouse.TODAY`,
  referenced in the system prompt and tests). When changing it, update both
  places and regenerate the warehouse.
- **Temperature is 0** in `_make_llm()` to keep the e2e tests deterministic.
- **Tests assert on lowercased substrings** of the agent's final answer
  (e.g. `"customer_b"`, `"load_customer_a_orders"`, `"truncat"`). When
  changing the system prompt or planted-bug names, run the full suite — a
  rename will silently break a substring assertion.
