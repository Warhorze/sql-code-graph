# Feature Plan: Per-file namespacing of session-scoped TEMPORARY tables

**Status: APPROVED (rev 3)** — plan-reviewer verdict APPROVE-WITH-AMENDMENTS (2026-06-12,
rev 2 cleared the rework blockers); amendments A5–A6 applied below. Ready for the developer.

**Plan-review amendments applied (2026-06-12, rev 3):**
- **A5 (warning)** — Step 2.1 source stamping must be placed **between the target-exclusion
  filter (`ansi_parser.py:409`) and the `_extract_column_lineage` call (line 422)**, NOT after
  the dedup; otherwise a `SELECT *` from a temp produces an orphaned bare-key `kind='table'`
  star-source row (verified: `query_sources=sources` feeds star resolution). Added star-source
  acceptance test.
- **A6 (warning)** — documented why the symmetric `Stored='table', EXCLUDED='temp'` direction is
  structurally impossible (namespaced vs bare PK never collide; the `_current_file_namespace
  is not None` guard means no bare `kind='temp'` row is ever emitted).

**Plan-review amendments applied (2026-06-12, rev 2):**
- **A1 (blocker)** — `upsert_nodes_bulk` ON CONFLICT must protect `kind='temp'` from cross-batch
  overwrite (§Cross-batch kind protection, Step 3.2, consumer audit row, test + acceptance).
- **A2 (blocker)** — `_lineage_node_to_edges` leaf key-formation spelled out exactly (Step 2.2).
- **A3 (warning)** — single shared `_temp_identity(db, name)` helper handling `db=None`, used
  byte-identically at Steps 1.2 / 2.1 / 2.2 (replaces the inconsistent inline `f"{db}.{name}"`).
- **A4 / W3 (warning)** — `star_sources` hardcoded-kind emit documented as no-change-IF-A1 in
  the consumer audit.
- **W1** — backward-only temp stamping made explicit (no forward-reference pre-scan).
- **W4** — temp-set reset required at the Snowflake `_parse_scripting_file` site too (Step 1.1).

Source of evidence: shepherd measurement on the live DWH graph (v1.19.1, 2026-06-12).
Reuses the per-file namespace machinery introduced for CTE/derived collisions in
[`sprint_postmortem_fixes.md`](sprint_postmortem_fixes.md) §PR 3 (repo-relative
`<rel-path>::<name>` keys via `TableRef.namespace` + `TableRef.full_id`). Honours the
performance-invariant table and the catalog-enrichment / no-parse-time-schema rules in
[`CLAUDE.md`](../../CLAUDE.md) and [`ARCHITECTURE_REVIEW.md`](../../ARCHITECTURE_REVIEW.md)
§3.2b. Single PR, version **1.21.0** (minor — new `kind='temp'` surface; master is 1.20.0).

## Summary

Session-scoped Snowflake `CREATE [OR REPLACE] TEMPORARY TABLE` targets are currently fused
into a single shared `kind='table'` graph node per schema-qualified name, falsely bridging
unrelated pipelines that each run a private session instance. This is the same identity-
collision class as the CTE collisions fixed by PR #86, applied to temporary tables. The fix
keys each temp's `TableRef` to its defining file via the existing namespace machinery and a
new `kind='temp'`, so `ba.tmp_base` defined in 13 files becomes 13 per-file nodes
(`etl/.../wtfv_bon.sql::ba.tmp_base`), and intra-file lineage through the temp survives
(re-keyed, not dropped).

## Measured evidence (live DWH graph v1.19.1, shepherd 2026-06-12)

- `ba.tmp_base`: **13 distinct files** each run `create or replace temporary table tmp_base
  as …`. The graph fuses them into **ONE** `kind='table'` node → false bridge across 13
  unrelated pipelines.
- Class-wide: **85** tmp-named tables have >1 writer file (`da.tmp_max_mut` ×6,
  `ba.tmp_prognose_ontdubbeld` ×5, `da.tmp_vt` ×5), touching **~2,607 COLUMN_LINEAGE edges**.
  That 85/2,607 count is **name-pattern-based and is NOT the fix trigger** — the fix keys off
  the **TEMPORARY property on the sqlglot `exp.Create` AST**, never a name heuristic.
- **368** multi-writer targets exist in total. The non-temporary subset (the `368 − 85` set
  of genuinely shared physical tables) **must be byte-identical before/after**.

### sqlglot detection (verified during planning)

`sqlglot.parse_one("create or replace temporary table ba.tmp_base as select 1 as x",
dialect="snowflake")` yields an `exp.Create` whose `args["properties"]` contains a
`exp.TemporaryProperty`; `create_stmt.find(exp.TemporaryProperty) is not None` is the reliable,
dialect-generic test (the same property is produced for ANSI `CREATE TEMPORARY TABLE`). `.this`
is the `exp.Table` (`ba.tmp_base`). Use `find(exp.TemporaryProperty)`, not name matching.

## Scope

