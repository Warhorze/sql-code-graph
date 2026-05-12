# Feature Plan: Synthetic E{n} Lineage-Failure Test Suite (Snowflake)

## Summary

Build a synthetic, DWH-free lineage-failure test suite under `tests/snowflake/`
that gates two anchor scenarios as go/no-go sprint checks and pins current
Snowflake parser behaviour (correct or broken) for every known E{n} failure
pattern in [`ARCHITECTURE_REVIEW.md`](../ARCHITECTURE_REVIEW.md) §12.3. All
fixtures are minimal SQL; all assertions are on observable output
([`edges()`](../tests/snowflake/conftest.py) tuples or
[`ParsedFile.errors`](../src/sqlcg/parsers/base.py) / `parse_quality`).

## Scope

### In Scope

- Two anchor tests (OMLOOPSNELHEID, MA_AANTAL_OP_ORDER chain) under
  `tests/snowflake/anchors/`.
- One fixture + one test module per E-code listed below
  (E2, E3, E4, E5, E8, E12, E16, E18, E21, E23, E25, E27, E36).
- Failure-documentation tests for unimplemented features — must carry an
  `# INVERSION TARGET` comment so the next sprint can flip them.
- Creation of missing parent dirs (`tests/snowflake/E2/`, `E3/`, `E4/`,
  `tests/snowflake/anchors/`) with empty `__init__.py`.

### Non-Goals

- No parser code changes. This plan adds tests only.
- No DWH content, no excerpts from the real corpus, no real Dutch identifiers
  except inside Anchor 2's semantic-layer fixture (where they are load-bearing).
- No new helpers in [`tests/snowflake/conftest.py`](../tests/snowflake/conftest.py)
  unless a per-test reason is documented in the step.
- No fixture larger than ~30 lines of SQL. Anchor 2 may exceed this for the
  semantic file.
- No assertion of internal parser fields (private attributes, sqlglot AST shape).
  Only `result.statements`, `result.errors`, `result.parse_quality`, and
  `edge.{src,dst,confidence}` are public surface.

## Design

### Test discovery and layout

```
tests/snowflake/
  conftest.py                  # existing
  anchors/
    __init__.py                # NEW (empty)
    fixture_omloopsnelheid.sql # NEW
    fixture_source.sql         # NEW (Anchor 2)
    fixture_etl.sql            # NEW (Anchor 2)
    fixture_semantic.sql       # NEW (Anchor 2)
    test_anchor_omloopsnelheid.py  # NEW
    test_anchor_ma_aantal_op_order.py  # NEW
  E2/                          # NEW dir
    __init__.py
    e2_expr_alias.sql
    test_e2.py
  E3/                          # NEW dir
    __init__.py
    e3_ddl_only.sql
    test_e3.py
  E4/                          # NEW dir
    __init__.py
    e4_if_not_exists.sql
    e4_unpivot.sql
    e4_unexpected_token.sql
    test_e4.py
  E5/                          # existing dir
    e5_cte_missing_source.sql
    test_e5.py
  E8/
    e8_dynamic_sources.sql
    test_e8.py
  E12/
    e12_lateral_flatten.sql
    test_e12.py
  E16/
    e16_merge.sql
    test_e16.py
  E18/
    e18_iff_decode.sql
    test_e18.py
  E21/
    e21_alias_forward_ref.sql
    test_e21.py
  E23/
    e23_stored_proc.sql
    test_e23.py
  E25/
    e25_cross_db.sql
    test_e25.py
  E27/
    e27_udf.sql
    test_e27.py
  E36/
    e36_temp_table.sql
    test_e36.py
```

### Conventions every test must follow

1. Use the `parser` fixture and the `parse` / `edges` helpers from
   [`conftest.py`](../tests/snowflake/conftest.py). Import them via
   `from tests.snowflake.conftest import parse, edges`. Do not redefine them.
2. Each fixture SQL lives in its own `.sql` file alongside the test. Read with
   `Path(__file__).with_name("e{n}_xxx.sql").read_text()`. Never inline more
   than a one-line SQL string in the test body.
