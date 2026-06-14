"""End-to-end tests for the `sqlcg viz` CLI command.

Guards the observable acceptance of the graph-viz feature
([plan doc](plan/sprints/feature_graph_viz.md), Test Strategy §E2E):

1. self-containment — no external resource-loading token; node --check passes on
   each emitted <script> block.
2. baked DATA.nodes carry id/schema/kind/deg/cat/tags/jobs + DATA.adj present.
3. a glob tag/job CSV maps the right nodes (many-to-many).
4. kind defaults: table/view/temp ON, cte/derived OFF (emitted unchecked).
5. config-driven schemas baked from [sqlcg.viz]; data-derived fallback when absent.
6. edge model — a SqlQuery-anchored SELECTS_FROM produces a source->target table
   link (not a query node).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from sqlcg.cli.main import app
from sqlcg.core.duckdb_backend import DuckDBBackend

runner = CliRunner()


@pytest.fixture
def seeded_db(tmp_path):
    """A file-backed DuckDB graph the CLI reads via SQLCG_DB_PATH fallback."""
    db_path = str(tmp_path / "graph.db")
    b = DuckDBBackend(db_path)
    b.init_schema()
    tables = [
        ("ba.orders", "ba", "table"),
        ("ba.orders_bck", "ba", "table"),
        ("da.fact_sales", "da", "view"),
        ("da.cte_tmp", "da", "cte"),
    ]
    for qualified, db, kind in tables:
        b.run_write(
            'INSERT INTO "SqlTable" (qualified, name, catalog, db, kind) VALUES (?,?,?,?,?)',
            {
                "qualified": qualified,
                "name": qualified.split(".")[-1],
                "catalog": "c",
                "db": db,
                "kind": kind,
            },
        )
    b.run_write(
        'INSERT INTO "HAS_COLUMN" (src_key, dst_key, source) VALUES (?,?,?)',
        {"src_key": "ba.orders", "dst_key": "ba.orders.id", "source": "ddl"},
    )
    b.run_write(
        'INSERT INTO "SqlQuery" (id, file_path, statement_index, sql, kind, '
        "target_table, parse_failed, confidence, parsing_mode, start_line) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        {
            "id": "q1",
            "file_path": "etl.sql",
            "statement_index": 0,
            "sql": "INSERT INTO da.fact_sales SELECT * FROM ba.orders",
            "kind": "INSERT",
            "target_table": "da.fact_sales",
            "parse_failed": False,
            "confidence": 1.0,
            "parsing_mode": "sqlglot",
            "start_line": 1,
        },
    )
    b.run_write(
        'INSERT INTO "SELECTS_FROM" (src_key, dst_key) VALUES (?,?)',
        {"src_key": "q1", "dst_key": "ba.orders"},
    )
    b.close()
    return db_path, tmp_path


def _run_viz(seeded_db, extra_args=None, config_dir=None):
    db_path, tmp_path = seeded_db
    out = tmp_path / "graph.html"
    args = ["viz", "--out", str(out)]
    if config_dir is not None:
        args += ["--config-dir", str(config_dir)]
    if extra_args:
        args += extra_args
    result = runner.invoke(app, args, env={"SQLCG_DB_PATH": db_path}, catch_exceptions=False)
    return result, out


def _data_block(html: str) -> dict:
    m = re.search(r"const DATA = (\{.*?\});", html, re.S)
    assert m is not None, "DATA const not found in emitted HTML"
    return json.loads(m.group(1))


def test_viz_writes_file_and_prints_counts(seeded_db):
    """`sqlcg viz` exits 0, writes the HTML, and prints node/edge/schema counts."""
    result, out = _run_viz(seeded_db)
    assert result.exit_code == 0, result.output
    assert out.exists()
    flat = " ".join(result.output.split())
    assert "4 nodes" in flat
    assert "1 edges" in flat
    assert "schemas" in flat


def test_viz_html_is_self_contained_and_node_check_passes(seeded_db):
    """No external resource-loading token; node --check passes on both script blocks."""
    result, out = _run_viz(seeded_db)
    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    for tok in (
        'src="http',
        'src="//',
        'href="http',
        'href="//',
        "fetch(",
        "<link",
        "import(",
        "cdnjs",
        "unpkg",
        "jsdelivr",
    ):
        assert tok not in html, f"external-load token present: {tok!r}"
    assert "ForceGraph" in html
    assert "const DATA = " in html

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available for --check gate")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) == 2
    for i, block in enumerate(blocks):
        js = out.parent / f"block_{i}.js"
        js.write_text(block, encoding="utf-8")
        proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        assert proc.returncode == 0, f"node --check failed on block {i}: {proc.stderr}"


def test_viz_baked_nodes_carry_all_fields_and_adj_present(seeded_db):
    """DATA.nodes carry id/schema/kind/deg/cat/tags/jobs; DATA.adj is present."""
    result, out = _run_viz(seeded_db)
    data = _data_block(out.read_text(encoding="utf-8"))
    assert "adj" in data
    by_id = {n["id"]: n for n in data["nodes"]}
    node = by_id["ba.orders"]
    assert {"id", "schema", "kind", "deg", "cat", "tags", "jobs"} <= set(node)
    assert node["schema"] == "ba"
    assert node["cat"] is True
    assert by_id["da.fact_sales"]["kind"] == "view"


def test_viz_edge_model_select_produces_source_to_target_link(seeded_db):
    """A SqlQuery-anchored SELECTS_FROM yields ba.orders->da.fact_sales (no query node)."""
    result, out = _run_viz(seeded_db)
    data = _data_block(out.read_text(encoding="utf-8"))
    pairs = {(lk["source"], lk["target"]) for lk in data["links"]}
    assert ("ba.orders", "da.fact_sales") in pairs
    node_ids = {n["id"] for n in data["nodes"]}
    assert "q1" not in node_ids  # the query node never leaks into the bake
    assert data["links"][0]["n"] >= 1  # new additive count field


def test_viz_tag_glob_csv_maps_matching_nodes_m2m(seeded_db):
    """A glob tag row tags exactly the matching node ids (many-to-many)."""
    db_path, tmp_path = seeded_db
    tags_csv = tmp_path / "tags.csv"
    tags_csv.write_text(
        "pattern,label,color\nba.*_bck,backup,#6e7681\nba.*,owned,\n",
        encoding="utf-8",
    )
    result, out = _run_viz(seeded_db, extra_args=["--tags", str(tags_csv)])
    data = _data_block(out.read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["ba.orders_bck"]["tags"] == ["backup", "owned"]
    assert by_id["ba.orders"]["tags"] == ["owned"]
    assert by_id["da.fact_sales"]["tags"] == []


def test_viz_job_csv_drives_job_facet(seeded_db):
    """A jobs CSV resolves into node jobs[] (the job dropdown facet)."""
    db_path, tmp_path = seeded_db
    jobs_csv = tmp_path / "jobs.csv"
    jobs_csv.write_text("pattern,label\nba.*,ej_daily\n", encoding="utf-8")
    result, out = _run_viz(seeded_db, extra_args=["--jobs", str(jobs_csv)])
    data = _data_block(out.read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["ba.orders"]["jobs"] == ["ej_daily"]
    assert by_id["da.fact_sales"]["jobs"] == []


def test_viz_kind_defaults_table_view_temp_on_cte_derived_off(seeded_db):
    """The emitted template wires table/view/temp ON, cte/derived OFF; no external kind."""
    result, out = _run_viz(seeded_db)
    html = out.read_text(encoding="utf-8")
    assert "KIND_DEFAULT_ON = new Set(['table', 'view', 'temp'])" in html
    assert "KNOWN_KINDS = ['table', 'view', 'temp', 'cte', 'derived']" in html
    assert "'external'" not in html


def test_viz_schema_config_baked_from_toml(seeded_db):
    """[sqlcg.viz] schemas is baked into SCHEMA_CONFIG."""
    db_path, tmp_path = seeded_db
    (tmp_path / ".sqlcg.toml").write_text('[sqlcg.viz]\nschemas = ["ba", "da"]\n', encoding="utf-8")
    result, out = _run_viz(seeded_db, config_dir=tmp_path)
    html = out.read_text(encoding="utf-8")
    m = re.search(r"const SCHEMA_CONFIG = (\{.*?\});", html, re.S)
    assert m is not None
    cfg = json.loads(m.group(1))
    assert cfg["schemas"] == ["ba", "da"]


def test_viz_schema_config_falls_back_to_data_derived(seeded_db):
    """With no [sqlcg.viz] block, SCHEMA_CONFIG.schemas is empty (client derives)."""
    result, out = _run_viz(seeded_db, config_dir=seeded_db[1])
    html = out.read_text(encoding="utf-8")
    m = re.search(r"const SCHEMA_CONFIG = (\{.*?\});", html, re.S)
    assert m is not None
    cfg = json.loads(m.group(1))
    assert cfg["schemas"] == []
    # The data-derived count still reports both schemas in the summary line.
    flat = " ".join(result.output.split())
    assert "2 schemas" in flat


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
