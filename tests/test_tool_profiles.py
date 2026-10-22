"""Tool profiles (QGIS_MCP_TOOL_PROFILE): the granular surface, cut down.

The profile is applied at import, so each case runs in a subprocess with the
variable set - the same way a client launches the server. The numbers here are
the measured ones (see plan_optimizacion_ia_v2.md): read-only is 44 tools of
125, which is the largest saving available without compound mode's trade-offs.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import TOOL_COUNT

ROOT = Path(__file__).resolve().parents[1]

_LISTING = (
    "import json, sys\n"
    "sys.path.insert(0, 'src')\n"
    "import qgis_mcp.server as s\n"
    "print(json.dumps([t.name for t in s.mcp._tool_manager.list_tools()]))\n"
)


def _run(**env_vars):
    env = {k: v for k, v in os.environ.items() if not k.startswith("QGIS_MCP_")}
    env.update(env_vars)
    return subprocess.run(
        [sys.executable, "-c", _LISTING], cwd=ROOT, env=env, capture_output=True, text=True
    )


def _names(run):
    assert run.returncode == 0, run.stderr
    line = [line for line in run.stdout.splitlines() if line.startswith("[")][-1]
    return json.loads(line)


def test_the_default_profile_is_every_tool():
    assert len(_names(_run())) == TOOL_COUNT


def test_read_only_keeps_only_the_tools_marked_read_only():
    names = _names(_run(QGIS_MCP_TOOL_PROFILE="read-only"))

    assert len(names) == 44
    assert "get_layer_features" in names
    assert "remove_layer" not in names, "a destructive tool must not survive"
    assert "execute_code" not in names


def test_core_is_a_small_working_set():
    names = _names(_run(QGIS_MCP_TOOL_PROFILE="core"))

    assert len(names) == 24
    for required in ("get_layers", "get_layer_features", "execute_processing", "render_map"):
        assert required in names
    assert "create_processing_model" not in names, "the most expensive tool stays out"


def test_an_unknown_profile_refuses_to_start():
    run = _run(QGIS_MCP_TOOL_PROFILE="nope")

    assert run.returncode != 0
    assert "is not one of all, read-only, core" in run.stderr


def test_a_profile_with_compound_mode_refuses_instead_of_being_ignored():
    run = _run(QGIS_MCP_TOOL_PROFILE="read-only", QGIS_MCP_TOOL_MODE="compound")

    assert run.returncode != 0
    assert "applies to granular mode" in run.stderr