3. Every assertion must be on observable output: counts/tuples from `edges()`,
   `result.errors` (list of structured strings), `result.parse_quality`
   (`FULL` / `TABLE_ONLY` / `SCRIPTING_FALLBACK` / `FAILED` — all four are live
   values in [`base.py`](../src/sqlcg/parsers/base.py)), or
   `stmt.parse_failed` / `stmt.parsing_mode`. No "did not raise" tests.
4. Failure-documentation tests must include the exact comment:
   `# INVERSION TARGET: when E{n} is fixed, flip to positive edge assertion`
   on the line above the assertion that pins the broken state.
5. Test IDs must be `test_e{n}_<short_pattern>` so `pytest -k e36` works.
6. **Case sensitivity**: sqlglot normalises unquoted identifiers to uppercase for
   table names and column names returned in `LineageNode`. All edge tuple
   comparisons must match the case the parser actually produces. Use
   `.upper()` / `.lower()` normalisation when the exact case is ambiguous, or
   run the fixture through the parser once and inspect the output to determine
   the real case before writing the assertion. As a baseline: bare identifier
   table names are returned UPPERCASE (`"SRC"`, not `"src"`), aliased output
   columns retain their declaration case, and quoted identifiers preserve their
   quoted case. Confirm each assertion with a sanity-run before commit.

### Why these constraints

- `parse_quality` and `errors` are public on `ParsedFile`; `confidence > 0`
  gating is already inside `edges()`. We rely on the same surface that
  downstream code (indexer, MCP server) sees.
- Fixtures-as-files (not heredocs) keep the SQL diff-reviewable and let humans
  paste a fixture directly into a Snowflake worksheet to confirm validity.

## Implementation Steps

Steps are ordered so the smallest, cheapest fixtures go first. The developer
must commit after each step group (anchors / per-E-code) and not bundle the
whole suite into one commit.

### Phase 0: Scaffold missing directories

**Step 0.1**: Create `tests/snowflake/anchors/__init__.py` (empty),
`tests/snowflake/E2/__init__.py`, `tests/snowflake/E3/__init__.py`,
`tests/snowflake/E4/__init__.py`.

Note: E5, E8, E12–E27, E36 directories already exist (empty `__init__.py`
only). No re-creation needed; just add fixture SQL and test files.

- Files affected: 4 new empty `__init__.py`.
- Acceptance: `uv run pytest tests/snowflake --collect-only` discovers
  the new dirs with no errors.

### Phase 1: Anchor tests (sprint gates)

#### Step 1.1 — Anchor 1: OMLOOPSNELHEID temp-table drop

**Fixture**: `tests/snowflake/anchors/fixture_omloopsnelheid.sql`

Required SQL shape (developer writes the exact statement; this is the shape
that reliably triggers the pattern):

```sql
CREATE TEMP TABLE tmp_a AS
SELECT
    afzet,
    gemiddelde_vrd,
    afzet / NULLIF(gemiddelde_vrd, 0) AS omloopsnelheid
FROM stg_a;

CREATE TEMP TABLE tmp_b AS
SELECT
    afzet,
    gemiddelde_vrd,
    afzet / NULLIF(gemiddelde_vrd, 0) AS omloopsnelheid
FROM stg_b;

INSERT INTO persistent_target (afzet, gemiddelde_vrd)
SELECT afzet, gemiddelde_vrd FROM tmp_a
UNION ALL
SELECT afzet, gemiddelde_vrd FROM tmp_b;
```

Note: `omloopsnelheid` is **deliberately not** in the INSERT column list — this
is the exact silent-drop pattern from the corpus.

**Test**: `tests/snowflake/anchors/test_anchor_omloopsnelheid.py`

Assertions (all on `edges(result)`). Before writing assertion tuples, run the
fixture through the parser once to confirm actual identifier casing (see
Conventions §6). Expected values below assume CTAS targets are lowercased by
sqlglot (because they come from the `target` field, not from a LineageNode):

