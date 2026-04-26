"""
Shared fixtures for data-trace-agent pytests.

These tests hit a real LLM (via OPENROUTER_API_KEY / OPENAI_API_KEY) and the
real `sqllite-mcp-server` over stdio. They are slow and cost money — keep the
suite small and deterministic (temperature=0 in trace_agent._make_llm).
"""
from __future__ import annotations

import os
import sys

import pytest
import pytest_asyncio

# Make the project root importable when pytest is run from the package dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import setup_warehouse  # noqa: E402
import trace_agent  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Skip the whole suite if no LLM credentials are available."""
    if os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return
    skip = pytest.mark.skip(reason="No OPENROUTER_API_KEY / OPENAI_API_KEY in env")
    for item in items:
        item.add_marker(skip)


@pytest_asyncio.fixture(scope="session")
async def warehouse():
    """Build the simulated warehouse once per test session."""
    setup_warehouse.main()
    return setup_warehouse.DB_PATH


@pytest_asyncio.fixture(scope="session")
async def agent(warehouse):
    """Spin up the LangGraph ReAct agent + MCP client once per session."""
    _client, ag = await trace_agent.build_agent()
    return ag


@pytest_asyncio.fixture
async def fresh_history():
    """Per-test conversation history (each test starts clean)."""
    return []
