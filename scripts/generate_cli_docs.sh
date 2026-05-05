#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_FILE="$ROOT_DIR/docs/cli.md"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  PYTHON_BIN="python"
fi

mkdir -p "$ROOT_DIR/docs"

PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON_BIN" - "$OUT_FILE" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import click
import typer

from sqlcg.cli.main import app


def md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def fmt_default(command: click.Command, param: click.Parameter) -> str:
    if not isinstance(param, click.Option):
        return ""
    if not param.show_default:
        return ""
    default = param.get_default(click.Context(command), call=False)
    if default is None:
        return ""
    if isinstance(default, (tuple, list)):
        return ", ".join(str(v) for v in default)
    return str(default)


def fmt_type(param: click.Parameter) -> str:
    try:
        if hasattr(param, 'make_metavar'):
            # Click parameters
            metavar = param.make_metavar()
            if metavar:
                return metavar
    except TypeError:
        pass

    ptype = getattr(param, "type", None)
    if ptype is None:
        return ""
    name = getattr(ptype, "name", "")
    return str(name).upper() if name else ""


def fmt_option_name(opt: click.Option) -> str:
    if opt.secondary_opts:
        return f"{opt.opts[0]} / {opt.secondary_opts[0]}"
    return ", ".join(opt.opts)


def option_rows(command: click.Command) -> list[str]:
    rows = []
    for param in command.params:
        if not isinstance(param, click.Option):
            continue
        desc = param.help or ""
        if isinstance(param.type, click.Choice):
            choices = ", ".join(str(choice) for choice in param.type.choices)
            desc = f"{desc} Choices: {choices}."
        row = [
            md_escape(fmt_option_name(param)),
            md_escape(fmt_type(param)),
            "Yes" if param.required else "No",
            "Yes" if param.multiple else "No",
            md_escape(fmt_default(command, param)),
            md_escape(desc.strip()),
        ]
        rows.append("| " + " | ".join(row) + " |")
    return rows


def usage_for(command: click.Command, path: str) -> str:
    ctx = click.Context(command, info_name=path)
    return command.get_usage(ctx).strip().replace("Usage: ", "")


def build() -> str:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)

    lines: list[str] = [
        "# CLI Reference",
        "",
        "This page is auto-generated from CLI command metadata.",
        "",
        "Regenerate with:",
        "",
        "```bash",
        "bash scripts/generate_cli_docs.sh",
        "```",
        "",
        "## Commands",
        "",
        "| Command | Description |",
        "| --- | --- |",
    ]

    # Track all top-level and subcommand names
    all_commands = set()

    # List top-level commands and command groups
    for name, cmd in root.commands.items():
        desc = (cmd.help or "").strip().splitlines()[0] if cmd.help else ""
        lines.append(f"| `{name}` | {md_escape(desc)} |")
        all_commands.add(name)

    lines.extend(
        [
            "",
            "## `sqlcg`",
            "",
            "```bash",
            usage_for(root, "sqlcg"),
            "```",
            "",
            (root.help or "").strip(),
            "",
            "### Global Options",
            "",
            "| Option | Type | Required | Repeatable | Default | Description |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(option_rows(root))

    # Document each top-level command and subcommand groups
    for name, cmd in root.commands.items():
        lines.extend(
            [
                "",
                f"## `sqlcg {name}`",
                "",
                "```bash",
                usage_for(cmd, f"sqlcg {name}"),
                "```",
                "",
                (cmd.help or "").strip(),
                "",
            ]
        )

        # Check if this is a subcommand group (has subcommands)
        if isinstance(cmd, click.Group) and cmd.commands:
            lines.extend(
                [
                    "### Subcommands",
                    "",
                    "| Subcommand | Description |",
                    "| --- | --- |",
                ]
            )
            for sub_name, sub_cmd in cmd.commands.items():
                sub_desc = (sub_cmd.help or "").strip().splitlines()[0] if sub_cmd.help else ""
                lines.append(f"| `{sub_name}` | {md_escape(sub_desc)} |")
                all_commands.add(f"{name}.{sub_name}")

            # Document each subcommand
            for sub_name, sub_cmd in cmd.commands.items():
                lines.extend(
                    [
                        "",
                        f"## `sqlcg {name} {sub_name}`",
                        "",
                        "```bash",
                        usage_for(sub_cmd, f"sqlcg {name} {sub_name}"),
                        "```",
                        "",
                        (sub_cmd.help or "").strip(),
                        "",
                        "### Options",
                        "",
                        "| Option | Type | Required | Repeatable | Default | Description |",
                        "| --- | --- | --- | --- | --- | --- |",
                    ]
                )
                rows = option_rows(sub_cmd)
                if rows:
                    lines.extend(rows)
                else:
                    lines.append("| _none_ |  |  |  |  |  |")
        else:
            # This is a simple command with options
            lines.extend(
                [
                    "### Options",
                    "",
                    "| Option | Type | Required | Repeatable | Default | Description |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            rows = option_rows(cmd)
            if rows:
                lines.extend(rows)
            else:
                lines.append("| _none_ |  |  |  |  |  |")

    return "\n".join(lines).strip() + "\n"


out = Path(sys.argv[1])
out.write_text(build(), encoding="utf-8")
PY
