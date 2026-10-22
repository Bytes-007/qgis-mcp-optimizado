"""Tool-listing budget: the schema block cannot grow without anyone noticing.

Every tool's description and JSON schemas are sent to the model on every turn,
before any work happens, so they are paid for whether the tool is used or not.
This test pins the total: a new tool, or a description that grows, shows up as a
failing budget instead of a bigger bill.

The metric is chars rather than tokens because no tokenizer is installed and
chars are reproducible. ``size()`` counts what the client receives: the
description, the input schema and - only when the tool declares one - the output
schema. Charging ``{}`` for a tool without an output schema used to add 162 chars
to the granular block and made the same listing measure differently depending on
how the script was written.
"""

import json
from unittest.mock import AsyncMock

import pytest
from conftest import COMPOUND_TOOL_COUNT, TOOL_COUNT

import qgis_mcp.server as srv
from qgis_mcp.compound_tools import FastMCP, register_compound_tools

# Measured on v0.15.2 + the strips below (see plan_optimizacion_ia_v2.md §1):
#   granular 52.842 (125 tools), compound 28.215 (27 tools).
# The margin is deliberate but small: it fits a couple of new tools, not a new
# subsystem. Raise it in the same commit that justifies the growth.
GRANULAR_BUDGET = 54_500
COMPOUND_BUDGET = 29_000
# Largest single tool today: create_processing_model 2.019 (granular) and
# processing 3.113 (compound).
GRANULAR_TOOL_BUDGET = 2_200
COMPOUND_TOOL_BUDGET = 3_300


def size(tool) -> int:
    """Chars this tool adds to every turn."""
    out = getattr(tool, "output_schema", None)
    return (
        len(tool.description or "")
        + len(json.dumps(tool.parameters, ensure_ascii=False, default=str))
        + (len(json.dumps(out, ensure_ascii=False, default=str)) if out else 0)
    )


def report(tools) -> str:
    """The five most expensive tools, so a failure says what grew."""
    rows = sorted(tools, key=size, reverse=True)[:5]
    return "\n".join(f"  {size(tool):6d}  {tool.name}" for tool in rows)


def _single_instance(monkeypatch):
    """Skip on multi-instance setups: ``instance`` stays on every tool (~17%)."""
    monkeypatch.delenv("QGIS_MCP_INSTANCES", raising=False)
    if len(srv.get_instances()) > 1:
        pytest.skip("multi-instance config: every tool advertises `instance` (~17% more)")


def compound_tools(monkeypatch):
    """The compound listing as a client sees it.

    The strips are applied here because the ones at import time only touch the
    module-level ``mcp``; measuring the raw registry instead reported 30.050
    chars for a block that actually ships as 28.215.
    """
    monkeypatch.delenv("QGIS_MCP_INSTANCES", raising=False)
    mcp = FastMCP("context-budget-test")
    register_compound_tools(
        mcp,
        _send=AsyncMock(return_value={}),
        _confirm_destructive=AsyncMock(return_value=True),
    )
    srv._strip_schema_titles(mcp)
    srv._strip_instance_param(mcp)
    return mcp._tool_manager.list_tools()


def test_granular_tool_list_stays_within_budget(monkeypatch):
    _single_instance(monkeypatch)
    tools = srv.mcp._tool_manager.list_tools()
    assert len(tools) == TOOL_COUNT
    total = sum(size(tool) for tool in tools)
    assert total <= GRANULAR_BUDGET, (
        f"granular tool listing is {total} chars, over the {GRANULAR_BUDGET} budget. "
        f"Longest tools:\n{report(tools)}"
    )


def test_granular_single_tool_stays_within_budget(monkeypatch):
    _single_instance(monkeypatch)
    over = [
        tool for tool in srv.mcp._tool_manager.list_tools() if size(tool) > GRANULAR_TOOL_BUDGET
    ]
    assert not over, "tools over the per-tool budget: " + ", ".join(
        f"{tool.name} ({size(tool)})" for tool in over
    )


def test_compound_tool_list_stays_within_budget(monkeypatch):
    tools = compound_tools(monkeypatch)
    assert len(tools) == COMPOUND_TOOL_COUNT
    total = sum(size(tool) for tool in tools)
    assert total <= COMPOUND_BUDGET, (
        f"compound tool listing is {total} chars, over the {COMPOUND_BUDGET} budget. "
        f"Longest tools:\n{report(tools)}"
    )


def test_compound_single_tool_stays_within_budget(monkeypatch):
    over = [
        tool for tool in compound_tools(monkeypatch) if size(tool) > COMPOUND_TOOL_BUDGET
    ]
    assert not over, "tools over the per-tool budget: " + ", ".join(
        f"{tool.name} ({size(tool)})" for tool in over
    )


def _titled_properties(schema) -> list[str]:
    """Properties still carrying pydantic's auto-generated ``title`` metadata.

    A parameter *named* ``title`` is legitimate (``add_layout_legend`` has one),
    so the check looks at the schema of each property, not at the key itself.
    """
    found = []

    def walk(node):
        if not isinstance(node, dict):
            return
        for name, sub in (node.get("properties") or {}).items():
            if isinstance(sub, dict) and "title" in sub:
                found.append(name)
            walk(sub)
        for key in ("$defs", "definitions", "items", "additionalProperties"):
            walk(node.get(key))
        for key in ("anyOf", "allOf", "oneOf"):
            for sub in node.get(key) or []:
                walk(sub)

    walk(schema)
    return found


def test_the_budget_measures_what_the_client_sees(monkeypatch):
    """The strips must reach the listing the budget is measured from.

    This is the regression the budget itself could not catch: measuring a server
    without the strips reported the compound block 1.835 chars too high, so the
    threshold protected the wrong number.
    """
    _single_instance(monkeypatch)
    for tool in srv.mcp._tool_manager.list_tools() + compound_tools(monkeypatch):
        assert "title" not in tool.parameters, f"{tool.name} keeps the schema title"
        assert not _titled_properties(tool.parameters), (
            f"{tool.name} still advertises pydantic titles on "
            f"{', '.join(_titled_properties(tool.parameters))}"
        )
        assert "instance" not in tool.parameters.get("properties", {}), tool.name
