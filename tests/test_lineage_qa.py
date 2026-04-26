"""
End-to-end pytests for the data-trace agent.

Each test asks a real lineage / data-support question, lets the LangGraph
ReAct agent loop call the MCP SQL tools, and asserts the final answer
mentions the right upstream tables / source URIs / numbers.

Assertions are deliberately lenient on phrasing (lowercased substring checks)
but strict on the entities the agent has to surface — if it forgets to
mention `customer_b`, the test fails.
"""
from __future__ import annotations

import pytest

import trace_agent

pytestmark = pytest.mark.asyncio


async def _ask(agent, history, q: str) -> str:
    answer = await trace_agent.ask(agent, history, q)
    print(f"\n--- Q: {q}\n--- A: {answer}\n")
    return answer.lower()


async def test_anomaly_finds_customer_b_currency_bug(agent, fresh_history):
    """Today's total_revenue dropped. Agent must:
       1) localise the drop to customer_b, and
       2) read the upstream CSV file and discover that the loader dropped
          EUR rows (the source has 80 rows, only 5 made it to DB)."""
    a = await _ask(
        agent, fresh_history,
        "Today (2026-04-26) the daily_metrics.total_revenue is much lower "
        "than the prior 30-day average. By how much, and what is the root "
        "cause? Check the upstream raw source file too — the file may "
        "contain rows that the loader never inserted.",
    )

    assert "customer_b" in a or "customer-b" in a or "customer b" in a
    # Agent must have discovered the EUR/currency story from the file.
    assert "eur" in a or "currency" in a, (
        "Agent should identify the currency filter as the root cause "
        "(only possible by reading the source CSV)."
    )
    # Source has 80 rows, only 5 in DB — both numbers should appear.
    assert "80" in a, "Source file row count (80) should appear."
    assert any(tok in a for tok in [" 5 ", "(5)", " 5.", " 5,", "only 5", "5 orders", "5 rows", "5/80"]), (
        "Agent should mention that only 5 rows reached the DB."
    )


async def test_lineage_total_events_lists_both_event_sources(agent, fresh_history):
    """total_events is fed by clickstream + app logs — both must be named."""
    a = await _ask(
        agent, fresh_history,
        "Where does daily_metrics.total_events come from upstream? "
        "List every source table that feeds it.",
    )
    assert "s3_clickstream_raw" in a, "Must name s3_clickstream_raw upstream."
    assert "app_logs_raw" in a, "Must name app_logs_raw upstream."
    # Should not invent customer order tables in this lineage edge.
    assert "customer_a_orders_raw" not in a
    assert "customer_b_orders_raw" not in a


async def test_lineage_total_revenue_lists_both_order_feeds(agent, fresh_history):
    """total_revenue lineage must name both customer order feeds."""
    a = await _ask(
        agent, fresh_history,
        "Where does daily_metrics.total_revenue come from upstream? "
        "Show the source tables and the ETL job name.",
    )
    assert "customer_a_orders_raw" in a
    assert "customer_b_orders_raw" in a
    assert "agg_orders_daily" in a, "Should mention the ETL job name from _field_lineage."


async def test_customer_a_precision_discrepancy_file_vs_db(agent, fresh_history):
    """customer_a CSV stores amounts with cents (e.g. 119.06) but the loader
    casts via int() — DB has 119. The agent has to read the file AND query
    the DB to find this."""
    a = await _ask(
        agent, fresh_history,
        "For today's (2026-04-26) customer_a_orders_raw rows, do the DB "
        "amounts match the upstream source file exactly? Pick a few primary "
        "keys, read the source file, and compare them to the DB row by row. "
        "If they differ, name the loader and explain what the bug is.",
    )

    # Agent must have noticed the values differ.
    assert any(tok in a for tok in ["differ", "mismatch", "discrepan", "do not match",
                                    "doesn't match", "does not match", "not match", "truncat"]), (
        "Agent must report that file and DB values disagree."
    )
    # Agent must name the buggy loader from _source_registry.
    assert "load_customer_a_orders" in a, "Should name the buggy loader."
    # Agent must characterize the bug (truncation / int / decimals / cents).
    assert any(tok in a for tok in ["truncat", "int(", " int ", "integer",
                                    "decimal", "cents", "precision", "rounded",
                                    "rounding"]), (
        "Should describe the precision/truncation nature of the bug."
    )
