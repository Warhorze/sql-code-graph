"""Guard: _upsert_parsed_file must use upsert_nodes_bulk/upsert_edges_bulk.

Measured regression: per-node upsert produced 1020s+ on 1,340-file DWH
vs 181s with bulk UNWIND MERGE. Commit 4234e5d regressed this silently.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from sqlcg.indexer.indexer import Indexer


def _make_mock_db():
    mock_db = MagicMock()
    mock_db.run_read = MagicMock(return_value=[])
    return mock_db


def test_upsert_parsed_file_uses_bulk_not_per_node():
    """_upsert_parsed_file must call upsert_nodes_bulk/upsert_edges_bulk,
    never upsert_node/upsert_edge.

    Bulk upsert = ~10 execute() calls for a batch of files.
    Per-node upsert = ~14,500 execute() calls for 1,340 files = 1020s+ on DWH.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "test.sql").write_text("SELECT id, name FROM customers")

        mock_db = _make_mock_db()
        indexer = Indexer()
        indexer.index_repo(Path(tmpdir), dialect=None, db=mock_db)

    assert mock_db.upsert_nodes_bulk.called, (
        "upsert_nodes_bulk was never called — _upsert_parsed_file may have "
        "regressed to per-node upsert_node() (bulk=181s vs per-node=1020s+ on 1,340 files)"
    )
    assert not mock_db.upsert_node.called, (
        "upsert_node was called — use upsert_nodes_bulk instead "
        "(10 execute() calls vs ~14,500 for a 1,340-file repo)"
    )
    assert not mock_db.upsert_edge.called, "upsert_edge was called — use upsert_edges_bulk instead"