1. An edge from `stg_a` / `afzet` to `tmp_a` / `omloopsnelheid` is present.
   (Compare using `.upper()` or `.lower()` normalisation if case differs.)
2. An edge from `stg_a` / `gemiddelde_vrd` to `tmp_a` / `omloopsnelheid` is present.
3. Symmetric two edges for `stg_b → tmp_b`.
4. Zero edges whose `dst_table == "persistent_target"` and `dst_col == "omloopsnelheid"`
   (case-normalised).
5. Zero edges whose `src_col == "omloopsnelheid"` (column does not leak forward).

Acceptance: assertions 1–3 pass (parser sees the temp-table computation) and
assertions 4–5 pass (column is correctly absent from the INSERT). If 4–5 fail,
the test surfaces a real regression. If 1–3 fail, the test documents an
existing temp-CTE bug; mark with `# INVERSION TARGET` and ship anyway.

#### Step 1.2 — Anchor 2: MA_AANTAL_OP_ORDER 3-file chain

**Fixtures**:

- `tests/snowflake/anchors/fixture_source.sql` — a single
  `CREATE TABLE source_facts (ma_order_aantal NUMBER, verhoudingsgetal NUMBER, ...)`.
- `tests/snowflake/anchors/fixture_etl.sql` — two CTEs (`bm_orders`,
  `igdc_openstaand`) computing
  `SUM(ma_order_aantal / NULLIF(verhoudingsgetal, 0)) AS aantal_op_order`
  from `source_facts`, then `UNION ALL` into `openstaand_combined` CTE, then
  `INSERT INTO wtfs_openstaande_orders (ma_aantal_op_order) SELECT SUM(aantal_op_order) FROM openstaand_combined`.
- `tests/snowflake/anchors/fixture_semantic.sql` — semantic view
  `CREATE VIEW "Openstaande orders" AS SELECT ma_aantal_op_order AS "Aantal op order" FROM wtfs_openstaande_orders`.

**Test**: `tests/snowflake/anchors/test_anchor_ma_aantal_op_order.py`

Setup:

