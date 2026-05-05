# Feature Plan: Sprint Next — v0.3.0 Implementation

Review date: 2026-05-05
Author: architect-planner
Source authority: ARCHITECTURE_REVIEW.md sections 10.1–10.2.10, 10.B, 10.C, 8.2 comment 7, 4 (ranked improvements)

Policy: No backward compatibility. Breaking changes to MCP models, graph schema, MetricsStore
SQLite schema, and `.sqlcg.toml` format are acceptable without deprecation cycles. Re-index is
the migration path for graph schema changes.

---

## Summary

This plan covers all open work from the ARCHITECTURE_REVIEW.md as of 2026-05-05. It groups 13
ranked items from section 10.4 plus the three architectural improvements from section 4 (findings
3.1, 3.2, 3.4) and the parser refactor from section 8.2 comment 7 into 12 discrete tickets.
Dependencies are mapped explicitly. Each ticket is sized to fit in a single PR.

---

## Scope

### In Scope

All open items from the architecture review:
- ARCHITECTURE_REVIEW.md section 10.4 ranks 1–13 (issues #5 and #6 remediation)
- Section 4 findings 3.1, 3.2, 3.4 (atomicity, file re-open safety, exception recording)
- Section 8.2 comment 7 (`_classify` match/case + `exp.DML` refactor)
- Section 10.2.7 (tokenizer-based DML extraction, M effort)
- Section 10.2.4 (medallion-aware `analyze unused`)
- Section 10.B (hint field in result models)
- Section 10.C (parse_quality breakdown)

### Non-Goals

- `add_information_schema` (deferred to v2, finding 3.8 — raise `NotImplementedError` is already in code)
- `wizard.py` (no spec, confirmed scope creep)
- Cross-session temp table chains (fundamental static-analysis limit, section 6.4)
- BigQuery procedure body full parsing (fundamental limit, regex fallback is the defined approach)
- T-SQL procedure body parsing (not yet verified, out of scope)
- KùzuDB cluster mode
- DataHub pipeline integration

---

## Ticket Table

| Ticket | Title | Files Touched | Effort | Impact | Dependency | Blocks |
|--------|-------|---------------|--------|--------|------------|--------|
| T-01 | Add QUICK START block to CLI help | `cli/main.py`, `server/exceptions.py`, `server/tools.py` | XS | CRITICAL | INDEPENDENT | NONE |
| T-02 | Add db info health-check warnings | `cli/commands/db.py`, `server/models.py`, `server/tools.py` | XS | HIGH | INDEPENDENT | T-09 |
| T-03 | Implement sqlcg uninstall command | `cli/commands/uninstall.py` (new), `cli/main.py` | S | HIGH | INDEPENDENT | NONE |
| T-04 | Fix atomicity: implement transaction() in both backends | `core/kuzu_backend.py`, `core/neo4j_backend.py`, `core/graph_db.py` | S | HIGH | INDEPENDENT | NONE |
| T-05 | Fix resolve_pass2 file re-open safety | `lineage/aggregator.py` | XS | HIGH | INDEPENDENT | NONE |
| T-06 | Fix _extract_column_lineage exception recording | `parsers/base.py` | XS | HIGH | INDEPENDENT | T-10 |
| T-07 | Add hint field to empty result models | `server/models.py`, `server/tools.py` | S | HIGH | INDEPENDENT | NONE |
| T-08 | Add index progress output and edges warning | `indexer/indexer.py`, `cli/commands/index.py` | S | HIGH | INDEPENDENT | T-09 |
| T-09 | Introduce parse_quality breakdown | `parsers/base.py`, `indexer/indexer.py`, `cli/commands/db.py`, `cli/commands/index.py` | M | MEDIUM | DEPENDS ON T-06 | NONE |
| T-10 | Verify and fix SELECTS_FROM edges for INSERT-SELECT | `indexer/indexer.py`, `parsers/base.py`, `parsers/ansi_parser.py` | M | HIGH | DEPENDS ON T-06 | NONE |
| T-11 | Rewrite scripting-block DML extraction | `parsers/snowflake_parser.py` | M | HIGH | DEPENDS ON T-06 | NONE |
| T-13 | Add FN label and execute_cypher ratio to gain | `server/tools.py`, `metrics/store.py`, `cli/commands/gain.py` | S | MEDIUM | INDEPENDENT | NONE |
| T-14 | Binary/package name note and uvx cold-start docs | `server/tools.py`, `cli/commands/mcp.py`, `README.md` | XS | LOW | INDEPENDENT | NONE |

---

## Ticket Specifications

---

### T-01 — Add QUICK START block to CLI help

**Source**: ARCHITECTURE_REVIEW.md 10.2.2 (rank 1, CRITICAL), 10.2.1 (rank 12, LOW — combined)

**What to do**:

1. In `cli/main.py`, change the `Typer(help=...)` string to include a `QUICK START` section
   at the top, listing the three required steps in numbered order:
   ```
   QUICK START:
     1. sqlcg db init
     2. sqlcg index <path> --dialect snowflake
     3. sqlcg git install-hooks
   Note: Binary is `sqlcg`; PyPI package is `sql-code-graph`.
   ```

2. In `server/exceptions.py`, update the `NotIndexedError` message to:
   `"No repos indexed. Run 'sqlcg db init' then 'sqlcg index <path>' first."`
   Confirm the message includes the binary name `sqlcg`, not the package name.

3. In `server/tools.py`, add a one-line note to the `index_repo` and `list_dialects_and_repos`
   docstrings: "Binary is `sqlcg`; PyPI package is `sql-code-graph`."

4. In `cli/commands/mcp.py`, add the same one-line binary/package note to the `setup`
   command output (print it after writing the JSON).

**Files affected**:
- `src/sqlcg/cli/main.py` — Typer help string
- `src/sqlcg/server/exceptions.py` — NotIndexedError message
- `src/sqlcg/server/tools.py` — `index_repo`, `list_dialects_and_repos` docstrings
- `src/sqlcg/cli/commands/mcp.py` — setup output

**Tests to add**:

Unit tests (pure string/output assertions, no graph):

- `tests/unit/test_cli_help.py`: invoke the Typer app with `["--help"]` using `typer.testing.CliRunner`; assert the captured output contains `"QUICK START"`, `"sqlcg db init"`, `"sqlcg index"`, and `"sqlcg git install-hooks"` as substrings; assert they appear in that order (index of each string increases monotonically).
- `tests/unit/test_exceptions.py`: instantiate `NotIndexedError` directly; assert `str(exc)` contains `"sqlcg db init"` and `"sqlcg index <path>"`.

Integration tests (no graph backend needed — CLI string output only):

- Fixture: none required (help text is static).
- Scenario: `CliRunner.invoke(app, ["--help"])` → assert exit code 0 and the three step numbers `"1."`, `"2."`, `"3."` appear in the output, confirming the ordered block is present and not truncated by Typer line-wrapping.

**Definition of done**:
- `sqlcg --help` displays the QUICK START block with all three steps in order
- `sqlcg --help` contains "Binary is `sqlcg`; PyPI package is `sql-code-graph`"
- `NotIndexedError` message names both commands with their correct syntax
- `index_repo` and `list_dialects_and_repos` tool docstrings name the binary

---

### T-02 — Add db info health-check warnings

**Source**: ARCHITECTURE_REVIEW.md 10.2.3 (rank 2, HIGH), 10.B (partially addressed here)

**What to do**:

1. In `cli/commands/db.py` `db_info()`, after printing node counts, add a structured health
   check section:
   - If `Repo == 0`: print red error "Database is empty. Run 'sqlcg db init' and 'sqlcg index <path>' first."
   - If `Repo > 0` and `SqlQuery == 0`: print yellow warning "No queries indexed. Run 'sqlcg index <path>' to populate the graph."
   - If `SqlQuery > 0` and `SqlColumn == 0`: print yellow warning "Column lineage not available. Tools trace_column_lineage, get_downstream_dependencies, and get_upstream_dependencies will return empty results."
   - After the warning block, print a `edges:` count field explicitly (query `MATCH ()-[r:COLUMN_LINEAGE]->() RETURN COUNT(r) AS count`).

2. In `server/models.py`, add `warnings: list[str]` field to `DialectRepoResult`:
   ```python
   warnings: list[str] = Field(default_factory=list, description="Health warnings about the indexed graph state")
   ```

3. In `server/tools.py` `list_dialects_and_repos()`, after building `repos`, append
   health-check warnings to `DialectRepoResult.warnings` using the same logic as the CLI:
   count `SqlColumn` nodes and append a diagnostic string if zero.

**Files affected**:
- `src/sqlcg/cli/commands/db.py` — `db_info()` health check section
- `src/sqlcg/server/models.py` — `DialectRepoResult.warnings`
- `src/sqlcg/server/tools.py` — `list_dialects_and_repos()` warnings population

**Tests to add**:

Unit tests (mock backend, no graph):

- `tests/unit/test_db_info.py`:
  - Scenario A — empty database: mock backend `run_read` to return `[{"label": "Repo", "count": 0}]` for all labels; invoke `db_info` via `CliRunner`; assert stdout contains `"Database is empty"` and the word `"error"` or red ANSI prefix.
  - Scenario B — no column lineage: mock backend returning `Repo=1`, `SqlQuery=10`, `SqlColumn=0`; invoke `db_info`; assert stdout contains `"Column lineage not available"` and names `trace_column_lineage` in the warning.
  - Scenario C — healthy: mock backend returning `Repo=1`, `SqlQuery=10`, `SqlColumn=50`; invoke `db_info`; assert no warning lines.
- `tests/unit/test_tools.py`: mock backend with `SqlColumn` count query returning `[{"count": 0}]`; call `list_dialects_and_repos()`; assert `result.warnings` is a non-empty list and the first item contains `"SqlColumn"`.

Integration tests (real KùzuDB in-memory, real SQL fixture → Indexer → graph → tool call):

- Fixture: `tests/fixtures/snowflake/base_tables.sql` (already exists — creates `customers`, `orders`, `products`, `order_items`).
- Scenario — db_info after indexing DDL-only corpus: index `base_tables.sql` using `Indexer.index_repo()` into an in-memory KùzuDB; call `db.run_read("MATCH (n:SqlColumn) RETURN COUNT(n) AS count", {})` and assert it returns `[{"count": 0}]`; then call the `db info` CLI command and assert the yellow column-lineage warning appears in captured output.
- Assertion detail: `result.output` must contain `"Column lineage not available"` exactly once; must NOT contain `"Database is empty"` (Repo node was created).

**Definition of done**:
- `sqlcg db info` on an empty database prints a red "Database is empty" message
- `sqlcg db info` on an indexed-but-no-column-lineage graph prints the yellow column warning
- `sqlcg db info` always prints an `edges:` count line
- `list_dialects_and_repos` MCP tool populates `warnings` when column lineage is absent

---

### T-03 — Implement sqlcg uninstall command

**Source**: ARCHITECTURE_REVIEW.md 10.1, 10.A, 10.6 Q1 (rank 3, HIGH)

**What to do**:

Create `src/sqlcg/cli/commands/uninstall.py` implementing `uninstall_cmd` with this exact
three-step interaction contract (resolved in section 10.6 Q1):

Step 1 — always remove `mcpServers["sql-code-graph"]` from `~/.claude/settings.json`:
- Read the file atomically (same JSON load as `install.py`)
- Pop the `sql-code-graph` key from `mcpServers`
- Write back via `.tmp` + `os.replace` (same pattern as `install.py`)
- If key was not present, print "MCP entry not found — already removed"
- Print "Removed MCP registration from ~/.claude/settings.json"

Step 2 — offer to delete the local KùzuDB:
- Only if `SQLCG_BACKEND` is `kuzu` (or unset, defaulting to kuzu) AND the backend is NOT Neo4j
- If `--keep-db` flag: skip this step entirely and print "Keeping database at <path>"
- If `--force` flag: delete without prompting
- Otherwise: print prompt "This will delete the graph database at <path>. Continue? [y/N]"
  and wait for stdin input (use `typer.confirm` or read one line)
- Deletion: `shutil.rmtree(db_path, ignore_errors=True)` for the graph DB directory
- If `--force`: also delete `~/.sqlcg/metrics.db` (or the metrics file alongside the graph DB)
- Print "Deleted graph database at <path>"

Step 3 — remove the git hook sentinel block:
- Target: `.git/hooks/post-checkout` in `Path.cwd()` (or `--repo <path>` if provided)
- Read the file if it exists; strip the `# sqlcg post-checkout hook` sentinel block
  (from that sentinel comment through the next blank line or end-of-block marker)
- If the hook file becomes empty after stripping, delete it
- If the hook file does not exist, print "No git hook found in <path>"
- Print "Removed git hook from <path>/.git/hooks/post-checkout"

Options:
- `--keep-db`: skip DB deletion (step 2)
- `--force`: skip DB deletion prompt; also delete metrics store
- `--repo <path>`: target directory for git hook removal (default: cwd)

Register in `cli/main.py`:
```python
app.command("uninstall")(uninstall.uninstall_cmd)
```

**Files affected**:
- `src/sqlcg/cli/commands/uninstall.py` (new file)
- `src/sqlcg/cli/main.py` — add import and register

**Tests to add**:

Unit tests (all use `tmp_path`, no side effects on `~/.claude/` or `~/.sqlcg/`):

- `tests/unit/test_uninstall.py`:
  - Scenario A — MCP entry removal: write a `tmp_path / "settings.json"` containing `{"mcpServers": {"sql-code-graph": {"command": "sqlcg"}}}`, patch `SETTINGS_PATH` constant to point at that file, invoke `uninstall_cmd` via `CliRunner`; assert the resulting JSON file does not contain the `"sql-code-graph"` key and the remaining JSON is valid; assert stdout contains `"Removed MCP registration"`.
  - Scenario B — MCP entry already absent: write settings.json with `{"mcpServers": {}}`, invoke `uninstall_cmd`; assert exit code 0 and stdout contains `"already removed"`.
  - Scenario C — `--keep-db` flag: create a `tmp_path / "db"` directory, set `SQLCG_DB_PATH` env var to that path, invoke `uninstall_cmd --keep-db`; assert the directory still exists after the call.
  - Scenario D — `--force` flag: create `tmp_path / "db"` directory and `tmp_path / "metrics.db"` file; invoke `uninstall_cmd --force`; assert neither path exists after the call and no prompt was shown (confirm by asserting no `"Continue?"` in stdout).
  - Scenario E — git hook stripping: write `tmp_path / ".git" / "hooks" / "post-checkout"` containing the sentinel block followed by other content; invoke `uninstall_cmd --repo tmp_path`; assert the sentinel block is absent in the file and the remaining non-sentinel content is preserved.
  - Scenario F — git hook file becomes empty after stripping: write the hook file containing only the sentinel block; invoke `uninstall_cmd --repo tmp_path`; assert the hook file no longer exists.
  - Scenario G — git hook file absent: invoke `uninstall_cmd --repo tmp_path` where `.git/hooks/post-checkout` does not exist; assert exit code 0 and stdout contains `"No git hook found"`.

Integration test (no graph backend needed — filesystem side effects only):

- Not required for T-03. All scenarios are pure filesystem operations; the unit tests above with `tmp_path` constitute full integration coverage for this ticket. No KùzuDB is involved.

**Definition of done**:
- `sqlcg uninstall` removes the MCP registration, prompts for DB deletion, removes git hook
- `sqlcg uninstall --keep-db` skips DB deletion
- `sqlcg uninstall --force` deletes DB and metrics store without prompting
- Each step prints a confirmation line
- All 10+ unit tests pass with `tmp_path` fixtures

---

### T-04 — Fix atomicity: implement transaction() in both backends

**Source**: ARCHITECTURE_REVIEW.md finding 3.1 (rank 1 in section 4, HIGH), 8.2 comment 4

**What to do**:

The current `GraphBackend.transaction()` base method in `core/graph_db.py` yields `self` and
does nothing. Both concrete backends must implement real transactions.

1. In `core/graph_db.py`, update the `transaction()` abstract method docstring to require
   subclass override. Consider marking it `@abstractmethod` with a `contextmanager` that
   subclasses must wrap.

2. In `core/kuzu_backend.py`, implement `transaction()` using KùzuDB's connection-level API:
   ```python
   @contextmanager
   def transaction(self):
       self._conn.begin()
       try:
           yield self
           self._conn.commit()
       except Exception:
           self._conn.rollback()
           raise
   ```
   Verify the exact API method names against the installed `kuzu` package version.

3. In `core/neo4j_backend.py`, implement `transaction()` using Neo4j's explicit transaction:
   ```python
   @contextmanager
   def transaction(self):
       with self._driver.session() as session:
           with session.begin_transaction() as tx:
               try:
                   yield self
                   tx.commit()
               except Exception:
                   tx.rollback()
                   raise
   ```

4. In `core/kuzu_backend.py` `init_schema()`, wrap the entire DDL execution loop in a
   transaction (finding 8.2 comment 4 — schema init should also be atomic).

**Files affected**:
- `src/sqlcg/core/graph_db.py` — `transaction()` docstring/contract
- `src/sqlcg/core/kuzu_backend.py` — `transaction()` implementation, `init_schema()` wrap
- `src/sqlcg/core/neo4j_backend.py` — `transaction()` implementation

**Tests to add**:

Unit tests (KùzuDB in-memory — no real SQL files needed):

- `tests/unit/test_kuzu_backend.py`:
  - Scenario — rollback on upsert failure: open an in-memory `KuzuBackend`; call `init_schema()`; upsert one `File` node and one `SqlTable` node via `upsert_node()`; record node count via `run_read("MATCH (n) RETURN COUNT(n) AS count", {})`; open a `db.transaction()` context manager, call `upsert_node()` to add a second `SqlTable`, then raise `RuntimeError` inside the context; assert that after the exception, node count equals the pre-transaction value (the second `SqlTable` was rolled back).
  - Scenario — commit on success: open a transaction, upsert one node, exit cleanly (no exception); assert node count increased by 1.

Integration tests (real SQL fixture → Indexer → KùzuDB → injected failure → graph state assertion):

- `tests/integration/test_indexer_to_graph.py` (extend existing file):
  - Scenario — mid-index failure leaves graph consistent: index `tests/fixtures/snowflake/base_tables.sql` into an in-memory KùzuDB to establish a baseline; record node count `N_before`; patch `KuzuBackend.upsert_node` to raise `RuntimeError` on the second call; attempt `indexer.reindex_file(base_tables_path, db, dialect=None)` inside a `try/except`; assert `db.run_read("MATCH (n) RETURN COUNT(n) AS count", {})` returns `[{"count": N_before}]` — no orphaned nodes from the partial re-index.
  - Assert exact count: `assert result[0]["count"] == N_before` (not `>= 0` — the count must not change).

**Definition of done**:
- `KuzuBackend.transaction()` and `Neo4jBackend.transaction()` are real transaction wrappers
- A failed re-index (simulated exception) leaves the graph with the pre-existing state
- `init_schema()` on KuzuBackend is wrapped in a transaction

---

### T-05 — Fix resolve_pass2 file re-open safety

**Source**: ARCHITECTURE_REVIEW.md finding 3.2 (rank 3 in section 4, HIGH)

**What to do**:

In `lineage/aggregator.py`, the `resolve_pass2` method re-reads the file from disk. If the
file has been deleted, moved, or is unreadable between pass 1 and pass 2, it raises an
unhandled `FileNotFoundError`.

Wrap the `open()` call in `try/except (FileNotFoundError, OSError)` and return the pass-1
`ParsedFile` unchanged on failure:

```python
def resolve_pass2(self, parser, parsed: ParsedFile) -> ParsedFile:
    parser.schema_resolver.add_view_sources(self.sources)
    try:
        sql = Path(parsed.path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        logger.warning(
            "resolve_pass2: cannot re-read %s (%s) — returning pass-1 result",
            parsed.path,
            exc,
        )
        return parsed
    return parser.parse_file(parsed.path, sql)
```

**Files affected**:
- `src/sqlcg/lineage/aggregator.py` — `resolve_pass2()`

**Tests to add**:

Unit test (existing `test_cross_file_lineage.py` already covers this scenario — extend, do not duplicate):

- `tests/unit/test_aggregator.py`:
  - Scenario — deleted file during pass 2: write a temp file `tmp_path / "source.sql"` containing `CREATE TABLE raw_orders (id INT, amount DECIMAL);`; parse it with `get_parser(None, SchemaResolver())` and `parse_file()` to get `pass1_result`; register with `CrossFileAggregator.register_pass1(pass1_result)`; delete `tmp_path / "source.sql"` via `Path.unlink()`; call `aggregator.resolve_pass2(parser, pass1_result)` and capture the return value `result`; assert `result is pass1_result` (same object, not a new parse); assert `caplog` contains a WARNING entry with `"resolve_pass2"` and `"cannot re-read"` in the message.
  - Assertion must use `caplog` at level `logging.WARNING`, not just check return value — confirming the log path is exercised.

Integration test (real indexer path, volatile file — no graph backend needed):

- This scenario is already covered by `tests/integration/test_cross_file_lineage.py::test_resolve_pass2_deleted_file`. No new integration test needed for T-05. The existing test uses `CrossFileAggregator` + real `parse_file()` + `Path.unlink()` — identical to the spec above.

**Definition of done**:
- A deleted or unreadable file during pass 2 logs a warning and returns the pass-1 result
- No `FileNotFoundError` propagates to the caller
- Unit test confirms the warning is emitted

---

### T-06 — Fix _extract_column_lineage exception recording

**Source**: ARCHITECTURE_REVIEW.md finding 3.4 (rank 2 in section 4, HIGH), finding 10.C (amplification)

**What to do**:

The current `_extract_column_lineage` in `parsers/base.py` already logs warnings and appends
to `ParsedFile.errors` (the architecture review found this to already be partially implemented
in the code). However, the fix is incomplete: inspect the current implementation carefully and
verify that:

1. The `except Exception as exc` block around `sg_lineage()` call:
   - Logs at WARNING with file path, column name, and exception
   - Appends to `out.errors` as `f"col_lineage:{col_name}:{exc}"`
   - Emits a zero-confidence `LineageEdge` placeholder

2. The outer `except Exception` block (entire statement failure):
   - Logs at WARNING
   - Appends to `out.errors` as `f"col_lineage:statement:{exc}"`

3. The `# TODO: convert root to LineageEdge(s)` placeholder in the `SELECT` body path
   is a known limitation (lineage conversion not yet implemented); add a `logger.debug`
   when `root` is returned from `sg_lineage()` but is not yet converted.

These are the only changes for this ticket. The full lineage-to-edge conversion is NOT in
scope for this ticket (it is a separate feature requiring deeper design work).

**Also**: the `parse_quality` breakdown (T-09) depends on T-06 being complete first because
T-09 uses the error list populated by T-06 to determine quality categories.

**Files affected**:
- `src/sqlcg/parsers/base.py` — `_extract_column_lineage()` — verify all three points above

**Tests to add**:

Unit tests (pure parser — no graph backend):

- `tests/unit/test_base_parser.py`:
  - Scenario — sg_lineage exception recorded: construct a minimal `exp.Select` AST node (via `sqlglot.parse_one("SELECT bad_col FROM t")`); patch `sqlglot.lineage.lineage` (the `sg_lineage` function imported inside `_extract_column_lineage`) to raise `ValueError("mock lineage failure")`; call `_extract_column_lineage(out, stmt, schema=None, col_name="bad_col")` directly on an `AnsiParser` instance; assert `out.errors` contains an entry matching `"col_lineage:bad_col:mock lineage failure"`; assert `caplog` contains a WARNING message with `"bad_col"` and `"mock lineage failure"`; assert the returned edge list contains exactly one `LineageEdge` with `confidence == 0.0`.
  - Assertion on the zero-confidence edge: `assert edges[0].confidence == 0.0` — not `edges[0].confidence < 0.1`. Must be exactly zero.
  - Scenario — outer statement exception recorded: patch `_extract_column_lineage` to raise during the outer try block (e.g. by passing a non-`exp.Select` node that causes a TypeError); assert `out.errors` contains an entry matching `"col_lineage:statement:"`.

Integration test: none required for T-06. Exception recording is a pure parser behaviour; correctness is fully observable from `ParsedFile.errors` without a graph backend.

**Definition of done**:
- Column lineage failures append to `ParsedFile.errors` with a structured key
- A zero-confidence edge is returned (not silent skip)
- The WARNING log includes file path, column name, and exception text
- T-09 is unblocked

---

### T-07 — Add hint field to empty result models

**Source**: ARCHITECTURE_REVIEW.md finding 10.B (rank 5, HIGH)

**What to do**:

1. In `server/models.py`, add `hint: str | None = None` field to:
   - `LineageResult`
   - `DependencyResult`
   - `TableUsageResult`
   - `SqlPatternResult`

   Field description: `"Diagnostic hint when result list is empty. Explains the likely cause and suggests a next step."`

2. In `server/tools.py`, populate `hint` when the result list (`lineage`, `nodes`,
   `usages`, `matches`) is empty. Use these exact diagnostic strings:

   For `trace_column_lineage` and `get_upstream_dependencies` / `get_downstream_dependencies`
   when `lineage` / `nodes` is empty:
   ```
   "No lineage found. Check that 'sqlcg db info' shows SqlColumn > 0. If SqlColumn is 0, column lineage was not extracted — check parse errors. Submit feedback with submit_feedback tool if this was a false negative."
   ```

   For `find_table_usages` when `usages` is empty:
   ```
   "No usages found for this table. The table may not be referenced by any indexed SQL file, or it may be consumed externally (BI tools, APIs). Run 'analyze impact <table>' from the CLI to cross-check."
   ```

   For `search_sql_pattern` when `matches` is empty:
   ```
   "No matches found. Try a shorter or partial pattern. Pattern matching is case-sensitive substring search."
   ```

**Files affected**:
- `src/sqlcg/server/models.py` — add `hint` field to four models
- `src/sqlcg/server/tools.py` — populate `hint` in four tools on empty result

**Tests to add**:

Unit tests (mock backend — no graph):

- `tests/unit/test_tools_hints.py`:
  - Scenario A — `trace_column_lineage` empty result: patch `KuzuBackend.run_read` to return `[]`; call `trace_column_lineage("orders.amount")`; assert `result.hint is not None`; assert `"SqlColumn"` in `result.hint`; assert `result.lineage == []`.
  - Scenario B — `trace_column_lineage` non-empty result: patch `run_read` to return one row `{"id": "raw_orders.amount", "col_name": "amount"}`; assert `result.hint is None`.
  - Scenario C — `find_table_usages` empty: patch `run_read` to return `[]`; assert `result.hint` contains `"BI tools"` or `"analyze impact"`.
  - Scenario D — `search_sql_pattern` empty: patch `run_read` to return `[]`; assert `result.hint` contains `"case-sensitive"`.
  - Each scenario must assert the exact presence/absence of `hint` — not just `result.hint is not None` but that the expected diagnostic keyword from the spec is in the string.

Integration test (real KùzuDB + real indexer — hint field appears on genuinely empty graph):

- Scenario: create an in-memory KùzuDB; call `init_schema()` without indexing any files; call `trace_column_lineage("orders.amount")` against the real backend (bypassing the `_assert_indexed` guard by inserting at least one `Repo` node manually via `db.upsert_node("Repo", {"path": "/tmp/fake", "name": "fake"})`); assert `result.lineage == []` and `result.hint` is not None and contains `"SqlColumn"`.
- This confirms hint is populated from a real empty-graph state, not a mocked one.

**Definition of done**:
- `LineageResult`, `DependencyResult`, `TableUsageResult`, `SqlPatternResult` all have `hint: str | None`
- When the result list is empty, `hint` is populated with a diagnostic string
- When the result list is non-empty, `hint` is `None`
- Unit tests verify hint content for each tool

---

### T-08 — Add index progress output and edges warning

**Source**: ARCHITECTURE_REVIEW.md 10.2.9 (rank 4, HIGH)

**What to do**:

1. In `indexer/indexer.py` `index_repo()`, add periodic progress output. After every 100
   files parsed in the pass-1 loop, call a progress callback or emit a structured log event.
   The implementation should accept an optional `progress_callback: Callable[[int, int], None] | None`
   parameter (current_count, total_count) so tests can inject a mock without touching
   stdout.

2. In `cli/commands/index.py` `index_cmd()`, inject a progress callback that calls
   `console.print(f"\r  Indexed {n}/{total}...", end="")` using `rich`. Use carriage return
   to overwrite the same line. After the loop completes, print a newline to clear the
   carriage return.

3. After `indexer.index_repo()` returns, in `index_cmd()`, if `summary['lineage_edges_created'] == 0`:
   print a yellow warning to stdout:
   "Warning: 0 lineage edges created. Column-level lineage tracing will not be available. Check parse errors above."

4. In `cli/commands/db.py` `db_info()`, add an `edges` count line:
   - Query: `MATCH ()-[r:COLUMN_LINEAGE]->() RETURN COUNT(r) AS count`
   - Print: `  COLUMN_LINEAGE edges: {count}`
   - This is additive to the health warnings from T-02.

**Files affected**:
- `src/sqlcg/indexer/indexer.py` — `index_repo()` progress callback parameter
- `src/sqlcg/cli/commands/index.py` — progress output, edges=0 warning
- `src/sqlcg/cli/commands/db.py` — `COLUMN_LINEAGE edges` count line

**Tests to add**:

Unit tests (mock callback, no real graph):

- `tests/unit/test_indexer_progress.py`:
  - Scenario — callback invoked at 100-file boundary: create 105 tiny `.sql` files in `tmp_path` (each containing a single `SELECT 1`); index them into an in-memory KùzuDB with a `progress_callback` that records all `(current, total)` tuples; assert the callback was called at least once with `current == 100` (or the next boundary below total if fewer than 200 files).
  - Assertion: `assert any(c == 100 for c, t in calls)` where `calls` is the list of `(current, total)` tuples passed to the callback.
- `tests/unit/test_index_cmd.py`:
  - Scenario — zero edges warning on stdout: patch `Indexer.index_repo` to return `{"files_parsed": 5, "parse_errors": 0, "tables_found": 3, "lineage_edges_created": 0}`; invoke `index_cmd` via `CliRunner` with a `tmp_path` argument; assert `"0 lineage edges"` or `"Warning"` appears in `result.output`; assert exit code is 0 (it is a warning, not an error).
  - Scenario — no warning when edges > 0: patch `index_repo` to return `lineage_edges_created=5`; assert `"Warning"` does NOT appear in output related to lineage edges.

Integration test (real indexer → real KùzuDB → CLI output):

- Scenario: index `tests/fixtures/snowflake/base_tables.sql` (DDL only, no DML → zero lineage edges); capture CLI output; assert `result.output` contains `"0 lineage edges"` warning and `"COLUMN_LINEAGE edges: 0"` in the `db info` output after re-running `db info`. These two assertions span both `index_cmd` and `db_info` and confirm the warning propagates through the full stack.
- Assertion is on exact substring: `assert "lineage_edges_created" not in result.output` (the raw key must not leak); `assert "0 lineage edges" in result.output` (the rendered warning must appear).

**Definition of done**:
- `sqlcg index` on 1457 files prints progress every 100 files
- A zero-edges result prints a yellow warning to stdout (not just the log)
- `sqlcg db info` shows the `COLUMN_LINEAGE edges` count

---

### T-09 — Introduce parse_quality breakdown

**Source**: ARCHITECTURE_REVIEW.md finding 10.C (rank 11, MEDIUM)

**Depends on**: T-06 (must be complete so `ParsedFile.errors` is reliably populated)

**What to do**:

1. In `parsers/base.py`, add a `ParseQuality` `StrEnum`:
   ```python
   class ParseQuality(StrEnum):
       FULL = "full"           # sqlglot parsed fully, column lineage extracted
       TABLE_ONLY = "table_only"  # tables extracted, column lineage not available
       SCRIPTING_FALLBACK = "scripting_fallback"  # regex extraction, confidence 0.3
       FAILED = "failed"       # unhandled exception, no lineage
   ```

2. In `parsers/base.py`, add `parse_quality: ParseQuality = ParseQuality.TABLE_ONLY` field
   to `ParsedFile`. The default is `TABLE_ONLY` (most common case — tables extracted but
   no column lineage yet because the sg_lineage→edge conversion is incomplete).

3. In `parsers/base.py` `SqlParser`, where a file parses successfully with column lineage
   edges, set `parse_quality = ParseQuality.FULL`. In `snowflake_parser.py`
   `_parse_scripting_file`, set `parse_quality = ParseQuality.SCRIPTING_FALLBACK`. In the
   timeout/exception handler in `indexer.py`, set `parse_quality = ParseQuality.FAILED`.

4. In `indexer/indexer.py` `index_repo()`, collect quality counts. Return them in the
   summary dict:
   ```python
   {
       "files_parsed": ...,
       "parse_errors": ...,
       "tables_found": ...,
       "lineage_edges_created": ...,
       "quality": {
           "full": N,
           "table_only": N,
           "scripting_fallback": N,
           "failed": N,
       }
   }
   ```

5. In `cli/commands/index.py` `index_cmd()`, print the quality breakdown after the summary
   line (unless `--quiet`):
   ```
   Parse quality: full=N table_only=N scripting_fallback=N failed=N
   ```

6. In `cli/commands/db.py` `db_info()`, add a query to count `SqlQuery` nodes grouped by
   `parsing_mode` property. Display the breakdown.

**Files affected**:
- `src/sqlcg/parsers/base.py` — `ParseQuality` enum, `ParsedFile.parse_quality` field
- `src/sqlcg/parsers/snowflake_parser.py` — set `SCRIPTING_FALLBACK`
- `src/sqlcg/indexer/indexer.py` — collect quality counts, include in return dict
- `src/sqlcg/cli/commands/index.py` — print quality breakdown
- `src/sqlcg/cli/commands/db.py` — query and display parsing_mode distribution

**Tests to add**:

Unit tests (parser layer — no graph):

- `tests/unit/test_parse_quality.py`:
  - Scenario — scripting fallback: read `tests/benchmarks/golden_corpus/snowflake/scripting_block.sql`; parse it with `SnowflakeParser`; assert `parsed.parse_quality == ParseQuality.SCRIPTING_FALLBACK`.
  - Scenario — failed parse: construct a `ParsedFile` that has a simulated unhandled exception recorded; assert the quality is set to `ParseQuality.FAILED` (simulate by calling `Indexer._handle_parse_failure()` or setting the field directly in a test double).
  - Scenario — table_only default: parse `tests/fixtures/snowflake/base_tables.sql` (pure DDL); assert `parsed.parse_quality == ParseQuality.TABLE_ONLY` (no column lineage extracted from DDL-only).

Integration tests (real SQL fixture → Indexer → summary dict → quality breakdown):

- `tests/unit/test_indexer_quality.py` (named unit but actually integration — exercises Indexer + KùzuDB):
  - Fixture contents: use `tests/fixtures/snowflake/` which has `base_tables.sql` (DDL → TABLE_ONLY), `views.sql` (CREATE VIEW → TABLE_ONLY), `reports.sql` (SELECT → TABLE_ONLY or FULL if lineage extracted).
  - Add `tests/fixtures/snowflake/scripting_sample.sql` (new fixture, content below) to introduce a SCRIPTING_FALLBACK file.
  - Fixture `tests/fixtures/snowflake/scripting_sample.sql`:
    ```sql
    -- Snowflake scripting block sample for parse_quality testing
    BEGIN
      INSERT INTO dwh.target SELECT id, amount FROM raw.source;
      CALL my_proc();
    END;
    ```
  - Index the full `tests/fixtures/snowflake/` directory with dialect `"snowflake"` into an in-memory KùzuDB; assert `summary["quality"]["scripting_fallback"] >= 1` (the new scripting file); assert `summary["quality"]["full"] + summary["quality"]["table_only"] + summary["quality"]["scripting_fallback"] + summary["quality"]["failed"] == summary["files_parsed"]` (quality counts sum to total files); assert `summary["quality"]` key exists in the return dict.
  - Exact assertion: `assert sum(summary["quality"].values()) == summary["files_parsed"]` — this is the golden rule for the quality breakdown.

**Definition of done**:
- `ParseQuality` enum exists in `parsers/base.py`
- `ParsedFile` carries `parse_quality`
- `sqlcg index` prints the four-category quality breakdown
- `sqlcg db info` shows the `parsing_mode` distribution from the graph
- The index summary dict includes the `quality` key

---

### T-10 — Verify and fix SELECTS_FROM edges for INSERT-SELECT

**Source**: ARCHITECTURE_REVIEW.md 10.2.5, 10.6 Q4 (rank 10, elevated to HIGH — confirmed parser bug)

**Depends on**: T-06 (for clean error visibility during investigation)

**What to do**:

This ticket requires investigation before implementation. The symptom: `analyze impact`
returns only DDL files for a table while `find pattern` finds ETL INSERT statements for the
same table. The bug is suspected to be in `_upsert_parsed_file` not creating `SELECTS_FROM`
edges for INSERT statements, or in the parser not extracting `sources` for INSERT statements.

**Investigation steps** (developer must complete before coding):

1. Index a test fixture containing:
   ```sql
   -- tests/fixtures/snowflake/insert_select_impact.sql
   INSERT INTO dwh.target_table
   SELECT t.col_a, c.col_b
   FROM source_schema.source_table t
   JOIN ref_schema.dim_customers c ON t.id = c.id;
   ```

2. Run `execute_cypher("MATCH (q:SqlQuery)-[:SELECTS_FROM]->(t:SqlTable) RETURN q.id, t.qualified LIMIT 20")`.
   If `source_schema.source_table` appears as a target of `SELECTS_FROM`, the edges are being
   created and the bug is elsewhere (likely query routing in `analyze impact`). If it does not
   appear, the bug is in the parser or `_upsert_parsed_file`.

3. Check `parsers/ansi_parser.py` `_parse_statement()` for INSERT handling: verify that
   `stmt.sources` is populated for `exp.Insert` nodes. sqlglot represents `INSERT INTO t SELECT ...`
   as `exp.Insert` with the SELECT as the body; the sources in the SELECT body must be extracted
   by the scope walker.

**Implementation** (after investigation identifies root cause):

- If the bug is in `_parse_statement`: fix the INSERT source extraction in `ansi_parser.py`.
- If the bug is in `_upsert_parsed_file`: verify the `for src_table in stmt.sources` loop
  also runs for INSERT nodes (it should — the code is dialect-agnostic at that layer).
- If the bug is in the Cypher query in `analyze.py` `impact()`: verify that `SELECTS_FROM`
  is the correct relationship for INSERT-SELECT (it is — `SELECTS_FROM` means "this query
  reads from this table", not "this query selects from").

**Mandatory deliverable**: the test fixture `tests/fixtures/snowflake/insert_select_impact.sql`
and a test that calls `analyze impact` on the indexed fixture and asserts the INSERT file
appears in results.

**Files affected** (expected, pending investigation):
- `src/sqlcg/parsers/ansi_parser.py` — INSERT source extraction
- `tests/fixtures/snowflake/insert_select_impact.sql` (new)
- `tests/unit/test_insert_select_impact.py` (new)

**Tests to add**:

New SQL fixtures required:

1. `tests/fixtures/snowflake/insert_select_impact.sql` (exact content):
   ```sql
   -- Fixture: INSERT-SELECT for SELECTS_FROM edge verification (T-10)
   INSERT INTO dwh.target_table
   SELECT t.col_a, c.col_b
   FROM source_schema.source_table t
   JOIN ref_schema.dim_customers c ON t.id = c.id;
   ```

2. `tests/fixtures/snowflake/etl_chain.sql` (exact content):
   ```sql
   -- Fixture: intra-file multi-step ETL chain (T-10)
   CREATE TABLE stage_orders AS
   SELECT id, customer_id, amount
   FROM raw.orders;

   INSERT INTO dwh.orders
   SELECT o.id, o.customer_id, c.name, o.amount
   FROM stage_orders o
   JOIN raw.customers c ON o.customer_id = c.id;
   ```

Column lineage SQL pattern matrix (all patterns must be covered by fixtures in T-10):

| Pattern | Fixture file | Expected SELECTS_FROM source table | Expected edge count |
|---------|-------------|-------------------------------------|---------------------|
| Simple INSERT-SELECT | `tests/fixtures/snowflake/insert_select_impact.sql` | `source_schema.source_table` | >= 1 SELECTS_FROM edge |
| Multi-step intra-file CTAS + INSERT | `tests/fixtures/snowflake/etl_chain.sql` | `raw.orders` and `raw.customers` | >= 2 SELECTS_FROM edges |

Integration tests (full stack: fixture → Indexer → real KùzuDB → Cypher query):

- `tests/integration/test_indexer_to_graph.py` (extend):

  **Scenario 1 — INSERT-SELECT emits SELECTS_FROM edge**:
  - Index only `tests/fixtures/snowflake/insert_select_impact.sql` into in-memory KùzuDB.
  - Run: `db.run_read("MATCH (q:SqlQuery)-[:SELECTS_FROM]->(t:SqlTable) WHERE t.qualified CONTAINS 'source_table' RETURN q.id AS qid, t.qualified AS tbl", {})`.
  - Assert `len(rows) >= 1`.
  - Assert `rows[0]["tbl"]` contains `"source_table"` (case-insensitive tolerated via `lower()`).
  - Assert `len(rows) <= 2` — at most one SELECTS_FROM edge per source table per query (no duplicates from JOIN expansion).
  - **Golden rule**: exact edge count assertion: `assert len(rows) == 2` (source_table + dim_customers — both JOIN sources must produce edges).

  **Scenario 2 — CTAS produces SELECTS_FROM edge to its source**:
  - Index only `tests/fixtures/snowflake/etl_chain.sql`.
  - Run: `db.run_read("MATCH (q:SqlQuery)-[:SELECTS_FROM]->(t:SqlTable) RETURN t.qualified AS tbl ORDER BY tbl", {})`.
  - Assert result contains a row with `tbl` matching `"raw.orders"` (or `"orders"` depending on qualification).
  - Assert result contains a row with `tbl` matching `"raw.customers"`.
  - Assert `len(rows) == 3`: CTAS reads `raw.orders`; INSERT reads `stage_orders` and `raw.customers`. Total = 3 distinct SELECTS_FROM edges.
  - **Golden rule**: `assert len(rows) == 3` — not `>= 2`. Spurious extra edges must not be present.

  **Scenario 3 — analyze impact finds the INSERT file**:
  - Index `tests/fixtures/snowflake/insert_select_impact.sql`.
  - Run: `db.run_read("MATCH (q:SqlQuery)-[:SELECTS_FROM]->(t:SqlTable) WHERE t.qualified CONTAINS 'source_table' RETURN q.id", {})`.
  - Assert `len(rows) >= 1`. This is the graph-level equivalent of `analyze impact source_schema.source_table` returning the INSERT file.

  **Scenario 4 — find_table_usages and analyze impact consistency**:
  - Index `tests/fixtures/snowflake/insert_select_impact.sql`.
  - Run `FIND_TABLE_USAGES_QUERY` with `name="source_table"`.
  - Assert `len(usages) >= 1` and `usages[0]["kind"] == "INSERT"`.
  - This confirms `find_table_usages` and `analyze impact` return consistent results for the same table.

**Definition of done**:
- `analyze impact <table>` returns the INSERT file when the table is a source in an INSERT-SELECT
- `find pattern <table>` and `analyze impact <table>` return consistent (overlapping) results for the same table
- `tests/fixtures/snowflake/insert_select_impact.sql` exists with the fixture SQL
- Tests pass

---

### T-11 — Rewrite scripting-block DML extraction

**Source**: ARCHITECTURE_REVIEW.md 10.2.7 (rank 9, HIGH, M effort), 3.12 (SUPERSEDED — fixed by this ticket)

**Depends on**: T-06 (for clean error recording during the rewrite)

**What to do**:

Replace the `_EMBEDDED_DML` regex in `SnowflakeParser._parse_scripting_file` with a two-step
sqlglot-native approach. The `_has_scripting_block()` method remains unchanged.

**Step 1 — Tokenizer-based statement splitting**:

```python
def _split_statements(self, sql: str) -> list[str]:
    """Split SQL into statement strings using the sqlglot tokenizer.
    
    Handles semicolons inside string literals correctly — no regex needed.
    """
    from sqlglot.tokens import Tokenizer, TokenType
    toks = Tokenizer.from_dialect(self.DIALECT).tokenize(sql)
    chunks = []
    current = []
    for tok in toks:
        if tok.token_type == TokenType.SEMICOLON:
            chunk = sql[current[0].start:tok.start].strip() if current else ""
            if chunk:
                chunks.append(chunk)
            current = []
        else:
            current.append(tok)
    # Handle final statement without trailing semicolon
    if current:
        chunk = sql[current[0].start:current[-1].end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks
```

Note: use `tok.start` and `tok.end` — check the exact attribute names on the installed
`sqlglot.tokens.Token` class (may be `start`/`end` or `col`/`line`). An alternative is to
collect tokens by position and reassemble from the raw SQL text. Verify against installed version.

**Step 2 — DML classification filter**:

After splitting, parse each chunk and keep only DML/SELECT:

```python
import sqlglot.expressions as exp

def _is_indexable(stmt) -> bool:
    if isinstance(stmt, (exp.Select,)):
        return True
    if isinstance(stmt, exp.DML) and not isinstance(stmt, exp.Copy):
        return True
    if isinstance(stmt, exp.Create):
        # Pass CTAS through — _parse_statement handles it
        return True
    return False
```

Drop `exp.Command`, `exp.DDL` (ALTER, SET, LET, etc.) at DEBUG level, not WARNING.

**Step 3 — Snowflake/Databricks procedure body extraction**:

After the tokenizer split, scan for `exp.Create` nodes that contain an `exp.RawString` body
(the `$$...$$` delimiter). Extract the body string, strip the outer `BEGIN` / `END` wrapper,
then recursively apply the tokenizer split + DML filter to the inner content.

```python
def _extract_procedure_body(self, stmt: exp.Create) -> str | None:
    """Extract the SQL body from a Snowflake/Databricks procedure."""
    body = stmt.find(exp.RawString)
    if body is None:
        return None
    text = body.this  # The raw string content
    # Strip leading/trailing BEGIN...END wrapper
    text = re.sub(r"^\s*BEGIN\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*END\s*$", "", text, flags=re.IGNORECASE)
    return text.strip()
```

**Step 4 — BigQuery fallback**:

For BigQuery (detected when `self.DIALECT == "bigquery"`), `exp.Create` bodies land as
`exp.Command`. Keep the existing `_EMBEDDED_DML` regex as the fallback path, called only for
`exp.Command` nodes with a BigQuery dialect. Do NOT remove the `_EMBEDDED_DML` regex from the
module entirely — it must remain for the BigQuery case.

**Step 5 — Remove the top-level DML regex path**:

The primary path in `_parse_scripting_file` changes from:
```python
dml_matches = _EMBEDDED_DML.finditer(sql)
for match in dml_matches: ...
```
to the tokenizer split + parse + DML filter approach.

Rename `_parse_scripting_file` internals; the public signature stays the same.

**Files affected**:
- `src/sqlcg/parsers/snowflake_parser.py` — rewrite `_parse_scripting_file`, add `_split_statements`, `_extract_procedure_body`, `_is_indexable`; keep `_EMBEDDED_DML` for BigQuery fallback only

**Test fixtures to add**:
- `tests/fixtures/snowflake/scripting_noise.sql`:
  A scripting block with `ALTER WAREHOUSE`, `CALL`, `TRUNCATE TABLE` (all noise),
  followed by two DML statements that must pass: `INSERT INTO ... SELECT` (x2).
  Expected: only the two INSERTs are indexed; the three noise statements are dropped at DEBUG.
- `tests/fixtures/snowflake/procedure.sql`:
  `CREATE OR REPLACE PROCEDURE my_proc() AS $$ BEGIN INSERT INTO t SELECT ... END $$`.
  Expected: INSERT inside the `$$` body is indexed.

**Deferred (other dialects)**:
- BigQuery procedure fixture (`tests/fixtures/bigquery/procedure.sql`) — deferred until
  BigQuery-specific issues are filed. The BigQuery regex fallback must not regress, but
  its integration test is out of scope for this sprint.

**Tests to add**:

New SQL fixtures required:

1. `tests/fixtures/snowflake/scripting_noise.sql` (exact content):
   ```sql
   -- Fixture: Snowflake scripting block with DML and noise statements (T-11)
   -- ALTER WAREHOUSE → exp.Command → DROPPED (not exp.DML)
   -- CALL           → exp.Command → DROPPED
   -- TRUNCATE TABLE → exp.Truncate (DDL) → DROPPED (not exp.DML)
   -- INSERT INTO    → exp.Insert (exp.DML subclass) → PASSES
   BEGIN
     ALTER WAREHOUSE IDENTIFIER($my_wh) SET WAREHOUSE_SIZE = 'X-Large';
     CALL MA.MSSPR_UPDATE_STATS();
     TRUNCATE TABLE staging.temp_load;
     INSERT INTO dwh.target (id, amount)
     SELECT id, amount
     FROM raw.source
     WHERE active = TRUE;
     INSERT INTO dwh.audit_log (id, ts, event)
     SELECT id, CURRENT_TIMESTAMP() AS ts, 'load_done' AS event
     FROM raw.source;
   END;
   ```
   **Assertions**:
   - `assert len(parsed.statements) == 2` — exactly the two INSERTs, nothing else
   - `assert all(s.kind == "INSERT" for s in parsed.statements)`
   - `caplog` at WARNING level must be empty — ALTER WAREHOUSE, CALL, and TRUNCATE
     must be logged at DEBUG only

2. `tests/fixtures/snowflake/procedure.sql` (exact content):
   ```sql
   -- Fixture: Snowflake procedure with embedded DML (T-11)
   CREATE OR REPLACE PROCEDURE etl.load_orders()
   RETURNS VARCHAR
   LANGUAGE SQL
   AS $$
   BEGIN
     INSERT INTO dwh.orders (id, customer_id, amount)
     SELECT id, customer_id, amount
     FROM raw.orders
     WHERE processed = FALSE;
   END
   $$;
   ```
   **Assertions**:
   - `assert len(parsed.statements) == 1`
   - `assert parsed.statements[0].kind == "INSERT"`
   - `assert "raw.orders" in [str(s) for s in parsed.statements[0].sources]`
   - The `CREATE PROCEDURE` wrapper must not appear as a second statement

**Deferred fixtures (other dialects)**:
- `tests/fixtures/bigquery/procedure.sql` — BigQuery procedure body via regex fallback.
  Deferred until BigQuery issues are filed. Folder structure `tests/fixtures/bigquery/`
  should be created empty with a `.gitkeep` so the pattern is established.

Column lineage SQL pattern matrix for T-11 (scripting-block DML extraction):

| Pattern | Fixture | Expected statement kinds in `parsed.statements` | Expected absent kinds |
|---------|---------|--------------------------------------------------|----------------------|
| MERGE inside scripting block | `tests/fixtures/snowflake/scripting_noise.sql` | `MERGE` | `ALTER`, `CALL`, `TRUNCATE` |
| INSERT inside scripting block | `tests/fixtures/snowflake/scripting_noise.sql` | `INSERT` | `ALTER`, `CALL`, `TRUNCATE` |
| Procedure body INSERT (Snowflake `$$`) | `tests/fixtures/snowflake/procedure.sql` | `INSERT` | `CREATE_PROCEDURE` body as opaque node |
| BigQuery procedure body INSERT | `tests/fixtures/bigquery/procedure.sql` (deferred) | — | — |

Unit tests (parser layer — no graph backend, Snowflake only this sprint):

- `tests/unit/snowflake/test_scripting_noise.py`:
  - Scenario — noise dropped, DML passes: parse `tests/fixtures/snowflake/scripting_noise.sql`
    with `SnowflakeParser`; assert `len(parsed.statements) == 2`; assert all kinds are `"INSERT"`;
    assert `"UNKNOWN"` not in kinds (no `exp.Command` noise leaked through).
  - Scenario — no WARNING logs for noise: assert `caplog` at `logging.WARNING` level is empty.
    ALTER WAREHOUSE, CALL, and TRUNCATE must each appear in a DEBUG-level record only.

- `tests/unit/snowflake/test_procedure.py`:
  - Scenario — INSERT extracted from `$$` body: parse `tests/fixtures/snowflake/procedure.sql`
    with `SnowflakeParser`; assert `len(parsed.statements) == 1`;
    assert `parsed.statements[0].kind == "INSERT"`;
    assert `parsed.statements[0].sources` contains a reference to `raw.orders`.
  - **Exact count**: `assert len(parsed.statements) == 1` — CREATE PROCEDURE wrapper must not appear.

**Deferred unit tests (other dialects)**:
- `tests/unit/bigquery/test_procedure_fallback.py` — deferred until BigQuery issues filed.
  Note: the existing regex fallback must not regress. A snapshot regression test against the
  old regex path should be added as part of T-11 to guard this, even if the BigQuery
  integration test is deferred. This can live in `tests/unit/test_snowflake_parser.py` as
  a backward-compatibility assertion on the fallback code path.

Integration tests (full stack: fixture → Indexer → real KùzuDB → graph query, Snowflake only this sprint):

- `tests/integration/snowflake/test_scripting_block.py` (new file):

  **Scenario — scripting block INSERTs create SELECTS_FROM edges, noise does not**:
  - Index `tests/fixtures/snowflake/scripting_noise.sql` with dialect `"snowflake"` into in-memory KùzuDB.
  - Run: `db.run_read("MATCH (q:SqlQuery)-[:SELECTS_FROM]->(t:SqlTable) RETURN q.kind AS kind, t.qualified AS tbl ORDER BY kind", {})`.
  - Assert all rows have `kind == "INSERT"` — TRUNCATE, CALL, ALTER must not create any query nodes.
  - **Exact edge count (golden rule)**: `assert len(rows) == 2` — one SELECTS_FROM per INSERT; the three noise statements must produce zero edges.

  **Scenario — procedure body INSERT creates SELECTS_FROM edge**:
  - Index `tests/fixtures/snowflake/procedure.sql` with dialect `"snowflake"`.
  - Run: `db.run_read("MATCH (q:SqlQuery {kind: 'INSERT'})-[:SELECTS_FROM]->(t:SqlTable) RETURN t.qualified AS tbl", {})`.
  - Assert `len(rows) == 1`; assert `rows[0]["tbl"]` contains `"raw.orders"`.

**Deferred integration tests (other dialects)**:
- `tests/integration/bigquery/` — deferred until BigQuery issues filed.

  **Scenario — column lineage matrix for scripting-block patterns** (full column lineage end-to-end):

  This is the hardest correctness test. It requires that after T-10 and T-11 are both complete, column-level `COLUMN_LINEAGE` edges are present in the graph for scripting-block DML. The following matrix applies:

  | SQL Pattern | Fixture | Expected COLUMN_LINEAGE edge | Expected confidence | Expected exact edge count |
  |-------------|---------|------------------------------|---------------------|--------------------------|
  | Simple SELECT with alias | `views.sql` (`amount AS total`) | `orders.amount` → `customer_orders.total` | >= 0.7 | 1 |
  | SELECT * (no schema) | `tests/fixtures/snowflake/star_select.sql` (new) | `orders.*` → confidence downgrade | <= 0.3 | 0 explicit edges (no column expansion without schema) |
  | INSERT INTO ... SELECT | `tests/fixtures/snowflake/insert_select_impact.sql` | `source_schema.source_table.col_a` → `dwh.target_table.col_a` | >= 0.7 | 2 (col_a and col_b) |
  | MERGE INTO WHEN MATCHED UPDATE | `tests/fixtures/snowflake/scripting_noise.sql` | `raw.source.amount` → `dwh.target.amount` | >= 0.5 | 1 |
  | CTAS | `tests/fixtures/snowflake/etl_chain.sql` | `raw.orders.id` → `stage_orders.id` | >= 0.7 | 3 (id, customer_id, amount) |
  | Multi-step intra-file temp table | `tests/fixtures/snowflake/etl_chain.sql` | `raw.orders.amount` → `dwh.orders.amount` (through stage_orders) | >= 0.5 | >= 1 |
  | Snowflake scripting INSERT | `tests/fixtures/snowflake/procedure.sql` | `raw.orders.id` → `dwh.orders.id` | >= 0.5 | 3 (id, customer_id, amount) |

  For each row in the matrix, add an assertion in `tests/integration/snowflake/test_dialect_matrix.py` (Snowflake scenarios only this sprint; file is structured to accept future dialect rows):
  ```python
  # Example for INSERT INTO ... SELECT (col_a)
  rows = db.run_read(
      "MATCH (src:SqlColumn {id: $src_id})-[e:COLUMN_LINEAGE]->(dst:SqlColumn {id: $dst_id}) "
      "RETURN e.confidence AS conf",
      {"src_id": "source_schema.source_table.col_a", "dst_id": "dwh.target_table.col_a"}
  )
  assert len(rows) == 1, f"Expected 1 edge, got {len(rows)}"
  assert rows[0]["conf"] >= 0.7, f"Expected confidence >= 0.7, got {rows[0]['conf']}"
  ```
  Repeat this pattern for each matrix row. The `SELECT *` row must assert `len(rows) == 0` (no expansion without schema) and that `parse_quality == TABLE_ONLY` or the edge has `confidence <= 0.3`.

  **New fixture required**:
  `tests/fixtures/snowflake/star_select.sql` (exact content):
  ```sql
  -- Fixture: SELECT * confidence downgrade test (T-11 column lineage matrix)
  INSERT INTO dwh.star_target
  SELECT *
  FROM raw.source_wide;
  ```
  Expected: after indexing with dialect `"snowflake"`, graph has zero `COLUMN_LINEAGE` edges for this file (no column expansion without DDL schema) OR one edge per column with `confidence <= 0.3` if schema is known. Assert `len(lineage_rows) == 0` when no DDL for `raw.source_wide` is indexed.

**Definition of done**:
- `ALTER WAREHOUSE`, `CALL`, `SET`, `LET`, `RETURN`, and other non-DML statements are
  dropped at DEBUG level, not WARNING, and do not appear in `parsed.statements`
- `ALTER WAREHOUSE`, `CALL`, `TRUNCATE TABLE`, and other non-DML statements are dropped
  at DEBUG level, not WARNING, and do not appear in `parsed.statements`
- `INSERT INTO ... SELECT` inside scripting blocks is indexed (passes via `exp.Insert ⊂ exp.DML`)
- Snowflake procedure bodies (via `$$...$$`) have their DML extracted and indexed
- BigQuery `exp.Command` bodies continue to use the regex fallback (no regression)
- `tests/fixtures/snowflake/scripting_noise.sql` and `tests/fixtures/snowflake/procedure.sql`
  produce the exact expected `parsed.statements` counts
- `tests/fixtures/bigquery/` directory exists with `.gitkeep` (structure established for future tests)

---

### T-13 — Add FN label and execute_cypher ratio to gain

**Source**: ARCHITECTURE_REVIEW.md 10.2.6 (rank 8, MEDIUM), 10.6 Q3

**What to do**:

**Part A — FN label** (10.6 Q3):

1. In `server/tools.py` `submit_feedback()`:
   - Change the valid labels check from `if label not in ("TP", "FP")` to
     `if label not in ("TP", "FP", "FN")`
   - Update the `ValueError` message to list all three labels
   - Update the docstring: add `FN` to the label description:
     `"FN" (false negative — expected a result but got empty)`

2. In `server/tools.py` docstring for `submit_feedback`, add:
   "Use label='FN' when the tool returned empty results but you expected results."

3. No MetricsStore schema change is needed — the `label` column is a free-text field;
   `FN` values are valid as-is.

**Part B — execute_cypher ratio in gain** (10.2.6):

1. In `cli/commands/gain.py` `gain_cmd()`, add a Section E after section D:
   - Query: count total tool calls and count `execute_cypher` calls
   - Compute ratio: `execute_cypher_count / total_calls`
   - If ratio > 0.3, print a yellow warning:
     "High raw-Cypher ratio: {ratio:.0%} of calls are execute_cypher. The high-level tools may not be meeting your needs. Check hint fields on empty results."
   - If ratio <= 0.3, print the ratio without a warning

2. Add this to the `--json` output as `"execute_cypher_ratio"`.

**Files affected**:
- `src/sqlcg/server/tools.py` — `submit_feedback()` label validation and docstring
- `src/sqlcg/cli/commands/gain.py` — Section E, execute_cypher ratio

**Tests to add**:

Unit tests:

- `tests/unit/test_submit_feedback.py`:
  - Scenario A — FN label accepted: call `submit_feedback("trace_column_lineage", "orders.amount", "FN")` with a real `MetricsStore` in `tmp_path`; assert no exception is raised; assert the stored record's `label` field equals `"FN"` when queried back via `store.get_recent(n=1)`.
  - Scenario B — TP and FP still valid: assert `submit_feedback(..., "TP")` and `submit_feedback(..., "FP")` do not raise.
  - Scenario C — invalid label raises: assert `submit_feedback(..., "XX")` raises `ValueError`; assert `"FN"` appears in the `ValueError` message (the error must list all three valid labels so the user knows FN is an option).
  - Assertion on stored label: `assert records[0].label == "FN"` — not just "no exception"; the label must round-trip through the store.

- `tests/unit/test_gain_ratio.py`:
  - Scenario A — ratio above 0.3 triggers warning: populate a real `MetricsStore` in `tmp_path` with 15 `execute_cypher` rows and 5 `trace_column_lineage` rows; invoke `gain_cmd` via `CliRunner`; assert output contains `"execute_cypher"` ratio section; assert the ratio `0.75` or `75%` appears in output; assert a warning phrase containing `"raw-Cypher"` or `"high"` appears.
  - Scenario B — ratio at or below 0.3 shows no warning: populate store with 2 `execute_cypher` and 18 `trace_column_lineage` rows; invoke `gain_cmd`; assert the ratio `0.10` or `10%` appears; assert `"high"` warning does NOT appear in the ratio section.
  - Scenario C — zero calls: populate store with zero rows; invoke `gain_cmd`; assert no division-by-zero exception; assert ratio section shows `0%` or `"No calls recorded"`.

Integration test (real MetricsStore populated by real tool calls):

- Scenario: use the real `MetricsStore` writing to `tmp_path / "metrics.db"`; call `trace_column_lineage("orders.amount")` 5 times and `execute_cypher("MATCH (n) RETURN n")` 2 times against a real in-memory KùzuDB (ignore the result); invoke `gain_cmd`; assert the output ratio is `"28%"` or `0.28` (2/7 rounded). This confirms the ratio is computed from actual `_timed_tool` decorators, not from a mock.

**Definition of done**:
- `submit_feedback` accepts `"FN"` as a valid label
- `submit_feedback` with `"XX"` raises `ValueError` naming all three valid labels
- `sqlcg gain` prints an `execute_cypher / total_calls` ratio section
- A ratio above 0.3 prints a yellow warning

---

### T-14 — Binary/package name note and uvx cold-start docs

**Source**: ARCHITECTURE_REVIEW.md 10.2.1 (combined into T-01 for code changes), 10.2.10 (rank 13, LOW)

Note: The code changes for T-01 already cover the binary/package name note in tool docstrings
and MCP setup output. T-14 covers the README documentation-only changes that are distinct.

**What to do**:

1. In `README.md`, add or update the `QUICK START` section:
   - Recommend `uv tool install sql-code-graph` for persistent local installs (fastest MCP startup)
   - Reserve `uvx sql-code-graph` for one-shot tries with a note: "First run may take several minutes on a cold cache."
   - Include the ordered three-step workflow (`db init`, `index`, `git install-hooks`)

2. In `cli/commands/install.py` `install_cmd()`, update the confirmation message to include:
   - If `uvx` was chosen: append "Note: Using uvx — first MCP startup may take several minutes on a cold cache. Run 'uv tool install sql-code-graph' for faster startup."

**Files affected**:
- `README.md` — QUICK START, `uv tool install` recommendation, cold-start warning
- `src/sqlcg/cli/commands/install.py` — uvx confirmation message

**Tests to add**:

Unit tests:

- `tests/unit/test_install_message.py`:
  - Scenario A — uvx available: patch `shutil.which("uvx")` to return `"/usr/bin/uvx"`; invoke `install_cmd` via `CliRunner` with a `tmp_path / "settings.json"` target; assert captured output contains `"cold cache"` or `"first MCP startup"` as a substring; assert the settings JSON written uses `"command": "uvx"`.
  - Scenario B — uvx not available: patch `shutil.which("uvx")` to return `None`; invoke `install_cmd`; assert `"cold cache"` does NOT appear in output; assert the settings JSON uses `"command": "sqlcg"`.
  - Each scenario must assert the exact JSON key written, not just the output message: parse `settings.json` with `json.loads()` and assert `config["mcpServers"]["sql-code-graph"]["command"]` equals `"uvx"` or `"sqlcg"` respectively.

Integration test: none required for T-14. All behaviour is in string output and filesystem writes; the unit scenarios above with `tmp_path` constitute full coverage.

**Definition of done**:
- README QUICK START section recommends `uv tool install` over `uvx` for regular use
- README notes that first `uvx` run may be slow
- `sqlcg install` confirmation message notes cold-start latency when using uvx

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| T-11 tokenizer API mismatch (Token.start/end attribute names) | MEDIUM | Developer must inspect installed `sqlglot.tokens.Token` class before coding; add a compatibility check at the top of the method |
| T-04 KùzuDB transaction API not matching documented pattern | MEDIUM | Check `kuzu` package version; read `conn.begin()` / `conn.commit()` / `conn.rollback()` method names from the live package; add a smoke test before implementing |
| T-10 investigation reveals a deeper architectural issue | LOW | If the root cause cannot be addressed in one PR (e.g., requires a new scope-traversal approach), T-10 should produce a regression test documenting the failure and a follow-up ticket |
| T-09 parse_quality field conflicts with existing `parsing_mode` field on QueryNode | LOW | These are different fields: `parsing_mode` is on `QueryNode` (already persisted to graph); `parse_quality` is on `ParsedFile` (aggregated metadata). No conflict expected. |

---

## Recommended Implementation Order

The following sequence minimizes merge conflicts and unblocks downstream tickets:

```
Week 1 (INDEPENDENT, XS/S):
  T-01 (XS) → T-02 (XS) → T-05 (XS) → T-14 (XS) → T-07 (S) → T-04 (S)

Week 2 (INDEPENDENT, S + foundation for blocked):
  T-03 (S) → T-08 (S) → T-13 (S) → T-06 (XS — clears T-09 and T-11 blocks)

Week 3 (DEPENDS ON T-06, M effort):
  T-12 (S) → T-10 (M) → T-11 (M) → T-09 (M)
```

T-06 is XS in code change but is a blocker for three M-effort tickets. Prioritize it early
in Week 2 so the three M-effort tickets can proceed in parallel if multiple developers are
working.
