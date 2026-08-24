"""CLI command generation, arg parsing, output, and async handling.

Tests use ``click.testing.CliRunner`` against ``_make_command`` directly (for the
arg/output/async logic) and against the generated ``cli`` command tree (for the
registry-coverage invariant). No live MCP transport needed.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from collections import defaultdict

import click
from click.testing import CliRunner

from daas.fd_daas_mcp import registry
from daas.fd_daas_mcp.cli import _make_command, cli


def test_cli_has_subcommand_per_registered_tool():
    """The generated CLI tree has a <group> subgroup per group, each with a
    <tool> command per registered tool."""
    tools = registry.build()
    by_group = defaultdict(set)
    for g, name, _ in tools:
        by_group[g].add(name)

    # Tool subcommands are built lazily; force the build so cli.commands reflects
    # the registry (init/doctor are registered eagerly and always present).
    cli._ensure_built()

    for g, names in by_group.items():
        assert g in cli.commands, f"group {g!r} missing from CLI tree"
        grp = cli.commands[g]
        assert isinstance(grp, click.Group)
        cmd_names = set(grp.commands.keys())
        assert names <= cmd_names, f"group {g!r}: missing tools {names - cmd_names}"

    total = sum(len(g.commands) for g in cli.commands.values() if isinstance(g, click.Group))
    assert total == len(tools), f"CLI leaf-command count {total} != tool count {len(tools)}"


def test_cli_groups_match_registry_groups():
    tools = registry.build()
    reg_groups = {g for g, _, _ in tools}
    cli._ensure_built()
    # init/doctor are eagerly-registered top-level commands, not registry groups.
    cli_groups = {g for g in cli.commands} - {"init", "doctor"}
    assert reg_groups == cli_groups, f"CLI groups {cli_groups} != registry groups {reg_groups}"


def test_init_doctor_registered_eagerly_without_registry():
    """init and doctor are top-level commands available before the tool registry
    is built (so they can run on a fresh install with no database)."""
    registry.reset_cache()
    # Accessing cli.commands is a plain dict lookup; it must not trigger a
    # registry build. init/doctor are registered eagerly at module import.
    assert "init" in cli.commands
    assert "doctor" in cli.commands
    assert registry._BUILD_CACHE is None, "init/doctor must not require the registry"


def test_malformed_arg_exits_2():
    def echo(x):  # pragma: no cover - argv parsing fails first
        return x
    cmd = _make_command("echo", echo)
    result = CliRunner().invoke(cmd, ["notkeyvalue"])
    assert result.exit_code == 2


def test_missing_required_param_exits_2():
    def needs(x):  # pragma: no cover - required-param check fails first
        return x
    cmd = _make_command("needs", needs)
    result = CliRunner().invoke(cmd, [])
    assert result.exit_code == 2


def test_json_flag_prints_structured_output():
    def data():
        return {"a": 1, "b": [2, 3]}
    cmd = _make_command("data", data)
    result = CliRunner().invoke(cmd, ["--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == {"a": 1, "b": [2, 3]}


def test_non_json_prints_str():
    def plain():
        return "hello"
    cmd = _make_command("plain", plain)
    result = CliRunner().invoke(cmd, [])
    assert result.exit_code == 0
    assert "hello" in result.output


def test_async_tool_is_awaited():
    async def afunc():
        await asyncio.sleep(0)
        return "async-result"

    cmd = _make_command("afunc", afunc)
    result = CliRunner().invoke(cmd, [])
    assert result.exit_code == 0
    assert "async-result" in result.output


def test_key_value_args_parsed():
    def add(x, y):
        return int(x) + int(y)
    cmd = _make_command("add", add)
    result = CliRunner().invoke(cmd, ["x=1", "y=2", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == 3