1. Build `SchemaResolver(dialect="snowflake")`. Parse `fixture_source.sql`
   text using `sqlglot.parse(sql, dialect="snowflake")` to get an AST list,
   then call `resolver.add_create_table(stmts[0])`. This method accepts an
   `exp.Create` AST node (not a `ParsedFile`) — confirmed at
   [`schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py) line 41.
   Do not call `parser.parse_file()` for this step; that returns a `ParsedFile`,
   not an AST node.
2. Instantiate `SnowflakeParser(schema_resolver=resolver)`.
3. Parse `fixture_etl.sql` and `fixture_semantic.sql` (in that order — order
   may matter to current implementation).
4. Aggregate edges across both `ParsedFile` results.

Assertions (full chain — every link is a separate assertion line). These are
the six links that must exist; mark each individually with `pytest.mark.xfail`
if the link is currently broken. Before writing tuple values, run the fixtures
through the parser and inspect the actual casing (see Conventions §6):

1. Edge from `source_facts` / `ma_order_aantal` to `bm_orders` / `aantal_op_order`.
2. Edge from `source_facts` / `ma_order_aantal` to `igdc_openstaand` / `aantal_op_order`.
3. Edge from `bm_orders` / `aantal_op_order` to `openstaand_combined` / `aantal_op_order`.
4. Edge from `igdc_openstaand` / `aantal_op_order` to `openstaand_combined` / `aantal_op_order`.
5. Edge from `openstaand_combined` / `aantal_op_order` to `wtfs_openstaande_orders` / `ma_aantal_op_order`.
6. Edge from `wtfs_openstaande_orders` / `ma_aantal_op_order` to `Openstaande orders` / `Aantal op order`.

Acceptance: if any of the 6 fail, mark with `pytest.mark.xfail` per
broken link with a reason string referencing the E-code that is implicated
(typically E36 for link 5 and E5 for link 6 if the schema is not resolved).
Do **not** delete failing links — they are the sprint's progress meter.

Cross-module constant alignment: confirm
[`SnowflakeParser`](../src/sqlcg/parsers/snowflake_parser.py) preserves quoted
identifiers (`"Aantal op order"`). If `_preprocess_snowflake_sql` strips
quoting, document the deviation in the test docstring.

### Phase 2: Per-E-code tests (alphabetical by code number)

Each step below lists fixture SQL shape and test assertions. The developer
writes literal SQL; the snippets here are intent specifications.

**Reminder**: before writing any tuple-based assertion, run the fixture through
the parser and print the actual edge tuples to confirm casing. Bare unquoted
identifiers come back UPPERCASE from sqlglot's lineage walker. See Conventions §6.

#### Step 2.1 — E2: Unaliased non-Column expression

**Fixture** `tests/snowflake/E2/e2_expr_alias.sql`:

```sql
SELECT x + y AS my_col FROM src;
```

**Test** `test_e2.py`:

Actual behaviour (verified): sqlglot returns edges with src_table `"SRC"`,
src_col `"X"` and `"Y"`, dst_col `"my_col"`, dst_table `"<output>"` (no INSERT
target). Tuples from `edges()` are:
`("SRC", "X", "<output>", "my_col")` and `("SRC", "Y", "<output>", "my_col")`.

- Assert `len([e for e in edges(result) if e[3] == "my_col"]) >= 1`.
- Assert `any(e[1].upper() == "X" and e[3] == "my_col" for e in edges(result))`.
- Assert `any(e[1].upper() == "Y" and e[3] == "my_col" for e in edges(result))`.
  This documents that an aliased binary expression is NOT silently skipped.

#### Step 2.2 — E3: DDL-only file

**Fixture** `tests/snowflake/E3/e3_ddl_only.sql`:

```sql
ALTER DYNAMIC TABLE my_table RESUME;
```

**Test** `test_e3.py`:

Actual behaviour (verified): `ALTER DYNAMIC TABLE` is unsupported by sqlglot
and falls back to `exp.Command`. `AnsiParser.parse_file` sets
`parse_quality = ParseQuality.SCRIPTING_FALLBACK` when any `Command` node is
present in the statement list. The returned quality is NOT `TABLE_ONLY`.

- Assert `edges(result) == []`.
- Assert `result.parse_quality in {ParseQuality.TABLE_ONLY, ParseQuality.SCRIPTING_FALLBACK}`
  (file parsed, no lineage expected; do not allow `FAILED` — that would be a
  regression). Both values are acceptable DDL-only outcomes.
- Assert no entry in `result.errors` contains "Exception" or "crash".

#### Step 2.3 — E4: Three dialect-gap fixtures

**Fixtures**:

- `tests/snowflake/E4/e4_if_not_exists.sql`:
  `CREATE TABLE t IF NOT EXISTS (id INT);`
- `tests/snowflake/E4/e4_unpivot.sql`: minimal Snowflake UNPIVOT, e.g.
  ```sql
  SELECT * FROM monthly_sales
  UNPIVOT(sales FOR month IN (jan, feb, mar));
  ```
- `tests/snowflake/E4/e4_unexpected_token.sql`: a short procedural block known
  to trigger an "Unexpected token" error in sqlglot's Snowflake dialect — the
  developer should grep the corpus's parse-error log
  ([`plan/sprint_06_lineage_coverage.md`](sprint_06_lineage_coverage.md)) for
  the exact pattern and reproduce minimally.

**IMPORTANT — known preprocessing**: `SnowflakeParser._preprocess_snowflake_sql`
already handles `IF NOT EXISTS` (strips it) and `UNPIVOT` (strips the clause).
After preprocessing:

- `e4_if_not_exists.sql` parses as valid DDL → `parse_quality = TABLE_ONLY`,
  `errors = ["parse_mode:pure_ddl_skip"]`. This is intentional — the fixture
  documents that preprocessing silently recovers the pattern.
- `e4_unpivot.sql` after UNPIVOT strip becomes `SELECT * FROM monthly_sales` →
  `parse_quality = TABLE_ONLY`, `errors = ["col_lineage_skip:star:..."]`.
- `e4_unexpected_token.sql` must be a pattern that preprocessing does NOT fix
  (grep the parse-error log for a non-UNPIVOT, non-IF-NOT-EXISTS failure).

**Test** `test_e4.py` (parametrize over the 3 fixtures):

- Assert `parse_file` returns a `ParsedFile` (does not raise).
- Assert `result.parse_quality in {ParseQuality.FAILED, ParseQuality.TABLE_ONLY,
  ParseQuality.SCRIPTING_FALLBACK}` — all three indicate no full lineage.
- Assert `len(result.errors) >= 1` and each error is a non-empty string.
- Assert `edges(result) == []` — broken/stripped files must not produce
  high-confidence lineage. (The `edges()` helper already filters `confidence > 0`.)
- For `e4_if_not_exists` and `e4_unpivot`: add a comment documenting that
  preprocessing transforms these patterns before they reach sqlglot, so the
  "failure" is handled gracefully, not hard-errored.

#### Step 2.4 — E5: CTE referencing missing source

**Fixture** `tests/snowflake/E5/e5_cte_missing_source.sql`:

```sql
WITH x AS (SELECT a FROM table_not_in_schema)
SELECT a FROM x;
```

**Test** `test_e5.py`:

Actual behaviour (verified): with no schema loaded, sqlglot's lineage walker
resolves the CTE body and traces the source column back to
`table_not_in_schema.A` with `confidence = 0.9`. No zero-confidence placeholder
is emitted. The edge IS present in `edges(result)`.

The plan's original assertion (`confidence == 0.0` placeholders) does not match
actual behaviour. Correct assertion:

- Use a freshly-built `SchemaResolver()` with **no** schema loaded.
- Assert `len(edges(result)) >= 1` — the CTE resolves, and an edge is present.
- Assert `any(e[0].upper() == "TABLE_NOT_IN_SCHEMA" for e in edges(result))` —
  the source is traceable to the missing table, confirming CTE resolution works.
- Add:
  ```python
  # INVERSION TARGET: when E5 cross-file resolution lands, assert
  # the src_table resolves to the *actual* file-qualified table, not
  # the raw name from the missing schema reference.
  ```

#### Step 2.5 — E8: Dynamic / literal sources

**Fixture** `tests/snowflake/E8/e8_dynamic_sources.sql`:

```sql
SELECT
    current_timestamp() AS ts_col,
    NEXTVAL('my_seq') AS seq_col,
    'static value' AS lit_col
FROM dual;
```

**Test** `test_e8.py`:

Actual behaviour (verified): `parse_quality = TABLE_ONLY`, zero high-confidence
edges, errors contain `col_lineage_skip:dynamic_source:ts_col` etc.

- Assert `edges(result) == []`.
- Assert `any("dynamic_source" in err for err in result.errors)` — confirms the
  skip is intentional and recorded, not a silent drop.
- This is **expected correct behaviour**. No `# INVERSION TARGET`.

#### Step 2.6 — E12: LATERAL FLATTEN

**Fixture** `tests/snowflake/E12/e12_lateral_flatten.sql`:

```sql
SELECT f.value::STRING AS col
FROM tbl, LATERAL FLATTEN(input => arr) f;
```

**Test** `test_e12.py`:

- Assert `[e for e in edges(result) if e[3] == "col"] == []` OR
  `any("flatten" in err.lower() for err in result.errors)`.
- `# INVERSION TARGET: when E12 fixed, assert edge from tbl.arr to (alias).col`.

#### Step 2.7 — E16: MERGE multi-branch

**Fixture** `tests/snowflake/E16/e16_merge.sql`:

```sql
MERGE INTO dst USING src ON dst.id = src.id
WHEN MATCHED THEN UPDATE SET col = src.col_a
WHEN NOT MATCHED THEN INSERT (col) VALUES (src.col_b);
```

**Test** `test_e16.py`:

Actual behaviour (verified): MERGE statements produce `kind = "MERGE"` and
**zero column lineage edges** (`parse_quality = TABLE_ONLY`). The parser does
not extract column lineage from MERGE branches at all.

The plan's original assertion "at least one edge exists" would always fail.
Correct assertion:

- Assert `edges(result) == []` — this pins the current zero-lineage state.
- `# INVERSION TARGET: when E16 fixed, assert at least one edge with`
  `dst_table == "DST" and dst_col == "col" from src.col_a (MATCHED branch)`
  `and src.col_b (NOT MATCHED branch) — srcs == {"COL_A", "COL_B"}`.

#### Step 2.8 — E18: IFF / DECODE

**Fixture** `tests/snowflake/E18/e18_iff_decode.sql`:

```sql
SELECT IFF(x > 0, a, b) AS col FROM src;
```

**Test** `test_e18.py`:

Actual behaviour (verified): sqlglot extracts edges for all three arguments
of IFF (condition `x` and both branches `a`, `b`). All edges present with
`confidence = 0.9`. Src table is `"SRC"` (uppercase).

- Assert `any(e[1].upper() == "A" and e[3] == "col" for e in edges(result))`.
- Assert `any(e[1].upper() == "B" and e[3] == "col" for e in edges(result))`.
- No `xfail` needed — this is currently correct behaviour.
- Note in test docstring: "IFF both branches produce edges; this is working as
  of the current sqlglot version. If a future version regresses, add xfail."

#### Step 2.9 — E21: Alias forward reference

**Fixture** `tests/snowflake/E21/e21_alias_forward_ref.sql`:

```sql
SELECT a + 1 AS b, b * 2 AS c FROM src;
```

**Test** `test_e21.py`:

Actual behaviour (verified): sqlglot resolves `c` back through the expression
`b * 2` to the root column `a` from `src`. Edges present: `("SRC", "A", "<output>", "b")`
and `("SRC", "A", "<output>", "C")`. Column `c` traces back to `src.A` (not a
forward-reference bug), destination column name is `"C"` (uppercase).

- Assert `any(e[3].upper() == "C" for e in edges(result))` — at least one edge
  to `c` exists.
- Assert `any(e[1].upper() == "A" and e[3].upper() == "C" for e in edges(result))` —
  the alias chain resolves correctly to the original source.
- No `xfail` needed — currently working.

#### Step 2.10 — E23: Stored procedure body

**Fixture** `tests/snowflake/E23/e23_stored_proc.sql`:

```sql
CREATE OR REPLACE PROCEDURE p() RETURNS STRING LANGUAGE SQL AS
$$
BEGIN
    INSERT INTO dst (a) SELECT a FROM src;
    RETURN 'ok';
END;
$$;
```

**Test** `test_e23.py`:

Actual behaviour (verified): `_has_scripting_block` detects `BEGIN` → routes
to `_parse_scripting_file` → `_EMBEDDED_DML` regex extracts the INSERT → edge
`("SRC", "A", "dst", "a")` is present with `confidence = 0.9` (reduced to 0.3
on `query_node.confidence`, but `LineageEdge.confidence` itself is 0.9 from the
lineage walk). `parse_quality = SCRIPTING_FALLBACK`.

- Assert `any(e[0].upper() == "SRC" and e[1].upper() == "A" and e[2] == "dst" and e[3] == "a" for e in edges(result))`.
- Assert `result.parse_quality == ParseQuality.SCRIPTING_FALLBACK`.
- No `xfail` needed — currently working.

#### Step 2.11 — E25: Cross-database reference

**Fixture** `tests/snowflake/E25/e25_cross_db.sql`:

```sql
SELECT a FROM other_db.public.tbl;
```

**Test** `test_e25.py`:

Actual behaviour (verified): sqlglot parses the three-part identifier; the
lineage walker produces an edge `("TBL", "A", "<output>", "a")` — the catalog
and db parts are NOT preserved in the `src.table.name` of the `LineageEdge`
(because `TableRef.name` is just the bare table name). Source is traceable
but NOT qualified.

- Assert `len(edges(result)) >= 1` — a bare-name edge IS produced.
- Assert `any(e[0].upper() == "TBL" for e in edges(result))` — bare table name
  is present.
- Assert `not any(e[0] == "other_db.public.tbl" for e in edges(result))` —
  fully-qualified name is NOT preserved (this is the E25 bug).
- `# INVERSION TARGET: when E25 cross-db resolution lands, assert`
  `src_table carries the fully-qualified "other_db.public.tbl" name`.

#### Step 2.12 — E27: UDF as column source

**Fixture** `tests/snowflake/E27/e27_udf.sql`:

```sql
SELECT my_udf(src_col) AS dst_col FROM src;
```

**Test** `test_e27.py`:

- Compute `dst_edges = [e for e in edges(result) if e[3] == "dst_col"]`.
- Pin current behaviour: assert `dst_edges == []`
  (the parser does not see through the UDF boundary), OR if it captures the
  arg-as-source, assert `any(e[1].upper() == "SRC_COL" for e in dst_edges)`.
- `# INVERSION TARGET: when E27 fixed, assert edge with src_col == "SRC_COL"`
  `and dst_col == "dst_col" from src table`.

#### Step 2.13 — E36: Cross-statement temp table (highest priority)

**Fixture** `tests/snowflake/E36/e36_temp_table.sql`:

```sql
CREATE TEMP TABLE t AS SELECT a FROM src;
INSERT INTO dst SELECT a FROM t;
```

**Test** `test_e36.py`:

Actual behaviour (verified): both edges ARE present. The sources_map
accumulation in `AnsiParser.parse_file` carries the CTAS body forward, so
the INSERT resolves through `t` back to `src`. Edges produced:
`("SRC", "A", "t", "a")` and `("SRC", "A", "dst", "a")`.

The plan's original assertion `("t", "a", "dst", "a")` absent is **wrong** —
that edge does NOT appear; what appears is the transitive resolution
`SRC.A → dst.a`. But the E36 bug (temp-table propagation not working
*across files*) is not visible in a single-file fixture.

Corrected assertions:

- Assert `any(e[0].upper() == "SRC" and e[2] == "t" for e in edges(result))` —
  the CTAS edge `SRC.A → t.a` is present.
- Assert `any(e[0].upper() == "SRC" and e[2] == "dst" for e in edges(result))` —
  the resolved INSERT edge `SRC.A → dst.a` is also present (sources_map resolved `t`).
- Assert `not any(e[0] == "t" for e in edges(result))` — `t` never appears as
  a *source* table in any edge (it is expanded, not forwarded raw).
- `# INVERSION TARGET: when E36 cross-file temp-table lands, write a two-file`
  `test where fixture_a.sql creates the TEMP TABLE and fixture_b.sql INSERTs`
  `from it, and assert the cross-file edge appears`.

Note: the E36 bug is a **cross-file** problem. A single-file fixture shows
working intra-file resolution. The inversion target must use a two-file setup.

## Test Strategy

- **Unit tests**: every test under `tests/snowflake/E*/` and `tests/snowflake/anchors/`
  is a unit test against the parser; no KuzuDB, no MCP, no CLI.
- **Integration tests**: none added by this plan. Indexer / aggregator behaviour
  is out of scope.
- **Markers**: failing-by-design assertions use `pytest.xfail("E{n}: <reason>")`
  or are wrapped behind an `if`-branch with a parametrized expected state. No
  `pytest.skip` — skipped tests rot.
- **Discovery sanity**: `uv run pytest tests/snowflake --collect-only` must
  show all 13 E-code modules plus 2 anchor modules.
- **Single-test invocation**: every test ID must run individually, e.g.
  `uv run pytest tests/snowflake/E36/test_e36.py::test_e36_temp_table -x`.

## Acceptance Criteria

- [ ] `tests/snowflake/anchors/` exists with 4 fixtures and 2 test modules.
- [ ] Anchor 1 asserts both the 4 temp-CTE edges and the absence of
      `omloopsnelheid` in `persistent_target`.
- [ ] Anchor 2 asserts all 6 links of the MA_AANTAL_OP_ORDER chain (failing
      links use `pytest.mark.xfail` with E-code reason).
- [ ] Each of E2, E3, E4, E5, E8, E12, E16, E18, E21, E23, E25, E27, E36 has
      its own `tests/snowflake/E{n}/e{n}_*.sql` fixture and `test_e{n}.py`.
- [ ] E4 has three separate fixtures driven by `@pytest.mark.parametrize`.
- [ ] Every failure-documentation test (E12, E16, E25, E27, E36, and any
      xfail in Anchors) carries the exact comment
      `# INVERSION TARGET: when E{n} ...`.
- [ ] No test asserts only "did not raise" — every test asserts on
      `edges()` content, `result.errors`, or `result.parse_quality`.
- [ ] `uv run pytest tests/snowflake -x` runs to completion with all expected
      passes green and all xfails reported as xfailed (not errored).
- [ ] No DWH-derived content; no Dutch identifiers outside
      `fixture_semantic.sql` and Anchor 2's chain (which uses minimal
      `bm_orders` / `igdc_openstaand` CTE names — explicitly approved).
