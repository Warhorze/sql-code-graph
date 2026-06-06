"""Smoke test: MCP server stdio transport sends JSON-RPC frames on stdout.

This test guards against the v1.0.0/v1.0.1 regression where sys.stdout was
replaced with sys.stderr before stdio_server() captured sys.stdout.buffer,
causing all JSON-RPC frames to go to stderr and Claude Code to time out the
handshake.

The test spawns a real subprocess and performs the MCP initialize handshake.
It is NOT marked xfail — it must pass after PR-01 ships.
"""

import json
import os
import subprocess
import tempfile

INIT_REQUEST = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0"},
            },
        }
    )
    + "\n"
)


def test_mcp_start_handshake_on_stdout():
    """MCP initialize must produce a JSON-RPC result on stdout (fd 1).

    Guards against the v1.0.0/v1.0.1 regression where JSON-RPC frames went
    to stderr (fd 2) instead of stdout (fd 1) because sys.stdout was replaced
    by _configure_mcp_logging() before stdio_server() captured sys.stdout.buffer.

    After PR-01: _real_stdout_buffer = os.fdopen(os.dup(1), ...) is captured
    at module top before any redirect, and passed explicitly to stdio_server().
    """
    # Use a fresh temp DB so the test is unaffected by any stale or locked
    # default database at ~/.sqlcg/graph.db.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "smoke_test.db")
        env = {**os.environ, "SQLCG_DB_PATH": db_path}
        proc = subprocess.Popen(
            ["uv", "run", "sqlcg", "mcp", "start"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            proc.stdin.write(INIT_REQUEST.encode())
            proc.stdin.flush()
            # Read with a short timeout — the server must respond on stdout immediately
            line = proc.stdout.readline()
            assert line, (
                "sqlcg mcp start produced no output on stdout — "
                "stdio transport regression: JSON-RPC frames are going to stderr "
                "instead of stdout. Guards against v1.0.0 regression."
            )
            response = json.loads(line)
            assert "result" in response, (
                f"MCP initialize must return a result on stdout — "
                f"stdio transport regression. Got: {line!r}"
            )
            assert "capabilities" in response.get("result", {}), (
                f"MCP initialize result must contain capabilities field. Response was: {response}"
            )
        finally:
            proc.kill()
            proc.wait()
