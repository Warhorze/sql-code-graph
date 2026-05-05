## v0.2.1 (2026-05-05)

### Fix

- **benchmarks**: add bench_*.py to pytest python_files so bench_indexer.py is collected
- **tests**: use tracked variable in fake_run and regenerate cli docs
- **tests**: strip ANSI codes in test_cli_help assertion

## v0.2.0 (2026-05-05)

### Feat

- **walker**: default to git ls-files to avoid indexing untracked files
- **phase-9+10**: git-aware graph freshness + PyPI publishing
- **phase-8**: metrics and feedback layer

### Fix

- **tools**: move inline Cypher queries to queries.py and fix decorator order (C1, C2)
- **walker**: address code-review findings C2/W1/W2/W4/S1