- [ ] `rg "import.*ansi_parser\|import.*ansi" tests/snowflake/` returns
      nothing — tests only import `SnowflakeParser` and `SchemaResolver`
      (per [`conftest.py`](../tests/snowflake/conftest.py)).

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Failing-by-design tests rot into "everyone ignores xfails" | All xfails carry an E-code in the reason string. Sprint compliance check greps for `xfail("E` and reports the count as a regression-or-progress signal. |
| Anchor 2 over-specifies internal table aliases | Use simple CTE names (`bm_orders`, `igdc_openstaand`, `openstaand_combined`); parser already preserves CTE alias as `TableRef.name`. Confirmed against [`base.py`](../src/sqlcg/parsers/base.py) `QueryNode.ctes` field. |
| E4 assertions mismatch after sqlglot version upgrade | E4 test pins `parse_quality in {FAILED, TABLE_ONLY, SCRIPTING_FALLBACK}` and asserts `edges(result) == []`. If a future sqlglot lands FULL parse, the developer should update the assertion narrowness, not silence it. |
| `SchemaResolver.add_create_table` signature changes | Wrap the Anchor 2 setup in a 4-line helper at the top of the test file; if the API shifts, only one location needs updating. |
| Quoted identifiers (`"Aantal op order"`) get lowercased by preprocessing | Anchor 2 test runs the exact SQL through `SnowflakeParser._preprocess_snowflake_sql` once first and asserts the quote survives, then proceeds. If it does not survive, that is a real bug and a separate finding for `ARCHITECTURE_REVIEW.md`. |
| Plan adds 18 files; merge conflicts on parallel sprints | Land in a single PR titled `tests: synthetic E{n} failure suite (sprint gate)`. Do not split per-E-code into multiple PRs. |
| Edge tuple casing wrong at assertion time | Developer must print actual edges from each fixture before writing assertions. The Conventions §6 case-sensitivity rule is mandatory. |

## Rollout / Rollback

- This plan adds tests only; no production code changes. Rollout is
  the merge of the test-suite PR.
- Rollback is a simple revert. No data migration. No re-index.
- Once landed, sprint 07's planning doc must reference the two anchor tests as
  the entry/exit criteria for any future lineage work.

## Plan-Compliance Checklist (for plan-reviewer)

- Every test file imports from `tests/snowflake/conftest.py` (no helper
  redefinition).
- Every test asserts observable output (not "no exception").
- Failure-documentation tests carry `# INVERSION TARGET` comment.
- Anchor tests live under `tests/snowflake/anchors/`.
- No fixture contains corpus content or real DWH identifiers (except where
  load-bearing in Anchor 2's semantic file).
- Every new method / helper in tests has a grep-confirmed call site
  (CLAUDE.md rule).