### In Scope
- Detect the TEMPORARY property on a CREATE target and key its `TableRef` to the defining
  file (namespace = `self._current_file_namespace`, repo-relative since #86) with a new
  `kind='temp'`.
- Track per-file temp identities so that **subsequent same-file references** (reads after the
  CREATE) resolve to the SAME namespaced key — in both pass-1 and pass-2.
- Make `TableRef.full_id` emit the namespaced key for the temp role (today the namespace only
  fires when `role != "table"` — temps will use `role="temp"`, so the existing condition
  already covers them; confirm and document).
- Apply the same handling in the ANSI parser (the property is dialect-generic).
- Audit and decide change/no-change for every `kind`/`'::'` consumer (the A3 pattern).
- Version bump to 1.21.0 + `uv lock`.

### Non-Goals
- **No name-heuristic detection.** The `tmp_*` naming convention is evidence, not the trigger.
  A real shared table named `tmp_archive` (no TEMPORARY property) must be untouched.
- **No change to non-temporary multi-writer tables** (the `368 − 85` real-shared set).
- **No parse-time schema feeding** (forbidden, ARCHITECTURE_REVIEW §3.2b / CLAUDE.md).
- **No SCHEMA_VERSION bump** — `kind` is a stored *value* in the property-bag schema, not a
  column (`SCHEMA_VERSION = "8"` in [`schema.py`](../../src/sqlcg/core/schema.py); the `kind`
  property already carries `cte`/`derived`/`external`/`view`/`table` values). Confirmed: a new
  value needs no schema migration. Re-index is still the migration for the re-keyed nodes.
- No backward-compat shims; **re-index is the migration**.
- No new lineage-extraction surface — edges through temps already exist; this only re-keys
  their temp endpoint.

## Design

### Key form decision (load-bearing)

Temps are **schema-qualified** at parse time. `SnowflakeParser._qualify_bare_tables`
([`snowflake_parser.py:363-392`](../../src/sqlcg/parsers/snowflake_parser.py)) prefixes every
bare `exp.Table` (that is not a CTE alias) with the active `USE SCHEMA` schema **before**
`_parse_statement` runs. So both the CREATE target and every later read of `tmp_base` arrive
as `ba.tmp_base` (db=`ba`, name=`tmp_base`). The CTE case (PR #86) namespaced the **bare**
alias; the temp case must namespace the **schema-qualified** identity.

**Decision: key form is `<rel-path>::<db.name>` (schema-qualified bare component).**
Example: `etl/sql/fact/wtfv_bon.sql::ba.tmp_base`.

Rationale: after `_qualify_bare_tables` runs, the only identity the CREATE target and the
later reads share is `ba.tmp_base`. Namespacing the bare `tmp_base` only (dropping the schema)
would (a) lose the schema in the key and (b) require stripping the schema in the read path,
which `_qualify_bare_tables` has already added — a fight against the qualification pass. Keying
the full `db.name` keeps the CREATE and the reads identical and survives the USE-SCHEMA path.

`TableRef.full_id` ([`base.py:78-96`](../../src/sqlcg/parsers/base.py)) builds
`bare = ".".join([catalog, db, name])` then returns `f"{namespace}::{bare}"` when
`namespace and role != "table"`. With `role="temp"` and `db="ba"`, `name="tmp_base"`, it
already produces `etl/.../wtfv_bon.sql::ba.tmp_base` with **no code change to `full_id`** —
the `role != "table"` guard is satisfied by `temp`. **Confirm this in the unit test; do not
edit `full_id` unless the test shows otherwise.**

### Kind decision (load-bearing)

**Decision: new `kind='temp'`, NOT reuse of `cte`.** Rationale: provenance honesty. A temp is
a real (if session-scoped) physical table with DDL, not a CTE alias. Reusing `cte` would make
`_Q_CTE_COLLISIONS`, the scoped-health label ("excl. CTE/derived"), and `analyze
--include-intermediate` lie about what they exclude. A distinct value keeps every consumer's
wording honest and lets the catalog kind-upgrade guard (`WHERE kind='derived'`) naturally skip
temps. `role="temp"` on the `TableRef`; the indexer emits `kind = role` for the target node.

### Per-file temp tracking (the core mechanism)

Unlike CTEs (whose aliases are local to one statement and tracked in `_cte_names` /
`cte_alias_names` per statement), a temp is created in one statement and **read in later
statements of the same file**. The parser must remember temp identities across statements
within a file.

- Add an instance set `self._current_file_temp_keys: set[str]` (the schema-qualified
  identities, e.g. `{"ba.tmp_base"}`), reset to empty alongside `self._current_file_namespace`
  at the same two assignment sites in each parser
  ([`snowflake_parser.py:445,545`](../../src/sqlcg/parsers/snowflake_parser.py);
  [`ansi_parser.py:118,244`](../../src/sqlcg/parsers/ansi_parser.py)) and cleared in the
  end-of-file path. **Warning 4 — there are TWO reset sites in the Snowflake parser**, and the
  temp set must be cleared at BOTH alongside `_current_file_namespace = None`: the normal
  `parse_file` path AND the `_parse_scripting_file` path (which clears
  `_current_file_namespace = None` at [`snowflake_parser.py:561`](../../src/sqlcg/parsers/snowflake_parser.py),
  a bare end-of-method assignment, not a `finally`). The ANSI parser resets at
  [`ansi_parser.py:118,244`](../../src/sqlcg/parsers/ansi_parser.py). Add
  `self._current_file_temp_keys = set()` (or `.clear()`) at every site that sets the namespace,
  including the two assignment sites that set it at the START of a file (so a re-used parser
  instance starts each file clean) and the end-of-file clears.
- **Identity-key formula (Amendment 3 — single shared formula, used at ALL THREE sites).**
  The string registered in `self._current_file_temp_keys` and the string looked up at every
  read/leaf site MUST be byte-identical, or the chain breaks at the temp. Define one helper and
  use it everywhere (target registration, source stamping, lineage-leaf stamping):
  ```python
  @staticmethod
  def _temp_identity(db: str | None, name: str) -> str:
      """Lowercased schema-qualified temp identity; bare name when db is falsy (ANSI path)."""
      name = name.lower()
      return f"{db.lower()}.{name}" if db else name
  ```
  Snowflake's `_qualify_bare_tables` always sets `db`, so the key is `ba.tmp_base`. The ANSI
  path may have `db=None` (no USE-SCHEMA qualification), so the key is the bare `tmp_x` — the
  `f"{target.db}.{target.name}"` form WITHOUT this guard would wrongly produce `"none.tmp_x"`.
  Use `_temp_identity` literally at Step 1.2, Step 2.1, and Step 2.2; never inline the f-string.
- **Pass-1 ordering (Warning 1 — backward-only stamping is intentional).** Files are parsed
  statement-by-statement in order; `self._current_file_temp_keys` is built **incrementally** as
  CREATEs are seen. A later statement's source/leaf whose `_temp_identity` is in the set is
  stamped `role="temp"` + `namespace=self._current_file_namespace`. A reference to a temp that
  appears **before** its CREATE in the same file is deliberately treated as an ordinary physical
  table reference (it is not yet in the set). This is correct for valid SQL — you cannot read a
  session temp before it is created in the same flow. **Do NOT add a per-file pre-scan / two-pass
  temp discovery** to handle the non-existent forward-reference case; backward-only is by design.
- **Stamping site for the target**: `_extract_target_table`
  ([`ansi_parser.py:480-496`](../../src/sqlcg/parsers/ansi_parser.py)) is a `@staticmethod`
  with no `self` — it cannot read the namespace or set role. **Refactor decision**: do the
  temp detection + role/namespace stamping in the instance method `_parse_statement`
  (around [`ansi_parser.py:320-322`](../../src/sqlcg/parsers/ansi_parser.py)) **after**
  `_extract_target_table` returns, where `self` and the `exp.Create` are both available:
  ```python
  if (isinstance(stmt, exp.Create) and stmt.find(exp.TemporaryProperty) is not None
          and target is not None and self._current_file_namespace is not None):
      target = dataclasses.replace(target, role="temp",
                                   namespace=self._current_file_namespace)
      self._current_file_temp_keys.add(self._temp_identity(target.db, target.name))
  ```
  Keep `_extract_target_table` unchanged (still a static helper that returns the plain ref);
  the role/namespace decoration is layered on in the instance method that owns `self`.
- **Stamping site for the sources (Amendment 5 — exact placement, load-bearing)**: source
  `TableRef`s are produced by `_real_tables`
  ([`base.py:582-613`](../../src/sqlcg/parsers/base.py)) and the fallback scan. The stamping
  MUST be inserted **after the target-exclusion filter
  ([`ansi_parser.py:409`](../../src/sqlcg/parsers/ansi_parser.py),
  `sources = [src for src in sources if src.full_id != target.full_id]`) and BEFORE the
  `_extract_column_lineage` call ([`ansi_parser.py:422`](../../src/sqlcg/parsers/ansi_parser.py))**
  — NOT after the dedup at line ~460. Reason: `_extract_column_lineage` receives
  `query_sources=sources` (line 429) and uses it for **star resolution** (`_resolve_star_source`
  via `query_sources`). If the temp source is still un-stamped when the star path runs, the
  resulting `StarSource.source.full_id` is the **bare** `ba.tmp_base` (not `<rel>::ba.tmp_base`),
  and the indexer's hardcoded `star_sources` emit
  ([`indexer.py:1459`](../../src/sqlcg/indexer/indexer.py)) writes an orphaned `kind='table'`
  row under the wrong key. Amendment 1's ON CONFLICT guard prevents kind-corruption of the temp
  node, but it cannot fix a wrong-key orphan — only correct ordering does. Map each source `s`
  whose `self._temp_identity(s.db, s.name)` is in `self._current_file_temp_keys` to a
  namespaced `role="temp"` ref via `dataclasses.replace(s, role="temp",
  namespace=self._current_file_namespace)`. This mirrors how the CTE fallback stamps a
  namespaced ref in `_lineage_node_to_edges`
  ([`base.py:844-853`](../../src/sqlcg/parsers/base.py)) but operates on the resolved source
  list rather than the lineage-node fallback.
- **Pass-2 / column lineage (Amendment 2 — exact leaf key-formation)**: the COLUMN_LINEAGE
  edges resolve through `sg_lineage` and `_lineage_node_to_edges`. The temp appears as a leaf
  source `exp.Table` whose `.db` is `ba` and `.name` is `tmp_base` (post-qualification on the
  Snowflake path; `.db` may be `None` on the ANSI path). Extend the namespace-stamping branch
  in `_lineage_node_to_edges` ([`base.py:844-853`](../../src/sqlcg/parsers/base.py)): in
  addition to the CTE-alias check, compute the leaf identity with the SAME helper —
  `self._temp_identity(source.db, source.name)` (read `source.db`/`source.name` off the
  `exp.Table`) — and if that string is in `self._current_file_temp_keys` and a namespace is
  set, return `TableRef(catalog=source.catalog, db=source.db, name=source.name, role="temp",
  namespace=self._current_file_namespace)`. The formula MUST be byte-identical to Step 1.2's
  registration (Amendment 3) so the leaf key matches the registered identity; a mismatch here
  (e.g. inlining `f"{db}.{name}"` without the `db=None` guard) would break the chain at the
  temp. This keeps the COLUMN_LINEAGE
  src/dst keys for the temp consistent with the target node — the ~2,607 edges are **re-keyed,
  not dropped**. The dst side (when a temp is the CREATE target) is keyed off `dst_table`,
  which is the already-stamped `target` — so dst is consistent by construction.
- **Hot-path safety**: all of this is a set membership test (`db.name in set`) per
  target/source/leaf — O(1) per ref, no qualify/scope/expand. `_current_file_temp_keys` is
  built incrementally as CREATEs are seen, never re-derived per column. The `find(exp.
  TemporaryProperty)` call is **once per CREATE statement**, not per column. Zero per-column
  cost. (See perf section.)

### Lineage-preservation invariant — temps must still collapse onto the correct tables

**This is the load-bearing correctness requirement (user-flagged).** Re-keying the temp to a
per-file `kind='temp'` node must NOT sever the lineage chain that flows *through* the temp.
A typical chain is `real_src → ba.tmp_base → real_final` (the temp is a bridge). The fix must
preserve `real_src → … → real_final` end-to-end. Two facts make this hold, and the plan
guarantees both:

1. **Single namespaced identity within a file.** The temp's CREATE-target dst key and every
   later same-file read src key must be the **identical** `<rel>::ba.tmp_base` key. If the
   CREATE side were namespaced but a read side were left as bare `ba.tmp_base` (or vice-versa),
   the COLUMN_LINEAGE chain would break at the temp and lineage *would* be lost. Steps 2.1
   (sources), 2.2 (lineage leaves), and 1.2 (target), plus the USE-SCHEMA parity test, exist
   precisely to enforce one identity. **Acceptance**: an integration test traces a real source
   through a temp to a real sink (`real_src → tmp → real_final`) and asserts the upstream/
   downstream trace from `real_final` reaches `real_src` (the chain is intact), with the temp
   merely re-keyed in the middle.

2. **Read-path traversal is kind-agnostic; the kind-filter only hides, never severs.** The
   recursive-CTE traversal in [`analyze.py`](../../src/sqlcg/cli/commands/analyze.py)
   (`_upstream_sql` / `_downstream_sql`) recurses over **every** COLUMN_LINEAGE edge with no
   kind predicate inside the recursion; the `kind IN ('table','external')` filter is applied
   **only to the final result rows**. So an intermediate `kind='temp'` node is traversed
   *through* and simply not listed — exactly the "collapse onto the correct tables" behaviour.
   Because `temp` (like `cte`/`derived`) is absent from the include-list, the default trace
   hides the temp but still connects `real_src` to `real_final`. **No change to the recursion
   is needed** — the temp inherits the same correct behaviour CTEs already have. The same is
   true for `tools.py` impact analysis, which explicitly **contracts** synthetic bridges
   (`producer -> cte -> consumer`); adding `'temp'` to `_SYNTHETIC_TABLE_KINDS` (Step 4.2) keeps
   the temp contracted as a bridge rather than dropped as a dead end.

The failure mode to guard against is therefore **not** the kind value itself (traversal is
kind-agnostic) but a **key mismatch** at the temp endpoint within a file. The integration test
in (1) and the USE-SCHEMA parity test below are the guards.

### USE-SCHEMA interaction (verified)

`_qualify_bare_tables` qualifies bare names to `<schema>.<name>` before parse. Because both
the CREATE target and the reads are qualified to the same `ba.tmp_base`, registering the temp
identity as the **post-qualification** `db.name` makes target and reads match. The temp's
identity is captured AFTER `_qualify_bare_tables` has run (the parser sees `ba.tmp_base`
already). Document this ordering in the implementation; add a unit test with an explicit
`USE SCHEMA ba;` + `create or replace temporary table tmp_base …` + later `select … from
tmp_base` to prove target and read share the namespaced key.

### Indexer target-node kind (load-bearing fix)

[`indexer.py:1287-1296`](../../src/sqlcg/indexer/indexer.py) emits the `defined_tables` target
row with a **hardcoded `"kind": "table"`**, ignoring `table.role`. This must become
`"kind": table.role` (so a `role="temp"` target produces `kind='temp'`). Confirm this does not
regress non-temp defined tables: their `role` is the default `"table"`, so the emitted kind is
unchanged for them. Source-table rows already use `src_table.role`
([`indexer.py:1364`](../../src/sqlcg/indexer/indexer.py)), so the namespaced temp source rows
get `kind='temp'` automatically once stamped.

The duplicate-DDL registry ([`indexer.py:1272-1285`](../../src/sqlcg/indexer/indexer.py)) keys
off `table.full_id`. Once temps are namespaced per file, each temp's `full_id` is unique, so
the spurious `duplicate_ddl:ba.tmp_base:already_in:<other file>` warnings for temps disappear
as a side effect (note this in the PR; no separate change).

### Cross-batch kind protection (Amendment 1 — BLOCKER fix, load-bearing)

**The single most important amendment.** `kind='table'` propagation to the target node (above)
is NOT sufficient on its own, because the persisted `kind='temp'` can be silently overwritten
across batch boundaries. The `upsert_nodes_bulk` ON CONFLICT guard
([`duckdb_backend.py:470-477`](../../src/sqlcg/core/duckdb_backend.py)) currently is:

```sql
"kind" = CASE
  WHEN "SqlTable"."kind" IN ('table', 'view') AND EXCLUDED."kind" = 'derived'
  THEN "SqlTable"."kind"
  ELSE EXCLUDED."kind" END
```

This protects only `('table','view')` from a `'derived'` downgrade. A committed `kind='temp'`
row is in the unguarded `ELSE EXCLUDED."kind"` path: if a **later batch** processes a file that
references `ba.tmp_base` as a source (emitting `kind='temp'` — fine) OR as a `star_source`
(emitting hardcoded `kind='table'` — see Warning 3 below) OR if a `kind='table'` reference row
for the same key arrives for any reason, `ELSE EXCLUDED."kind"` fires and **overwrites `'temp'`
with `'table'`, silently re-fusing the node** — defeating the entire feature. The in-batch
`_flush_row_batch` dedup (Rule 1 prefers `defined_in_file`) protects WITHIN a batch, but the
cross-batch ON CONFLICT is the gap.

**Required change**: extend the ON CONFLICT kind guard so `'temp'` is a protected stored kind
that only another `'temp'` may overwrite:

```sql
"kind" = CASE
  WHEN "SqlTable"."kind" IN ('table', 'view') AND EXCLUDED."kind" = 'derived'
  THEN "SqlTable"."kind"
  WHEN "SqlTable"."kind" = 'temp' AND EXCLUDED."kind" <> 'temp'
  THEN "SqlTable"."kind"
  ELSE EXCLUDED."kind" END
```

Rationale for the `<> 'temp'` form (not an inclusion list): a temp's identity is per-file and
its only legitimate writer is its own defining file; nothing else should ever re-kind it. This
also covers the star-source path (Warning 3) without a separate parser change. **This is the
sole cross-batch mechanism preserving `kind='temp'`** — analogous to how `WHERE kind='derived'`
is the sole precedence mechanism in `upgrade_derived_to_table_for_keys`. Grep-confirm the
edited CASE is the one reached for `label == NodeLabel.TABLE`.

**Symmetric direction is structurally impossible (Amendment 6).** The new arm only guards the
`Stored='temp'` direction. The reverse — `Stored='table', EXCLUDED='temp'` — would fall into the
`ELSE EXCLUDED."kind"` branch and overwrite a real `kind='table'` row with `'temp'` **IF** the
two rows ever shared a `qualified` PK. They never do: a `role="temp"` `TableRef` produces a
namespaced key (`<rel>::ba.tmp_base`), while a physical table named `tmp_base` produces the bare
key (`ba.tmp_base`). Because ON CONFLICT matches on `qualified` (the PK), a temp row and a
physical-table row are **different PKs and can never conflict**. The one invariant that makes
this hold is the `self._current_file_namespace is not None` guard in Step 1.2: a CREATE
TEMPORARY parsed without a namespace is left as `role="table"` and emits **no `kind='temp'` row
at all** (so a bare-key `EXCLUDED='temp'` row a buggy parser might produce simply never arises
from the correct implementation). No additional CASE arm is needed for the symmetric direction;
this paragraph documents *why*.

> **Note on `_upgrade_kinds`** ([`indexer.py:201-203`](../../src/sqlcg/indexer/indexer.py)):
> `temp` is intentionally NOT added to `_upgrade_kinds = {"cte","external"}`. The in-batch
> defined-target row always carries `defined_in_file`, so Rule 1 already wins in-batch; the
> cross-batch ON CONFLICT change above is what closes the real gap. Do not add `temp` to
> `_upgrade_kinds` (it would let a temp source-ref upgrade an unrelated real `kind='table'`).

### Consumer audit (A3 — change/no-change + justification)

| Consumer | Location | Decision | Justification |
|----------|----------|----------|---------------|
| Indexer target-node kind | [`indexer.py:1293`](../../src/sqlcg/indexer/indexer.py) | **CHANGE** → `table.role` | Sole place that hardcodes `"table"` for defined targets; must propagate `role="temp"`. |
| **`upsert_nodes_bulk` ON CONFLICT kind guard** | [`duckdb_backend.py:470-477`](../../src/sqlcg/core/duckdb_backend.py) | **CHANGE (Amendment 1, BLOCKER)** | Current guard protects only `('table','view')` from `'derived'`; a committed `kind='temp'` row falls in the unguarded `ELSE EXCLUDED."kind"` path and is silently re-fused to `'table'` by a later-batch reference/star-source row. Add a `WHEN "SqlTable"."kind"='temp' AND EXCLUDED."kind"<>'temp' THEN "SqlTable"."kind"` arm (see §Cross-batch kind protection). Sole cross-batch mechanism preserving `temp`. |
| Source-node kind | [`indexer.py:1364`](../../src/sqlcg/indexer/indexer.py) | no change | Already `src_table.role`; temp sources flow through. |
| **`star_sources` emit (hardcoded kind)** | [`indexer.py:1459`](../../src/sqlcg/indexer/indexer.py) | **no change IF Amendment 1 applied (Warning 3)** | Emits hardcoded `"kind":"table"` for star sources. If a temp is ever `SELECT *`'d, this would emit a `kind='table'` row for the temp's key. The Amendment-1 ON CONFLICT guard (`temp` only overwritten by `temp`) prevents this row from re-fusing a committed `kind='temp'` node, so **no parser/indexer change here is needed** — but ONLY because Amendment 1 covers it. Do NOT add star-source kind logic; rely on the guard. Add a test: a star-source row for a temp key does not flip the stored `kind='temp'`. |
| **`_upgrade_kinds` in-batch dedup** | [`indexer.py:201-203`](../../src/sqlcg/indexer/indexer.py) | no change (do NOT add `temp`) | In-batch, Rule 1 (`defined_in_file` wins) already keeps the temp target row; adding `temp` to `_upgrade_kinds` would wrongly let a temp source-ref upgrade an unrelated real `kind='table'`. The cross-batch gap is closed by Amendment 1, not here. |
| `_harvest_usage_catalog` kind-guard + `::` skip | [`indexer.py:128-143`](../../src/sqlcg/indexer/indexer.py) | no change | Guards on `kind in _USAGE_ELIGIBLE_KINDS = {"table","view"}` — `temp` is excluded; the `::` skip also catches namespaced temp keys. Both guards already exclude temps. Add a round-trip test. |
| `_Q_CTE_COLLISIONS` | [`coverage.py:244-257`](../../src/sqlcg/cli/coverage.py) | no change (verify) | Counts dst keys whose table `kind IN ('cte','derived')`. `temp` is **not** in that set, so temps never count as collisions. **This is the desired outcome**: zero multi-writer `kind='temp'` keys, and they do not pollute the CTE-collision counter. Confirm the query is unchanged and add a test asserting a namespaced temp does not register as a CTE collision. |
| Scoped-health exclusion | [`coverage.py:84,156,328`](../../src/sqlcg/cli/coverage.py) | **DECISION: add `'temp'`** to the `kind IN ('cte','derived')` exclusion sets | Scoped health excludes synthetic intermediates from the strict denominator. A session temp is a non-shared intermediate, same conceptual class. Adding `'temp'` keeps scoped health honest (the temp endpoints are intra-file plumbing, not durable lineage targets). **Document the denominator change** (the ~2,607 temp edges move from strict-eligible to scoped-excluded). Update the label text to "excl. CTE/derived/temp". |
| `noise_filter.py` `::` split | [`noise_filter.py:27-28`](../../src/sqlcg/server/noise_filter.py) | no change | `"::" in key` → `rsplit("::",1)[-1]` is format-agnostic; a `<rel>::ba.tmp_base` key returns `ba.tmp_base`. Works as-is. Add a test for the temp key form. |
| `tools.py` synthetic-kind set | [`tools.py:395`](../../src/sqlcg/server/tools.py) `_SYNTHETIC_TABLE_KINDS = {"cte","derived"}` | **DECISION: add `'temp'`** | Impact analysis excludes synthetic intermediates from the affected-table set and contracts them as bridges (`producer -> cte -> consumer`). A temp is exactly such a bridge — including it keeps impact analysis from reporting per-file session temps as durable affected tables. Add `'temp'` to the frozenset. |
| `analyze.py` kind filter | [`analyze.py:35,87`](../../src/sqlcg/cli/commands/analyze.py) `kind IN ('table','external')` | **DECISION: no change to the include-list** (temp stays excluded by default) | The default trace excludes anything not in `('table','external')`, so `temp` is excluded by default (correct — same as `cte`/`derived`). `--include-intermediate` flips the filter off entirely (no kind WHERE), so temps appear under that flag like CTEs do. No code change; document that `temp` behaves like `cte`/`derived` here. |
| Catalog kind-upgrade | [`duckdb_backend.py:525-558`](../../src/sqlcg/core/duckdb_backend.py) `upgrade_derived_to_table_for_keys` | no change | The `WHERE kind='derived'` guard means it only upgrades `derived`→`table`; `temp` is untouched. **This is required** — a catalogued `ba.tmp_base` must NOT be upgraded to `table` (it is per-file session-scoped; upgrading would re-fuse it). Add a test asserting a `kind='temp'` row is not upgraded by either catalog seam. |
| `gain` §G render | [`gain.py`](../../src/sqlcg/cli/commands/gain.py) via `render_coverage_lines` | inherits scoped-label change | The scoped-health line text changes to "excl. CTE/derived/temp" through `coverage.py`. No separate gain.py edit unless §G renders a temp-specific count (optional polish — not required). |
| ANSI parser | [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py) | **CHANGE** (apply the same stamping) | `exp.TemporaryProperty` is dialect-generic; `CREATE TEMPORARY TABLE` in the ANSI path must be namespaced identically. The stamping logic lives in `_parse_statement` / `_lineage_node_to_edges`, both shared via `AnsiParser` base — implement once, inherited by Snowflake. |

## Implementation Steps

### Phase 1: Parser — detect + stamp the temp target

**Step 1.1**: Add `self._current_file_temp_keys: set[str]` init + reset alongside
`self._current_file_namespace` in both parsers (init at construction). Also add the shared
`_temp_identity(db, name)` helper (Amendment 3). Reset/clear `self._current_file_temp_keys` at
**every** namespace assignment site — the file-start assignments AND the end-of-file clears —
in BOTH parsers, including the Snowflake `_parse_scripting_file` reset at
[`snowflake_parser.py:561`](../../src/sqlcg/parsers/snowflake_parser.py) (Warning 4).
- Files: [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py),
  [`snowflake_parser.py`](../../src/sqlcg/parsers/snowflake_parser.py),
  [`base.py`](../../src/sqlcg/parsers/base.py) (the `_temp_identity` helper, on the shared base).
- Acceptance: a fresh file parse starts with an empty temp-key set; parsing two files in
  sequence (including via the scripting path) does not leak temp keys from file A into file B
  (unit test: parse file A with a temp, then file B without; assert B's refs carry no
  namespace). `_temp_identity("ba","TMP_X") == "ba.tmp_x"` and `_temp_identity(None,"tmp_x")
  == "tmp_x"` (the `db=None` ANSI path, Amendment 3).

**Step 1.2**: In `_parse_statement`, after `target = self._apply_table_alias(
_extract_target_table(stmt))`, when `stmt` is an `exp.Create` with
`stmt.find(exp.TemporaryProperty) is not None` and `target` and `self._current_file_namespace`
are set: `dataclasses.replace(target, role="temp", namespace=self._current_file_namespace)`
and register `self._temp_identity(target.db, target.name)` (Amendment 3 — NOT an inline
f-string) in `self._current_file_temp_keys`.
- Files: [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py).
- Acceptance: parsing `create or replace temporary table ba.tmp_base as select …` from path
  `etl/x.sql` under a known root yields a `target.full_id` of `etl/x.sql::ba.tmp_base` and
  `target.role == "temp"`. A non-temp `create table ba.real as select …` yields
  `ba.real` with `role == "table"` (unchanged).

### Phase 2: Parser — stamp same-file reads + lineage leaves

**Step 2.1 (Amendment 5 — exact placement)**: Insert the source stamping **after the
target-exclusion filter at [`ansi_parser.py:409`](../../src/sqlcg/parsers/ansi_parser.py) and
BEFORE the `_extract_column_lineage` call at line 422** (NOT after the dedup at ~460). Replace
any source `s` whose `self._temp_identity(s.db, s.name)` is in `self._current_file_temp_keys`
with a namespaced `role="temp"` ref via `dataclasses.replace(s, role="temp",
namespace=self._current_file_namespace)` (Amendment 3 — same helper). This guarantees the
stamped sources flow into `_extract_column_lineage`'s star-resolution path.
- Files: [`ansi_parser.py`](../../src/sqlcg/parsers/ansi_parser.py).
- Acceptance: in a file that creates `ba.tmp_base` then `insert into ba.final select … from
  ba.tmp_base`, the second statement's source ref for the temp has
  `full_id == "<rel>::ba.tmp_base"` and `role == "temp"`; the `ba.final` target is unchanged.
- Acceptance (star-source, Amendment 5): a file with `CREATE TEMPORARY TABLE ba.tmp AS SELECT *
  FROM real_src` produces a `star_sources` entry whose `source.full_id` is the **namespaced**
  temp key (`<rel>::ba.tmp`), never the bare `ba.tmp` — i.e. no orphaned bare-key `kind='table'`
  row appears for the temp.

**Step 2.2**: Extend the namespace-stamping branch in `_lineage_node_to_edges`
([`base.py:844-853`](../../src/sqlcg/parsers/base.py)) to also stamp leaf sources whose
`self._temp_identity(source.db, source.name)` is in `self._current_file_temp_keys` (Amendment 2
— exact same helper as Step 1.2/2.1; read `source.db`/`source.name` off the `exp.Table`).
Return `TableRef(catalog=source.catalog, db=source.db, name=source.name, role="temp",
namespace=self._current_file_namespace)`.
- Files: [`base.py`](../../src/sqlcg/parsers/base.py).
- Acceptance: COLUMN_LINEAGE edges whose source or dst is the temp carry the namespaced
  `<rel>::ba.tmp_base.<col>` key on the temp endpoint; the non-temp endpoint is unchanged. A
  dedicated unit test parses an **ANSI** `CREATE TEMPORARY TABLE tmp_x AS …` (no schema
  qualification, `db=None`) and asserts the leaf source in `_lineage_node_to_edges` is stamped
  `role="temp"` with the bare `tmp_x` identity matching the registration (Amendment 2).

### Phase 3: Indexer + backend — propagate and protect the temp kind

**Step 3.1**: Change [`indexer.py:1293`](../../src/sqlcg/indexer/indexer.py) `"kind": "table"`
→ `"kind": table.role`.
- Files: [`indexer.py`](../../src/sqlcg/indexer/indexer.py).
- Acceptance: a defined temp produces a `SqlTable` row with `kind='temp'`; a defined non-temp
  table still produces `kind='table'`.

**Step 3.2 (Amendment 1, BLOCKER)**: Extend the `upsert_nodes_bulk` ON CONFLICT kind guard
([`duckdb_backend.py:470-477`](../../src/sqlcg/core/duckdb_backend.py)) so a committed
`kind='temp'` row is overwritten only by another `'temp'` row — add the
`WHEN "SqlTable"."kind"='temp' AND EXCLUDED."kind"<>'temp' THEN "SqlTable"."kind"` arm before
the `ELSE` (see §Cross-batch kind protection). Do NOT add `temp` to `_upgrade_kinds` in
[`indexer.py:202`](../../src/sqlcg/indexer/indexer.py).
- Files: [`duckdb_backend.py`](../../src/sqlcg/core/duckdb_backend.py).
- Acceptance (cross-batch, observable): commit a `kind='temp'` node in batch 1; in batch 2
  upsert a `kind='table'` (and separately a `kind='derived'`) reference row for the SAME
  qualified key; assert the stored `kind` remains `'temp'` after both. Counterpart: a real
  `kind='table'` node is NOT protected from a legitimate `kind='temp'`-free flow (a `kind='view'`
  DDL row still wins where it should) — assert the existing `('table','view')` vs `'derived'`
  guard is unchanged.

### Phase 4: Consumers — scoped-health + impact-analysis

**Step 4.1**: Add `'temp'` to the scoped-health exclusion `kind IN (...)` sets in
[`coverage.py`](../../src/sqlcg/cli/coverage.py) (lines 84, 156, 328) and update the rendered
label to "excl. CTE/derived/temp" (line ~624). Confirm `_Q_CTE_COLLISIONS` is **unchanged**
(temps deliberately do not count as CTE collisions).
- Acceptance: scoped-health denominator excludes `kind='temp'` dst keys; the rendered line
  reads "excl. CTE/derived/temp"; `_Q_CTE_COLLISIONS` count for temps is 0.

**Step 4.2**: Add `'temp'` to `_SYNTHETIC_TABLE_KINDS` in
[`tools.py:395`](../../src/sqlcg/server/tools.py).
- Acceptance: impact analysis excludes `kind='temp'` nodes from the affected-table set and
  contracts them as bridges (the existing cte/derived behaviour, now covering temp).

### Phase 5: Version + lock

**Step 5.1**: Bump `version` to `1.21.0` in
[`pyproject.toml`](../../pyproject.toml) + [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py);
run `uv lock`. Minor bump (new `kind='temp'` surface; nothing breaks).

## Test Strategy

Tests must assert **observable output** (graph rows, key strings, rendered lines, query
results), never "no exception raised". Name tests `test_<unit>_<scenario>_<expected>` and link
this plan in the docstring.

- **Unit (parser)** — detecting the TEMPORARY property on a CREATE produces a `role="temp"`,
  repo-relative-namespaced target; a non-temp CREATE is unchanged. Verifies the trigger is the
  AST property, not the name (include a `tmp_`-named *non-temporary* CREATE that stays
  `kind='table'`, and a non-`tmp_`-named TEMPORARY table that becomes `kind='temp'`).
- **Unit (parser)** — two different files each defining `ba.tmp_base` produce **distinct**
  namespaced keys; a temp key from file A does not leak into file B's refs.
- **Unit (parser, USE-SCHEMA)** — `USE SCHEMA ba;` + temp CREATE + later read resolve the
  CREATE target and the read to the **same** namespaced `<rel>::ba.tmp_base` key.
- **Unit (parser, intra-file lineage)** — a temp written then read within one file yields
  COLUMN_LINEAGE edges whose temp endpoint carries the namespaced key (edges re-keyed, present,
  not dropped).
- **Unit (portability)** — same relative layout under two absolute roots yields identical
  namespaced temp keys (mirrors PR #86's portability assertion).
- **Integration (indexer)** — index a small corpus with two files each creating `ba.tmp_base`;
  assert **two** `kind='temp'` `SqlTable` rows with distinct namespaced keys, **no** single
  fused `ba.tmp_base` `kind='table'` node, and that a separately-indexed genuinely-shared
  physical table (two writers, no TEMPORARY property) remains **one** `kind='table'` node.
- **Integration (lineage collapse through temp — user-flagged correctness gate)** — index a
  file with `real_src → ba.tmp_base → real_final` (temp written from a real source, then a real
  sink reads the temp). Assert the upstream trace from `real_final` reaches `real_src` (chain
  intact end-to-end), the temp endpoint is re-keyed `<rel>::ba.tmp_base` on **both** the
  produce and consume edges (single identity), and the default (non-`--include-intermediate`)
  trace hides the temp from the listed results while still connecting source to sink.
- **Unit (parser, ANSI bare temp — Amendment 2/3)** — an ANSI `CREATE TEMPORARY TABLE tmp_x AS
  SELECT …` (no USE-SCHEMA, `db=None`) registers the bare `tmp_x` identity; the target full_id
  is `<rel>::tmp_x` and `role="temp"`; a later read of `tmp_x` in the same file is stamped to
  the same key (no `none.tmp_x` mis-key).
- **Integration (cross-batch kind protection — Amendment 1, BLOCKER gate)** — commit a
  `kind='temp'` `SqlTable` node in batch 1, then in batch 2 upsert a `kind='table'` reference
  row (and separately a `kind='derived'` row) for the SAME qualified key via
  `upsert_nodes_bulk`; assert the stored `kind` is still `'temp'` after each. Regression
  counterpart: the existing `('table','view')` vs `'derived'` guard still holds (a `derived`
  row does not downgrade a committed `table` row).
- **Integration (star-source does not re-fuse — Warning 3)** — a `star_sources` row
  (hardcoded `kind='table'`) for a temp key, processed after the temp's defined-target row,
  leaves the stored `kind='temp'` intact (relies on Amendment 1; no parser change).
- **Integration (consumer round-trips)** — namespaced temp keys are excluded from
  `_harvest_usage_catalog` HAS_COLUMN(source='usage'); a `kind='temp'` row is NOT upgraded by
  `upgrade_derived_to_table_for_keys` via **both** catalog seams (`catalog load` command +
  `_reapply_catalog_if_configured`); a temp does not register in `_Q_CTE_COLLISIONS`.
- **Integration (coverage)** — scoped-health denominator excludes `kind='temp'`; rendered line
  reads "excl. CTE/derived/temp".
- **Behavioural perf** — the four perf-guard suites pass **unmodified**:
  [`test_perf_scaling_guard.py`](../../tests/unit/test_perf_scaling_guard.py),
  [`test_bulk_upsert_invariant.py`](../../tests/unit/test_bulk_upsert_invariant.py),
  [`test_upsert_batch_invariant.py`](../../tests/unit/test_upsert_batch_invariant.py),
  [`test_T09_01_qualify_once.py`](../../tests/unit/test_T09_01_qualify_once.py). A new
  assertion in the scaling guard is optional; the temp check adds no per-column op, so a red
  here means an unrelated invariant broke.

## Acceptance Criteria

- [ ] A CREATE carrying the `exp.TemporaryProperty` produces a `role="temp"`,
      repo-relative-namespaced `TableRef`; detection is by AST property, never by name.
- [ ] On a re-indexed DWH, the single shared `ba.tmp_base` `kind='table'` node is **GONE**;
      it is replaced by **13** per-file `kind='temp'` nodes keyed `<rel-path>::ba.tmp_base`.
- [ ] **Zero** multi-writer `kind='temp'` keys exist in the graph (every temp node has exactly
      one defining file).
- [ ] The ~2,607 affected COLUMN_LINEAGE edges are **re-keyed, not dropped** — intra-file
      lineage through every temp survives (edge count for those endpoints preserved, only the
      temp endpoint key changes).
- [ ] **Lineage collapses correctly through temps (user-flagged):** a `real_src → tmp →
      real_final` chain is traversable end-to-end after re-keying — an upstream trace from
      `real_final` reaches `real_src`, with the temp re-keyed (not severed) in the middle. The
      temp's CREATE-target key and its same-file read key are byte-identical (single
      namespaced identity per file).
- [ ] **Cross-batch protection (Amendment 1):** a committed `kind='temp'` node is NOT
      overwritten to `kind='table'`/`'derived'` by a later-batch reference or star-source row
      for the same key (the `upsert_nodes_bulk` ON CONFLICT `temp`-arm holds); the existing
      `('table','view')` vs `'derived'` guard is unchanged.
- [ ] Non-temporary multi-writer tables (the `368 − 85` set) are **byte-identical**
      before/after (same `qualified`, `kind='table'`, same node count).
- [ ] Scoped-health denominator change documented; rendered line reads "excl. CTE/derived/temp".
- [ ] `_Q_CTE_COLLISIONS` does not count temps; `upgrade_derived_to_table_for_keys` does not
      upgrade `kind='temp'`.
- [ ] No `SCHEMA_VERSION` bump (kind is a value); re-index documented as the migration.
- [ ] ANSI `CREATE TEMPORARY TABLE` is namespaced identically to the Snowflake path.
- [ ] Version bumped to 1.21.0 + `uv lock`.
- [ ] All four perf-guard suites pass unmodified.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Name-heuristic creep (treating `tmp_*` as temp) | Trigger is `stmt.find(exp.TemporaryProperty)` only; a test asserts a `tmp_`-named non-temporary CREATE stays `kind='table'` and a non-`tmp_` TEMPORARY table becomes `kind='temp'`. |
| Temp read in a later statement not stamped (key mismatch) | `_current_file_temp_keys` persists across statements within a file; Step 2.1/2.2 stamp sources and lineage leaves; USE-SCHEMA test proves target/read parity. |
| USE-SCHEMA qualification breaks the identity match | Identity is captured AFTER `_qualify_bare_tables` (post-qualification `db.name`); both target and reads are qualified identically. Explicit test. |
| Re-fusing via catalog kind-upgrade | `upgrade_derived_to_table_for_keys` only touches `kind='derived'`; `temp` is untouched. Test on both catalog seams. |
| **Cross-batch re-fusion (A1)**: a later-batch `kind='table'`/`'derived'`/star-source row silently overwrites a committed `kind='temp'` via the unguarded `ELSE EXCLUDED."kind"` ON CONFLICT path | Step 3.2 adds a `temp`-protection arm (`temp` only overwritten by `temp`); cross-batch integration test asserts the stored kind survives both a `table` and a `derived` reference row. **Without this, the whole feature is defeated by batch ordering.** |
| ANSI bare temp mis-keyed as `none.tmp_x` (A3) | Shared `_temp_identity` helper drops the db segment when `db` is falsy; ANSI bare-temp unit test asserts the `<rel>::tmp_x` key (no `none.` prefix). |
| Scoped-health denominator shift looks like a regression | Documented as an intentional honesty change (temp endpoints are intra-file plumbing); the `~2,607` edges move from strict-eligible to scoped-excluded; label updated. |
| Perf regression from per-CREATE property read | `find(exp.TemporaryProperty)` is once-per-CREATE; source/leaf stamping is O(1) set membership; no per-column qualify/scope/expand. Perf guards pass unmodified. |
| Temp-key set leaks across files | Reset at the namespace-assignment sites + cleared at end-of-file; isolation unit test. |

## User verification queue

Per house rule, the agent never tags releases. **On hold (pending user verification):**
v1.15.0–v1.20.0 (prior sprints). **This sprint adds:**
- **v1.21.0** — per-file TEMPORARY-table namespacing (`kind='temp'`).

After this merges and the DWH is re-indexed, **regenerate `table_graph.html`** to see the
de-fused graph (the single `ba.tmp_base` hub split into 13 per-file nodes). Version bumped in
[`pyproject.toml`](../../pyproject.toml) + [`src/sqlcg/__init__.py`](../../src/sqlcg/__init__.py)
+ `uv lock` in the feature branch before merge; tagged annotated `v1.21.0` on the master merge
commit per CLAUDE.md.
