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


async def test_anomaly_explains_customer_b_drop(agent, fresh_history):
    """Today's total_revenue dropped — agent must localise to customer_b."""
    a = await _ask(
        agent, fresh_history,
        "Today (2026-04-26) the daily_metrics.total_revenue is much lower "
        "than the prior 30-day average. By how much, and which upstream "
        "source caused the drop?",
    )

    assert "customer_b" in a or "customer-b" in a or "customer b" in a, (
        "Agent must name customer_b as the broken upstream feed."
    )
    # The broken feed shipped only 5 orders today; the answer should cite
    # that level of drop somewhere (mention "5", or a clearly-low ratio).
    assert any(tok in a for tok in [" 5 ", "(5)", " 5.", " 5,", "only 5", "5 orders", "5 rows"]), (
        "Agent should mention that customer_b had ~5 records today."
    )
    # Today's date should appear so we know the agent actually compared.
    assert "2026-04-26" in a or "today" in a


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
