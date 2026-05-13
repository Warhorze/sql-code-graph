# Feature Plan: Fix `exp.expand` / `as_sources_dict` / `SET TAG` Performance Regression

## Summary

Three targeted fixes that collectively bring per-file parse time from 16–106 s back
under 2 s on the DWH corpus. The regression was introduced when schema-CSV sources
started flowing into `exp.expand()` and `as_sources_dict()` and an additional
Snowflake DDL form (`ALTER … SET TAG`) started appearing in profiled files.

## Scope

### In Scope

- [`src/sqlcg/parsers/base.py`](../src/sqlcg/parsers/base.py) — stop passing
  schema-derived sources into `exp.expand()`.
- [`src/sqlcg/parsers/ansi_parser.py`](../src/sqlcg/parsers/ansi_parser.py) —
  accept an optional `dependency_filter` and use it to filter `xfile_sources`
  before they reach `sources_map` / `exp.expand()`.
- [`src/sqlcg/parsers/snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py)
  — thread `dependency_filter` through to `AnsiParser.parse_file`; strip
  `ALTER TABLE/VIEW … MODIFY COLUMN … SET TAG …;` statements.
- [`src/sqlcg/lineage/aggregator.py`](../src/sqlcg/lineage/aggregator.py) —
  compute the pass-1 referenced-table name set and pass it to
  `parser.parse_file(...)` as `dependency_filter`.
- [`src/sqlcg/lineage/schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py)
  — add a real cache for `as_sources_dict()`.

### Non-Goals

- Replicating SQLMesh's full `mapping_schema` pipeline.
- Pre-filtering the **schema** (mapping_schema) by `depends_on` — only the
  `xfile_sources` (cross-file CTAS bodies) get a per-file dependency filter in
  this plan. Schema CSV entries are still passed in full to
  `sg_lineage(sources=…)` via `schema_sources`.
- Switching `as_sources_dict()` to lazy per-table parsing (kept eager because
  the cache makes the cost a one-time hit per indexing job).
- Any change to `sg_lineage(sources=…)` semantics — schema_sources continue to
  feed sg_lineage for column resolution.
- Mutating `schema_resolver._cross_file_sources` per file (Option B). Ruled
  out because the resolver is shared across parse calls and mutation would
  not be thread-safe or composable with the `as_sources_dict()` cache.

## Design

### Fix A — Per-file dependency filtering of `xfile_sources` + stop expanding schema_sources

Two distinct O(N) sources of bloat reach `exp.expand()` and `sources_map`
today:

1. **schema_sources** (~10 000 synthetic CSV entries) — passed into
   `combined_sources` and then handed to `exp.expand()`. Resolved by
   excluding `schema_sources` from the dict given to `exp.expand()` (see Step
   A.1 below). `qualify()` keeps using `mapping_schema` directly, and
   `sg_lineage()` keeps receiving the full `combined_sources` via its
   `sources=` kwarg, so no column resolution regresses.

2. **`xfile_sources`** (cross-file CTAS bodies) — copied wholesale into
   `sources_map` for every file in
   [`ansi_parser.py:81`](../src/sqlcg/parsers/ansi_parser.py)
   (`xfile_sources = dict(self._schema.cross_file_sources())`). With 20–30 %
   of 1 445 files being CTAS, this dict reaches 300–450 entries, and every
   entry flows into `exp.expand()` via `sources_map`, producing
   `O(N_files × N_xfile)` behaviour. Resolved by filtering `xfile_sources`
   against the **pass-1 referenced-tables set** of the file currently being
   re-parsed.

The new contract:

| Dict | Consumer | After fix |
|---|---|---|
| `xfile_sources` (seed of `sources_map`) | `exp.expand()` via `sources_map` | Filtered to `{name : body for name, body in xfile_sources.items() if name in dependency_filter}` — typically 5–20 entries per file |
| `sources_to_expand` | `exp.expand()` | **CTAS / CTE / temp bodies only** (`sources` arg, ≤ ~10 entries) — no schema_sources |
| `combined_sources` | `sg_lineage(sources=…)` | unchanged — still `{**sources, **schema_sources}` |

#### Wiring

`CrossFileAggregator.resolve_pass2` is the only call site that holds both
the full pass-1 `ParsedFile` (with populated `referenced_tables`) and the
`parser`. The dependency set is computed there and threaded through:

```
aggregator.resolve_pass2(parser, parsed)
  └── ref_names = {t.name.lower() for t in parsed.referenced_tables if t.name}
  └── parser.parse_file(parsed.path, sql, dependency_filter=ref_names)
        └── SnowflakeParser.parse_file forwards dependency_filter
              └── AnsiParser.parse_file uses dependency_filter to filter xfile_sources
                    before seeding sources_map
```

Schema-side resolution during qualification continues to flow through
`mapping_schema` (passed to `qualify()` already, see
[`base.py:651-657`](../src/sqlcg/parsers/base.py)). `qualify()` does *not*
need `exp.expand()` to have inlined schema tables — it consults
`mapping_schema` directly. SQLMesh's pipeline confirms this is sufficient.

The default value of `dependency_filter` is `None`, meaning "no filter — use
all xfile_sources". This preserves pass-1 behaviour (where
`referenced_tables` is not yet populated) and direct test calls into
`parse_file(path, sql)` that do not go through the aggregator.

### Fix B — Cache `as_sources_dict()`

Field already exists at
[`schema_resolver.py:43`](../src/sqlcg/lineage/schema_resolver.py)
(`self._sources_cache: dict | None = None`) and invalidation is already wired
at four of the five mutation sites (lines 81, 91, 104, 155). Missing pieces:

1. The invalidation at the fifth site
   ([`schema_resolver.py:241`](../src/sqlcg/lineage/schema_resolver.py),
   end of `add_information_schema`) — currently only resets `_cache`.
2. The cache check inside `as_sources_dict()` itself
   ([`schema_resolver.py:272-319`](../src/sqlcg/lineage/schema_resolver.py)).

Cache returns the same `dict` reference (no `deepcopy`) — `exp.expand()` /
`sg_lineage()` treat sources as read-only AST handles, and `copy=True` on
`exp.expand` already guarantees the body itself is not mutated.

### Fix C — Strip `ALTER … SET TAG` statements

Same family as the existing Gap-4 fix at
[`snowflake_parser.py:124-130`](../src/sqlcg/parsers/snowflake_parser.py), but
matches a **full statement** instead of a column-suffix clause. sqlglot emits
these as `exp.Command` ("unsupported syntax") which both adds errors to the
warning stream and disturbs downstream statement classification.

Regex requirements:

- Match `ALTER\s+(TABLE|VIEW)` … up to the terminating `;` (or end-of-input).
- Allow quoted, dotted, space-bearing identifiers
  (e.g. `IA_SEMANTIC."Budget voorraad IGDC BIP"`).
- Allow multiple `MODIFY COLUMN … SET TAG …` clauses comma-chained inside one
  statement (Snowflake permits this).
- Case-insensitive.
- Do **not** match unrelated `ALTER TABLE … SET …` forms — anchor on
  `MODIFY\s+COLUMN` + `SET\s+TAG` to keep the strip narrow.

Proposed pattern (single line):
```
re.sub(
    r"ALTER\s+(?:TABLE|VIEW)\s+[^\s;]+(?:\s*\.\s*\"[^\"]+\")*"
    r"(?:\s+MODIFY\s+COLUMN\s+\"[^\"]+\"\s+SET\s+TAG\s+[^;]+?)+;?",
    "",
    sql,
    flags=re.IGNORECASE,
)
```
The implementer should adjust to handle the exact identifier forms seen in
the profiled files (see test fixtures in Test Strategy).

## Implementation Steps

### Phase 1 — Fix A (biggest win)

#### Step A.1 — Filter `sources_to_expand` to non-schema sources

In [`base.py`](../src/sqlcg/parsers/base.py) `_extract_column_lineage`, change
the `sources_to_expand` construction at lines 640–646 to iterate over
`sources` (the parameter), not `combined_sources`.

- Files affected: `src/sqlcg/parsers/base.py` (lines 638–649).
- Concretely: replace
  ```python
  if combined_sources:
      sources_to_expand = {
          k: v
          for k, v in combined_sources.items()
          if hasattr(v, "args")
      }
  ```
  with
  ```python
  if sources:
      sources_to_expand = {
          k: v
          for k, v in sources.items()
          if hasattr(v, "args")
      }
  else:
      sources_to_expand = {}
  ```
- Leave the `sg_kwargs = {"sources": combined_sources, ...}` call at line 798
  **unchanged** — schema_sources must still reach `sg_lineage`.
- Acceptance: the corpus test (see Test Strategy) drops below 2 s per file;
  no regression in existing column-lineage assertions.

#### Step A.2 — Inline comment explaining the exclusion

Add a comment block above the new `if sources:` branch explaining *why*
schema_sources are excluded (with a one-line reference to SQLMesh's
`mapping_schema` approach) so future readers don't re-introduce the
regression.

- Files affected: `src/sqlcg/parsers/base.py`.
- Acceptance: comment present, mentions both `exp.expand` cost and
  `mapping_schema`.

#### Step A.3 — Add `dependency_filter` parameter to `AnsiParser.parse_file`

In [`ansi_parser.py:35`](../src/sqlcg/parsers/ansi_parser.py), extend the
signature:

```python
def parse_file(
    self,
    path: Path,
    sql: str,
    dependency_filter: set[str] | None = None,
) -> ParsedFile:
```

Update the docstring to describe the new parameter:

> `dependency_filter`: optional set of lowercased table names. When provided,
> the cross-file sources seeded into `sources_map` are filtered to only those
> whose name is in the set. Pass-1 callers (and direct test callers) pass
> `None` to disable filtering; pass-2 callers
> (`CrossFileAggregator.resolve_pass2`) compute this from the pass-1
> `ParsedFile.referenced_tables`.

Then replace the seed block at lines 79–82:

```python
# Initialize sources_map to accumulate temp table definitions.
# Seed with cross-file CTAS bodies from pass 1 (intra-file overrides).
xfile_sources = dict(self._schema.cross_file_sources()) if self._schema else {}
sources_map: dict[str, Any] = xfile_sources
```

with:

```python
# Initialize sources_map to accumulate temp table definitions.
# Seed with cross-file CTAS bodies from pass 1 (intra-file overrides).
# When `dependency_filter` is provided (pass 2), keep only those CTAS bodies
# the current file actually references — keeps exp.expand O(N_refs) instead
# of O(N_corpus_ctas).
if self._schema:
    xfile_sources_all = self._schema.cross_file_sources()
    if dependency_filter is not None:
        xfile_sources = {
            name: body
            for name, body in xfile_sources_all.items()
            if name in dependency_filter
        }
    else:
        xfile_sources = dict(xfile_sources_all)
else:
    xfile_sources = {}
sources_map: dict[str, Any] = xfile_sources
```

- Files affected: `src/sqlcg/parsers/ansi_parser.py` (signature at line 35,
  docstring at lines 36–43, seed block at lines 79–82).
- Acceptance: with `dependency_filter={"foo"}` and `cross_file_sources()`
  returning `{"foo": ..., "bar": ...}`, `sources_map` after seeding contains
  only `"foo"`. With `dependency_filter=None`, both keys are present.

#### Step A.4 — Thread `dependency_filter` through `SnowflakeParser.parse_file`

In [`snowflake_parser.py:53`](../src/sqlcg/parsers/snowflake_parser.py),
extend the signature and forward the kwarg to both branches:

```python
def parse_file(
    self,
    path: Path,
    sql: str,
    dependency_filter: set[str] | None = None,
) -> ParsedFile:
    sql = self._preprocess_snowflake_sql(sql)
    if self._has_scripting_block(sql):
        logger.info("Snowflake scripting block detected in %s, using DML extraction", path)
        return self._parse_scripting_file(path, sql)
    return AnsiParser.parse_file(self, path, sql, dependency_filter=dependency_filter)
```

If `_parse_scripting_file` needs the filter too, follow up in the same step
— current scope passes it only on the AnsiParser path because scripting
blocks bypass `sources_map` seeding. Verify by reading
`_parse_scripting_file` and either add the parameter or document why it is
unnecessary in the docstring.

- Files affected: `src/sqlcg/parsers/snowflake_parser.py` (signature at line
  53, body at lines 63–72, possibly `_parse_scripting_file`).
- Acceptance: `SnowflakeParser.parse_file(path, sql, dependency_filter={...})`
  is callable without TypeError; the filter reaches
  `AnsiParser.parse_file`.

#### Step A.5 — Compute and pass `dependency_filter` from `resolve_pass2`

In [`aggregator.py:111`](../src/sqlcg/lineage/aggregator.py), replace:

```python
return parser.parse_file(parsed.path, sql)
```

with:

```python
ref_names = {
    (t.name or "").lower()
    for t in parsed.referenced_tables
    if t.name
}
return parser.parse_file(parsed.path, sql, dependency_filter=ref_names)
```

Add a one-line comment: `# Filter cross-file CTAS bodies to what this file
actually references — keeps exp.expand bounded by referenced_tables, not by
corpus size.`

- Files affected: `src/sqlcg/lineage/aggregator.py` (line 111 and a comment
  immediately above).
- Acceptance: re-parse on pass 2 for a file with 3 referenced tables yields
  a `sources_map` whose pre-loop size is at most 3 (verified via test T-A
  below).

### Phase 2 — Fix B (cache `as_sources_dict`)

**Step B.1**: Add cache invalidation at
[`schema_resolver.py:241`](../src/sqlcg/lineage/schema_resolver.py) — change
`self._cache = None` to `self._cache = None; self._sources_cache = None` to
match the convention used at lines 81, 91, 104, 155.

- Files affected: `src/sqlcg/lineage/schema_resolver.py` (line 241).
- Acceptance: grep confirms 5 invalidation sites for `_sources_cache`.

**Step B.2**: In `as_sources_dict()` at lines 272–319, wrap the body with a
cache check:

```python
with self._lock:
    if self._sources_cache is not None:
        return self._sources_cache
    result: dict[str, Any] = {}
    # ... existing build loop ...
    self._sources_cache = result
    return result
```

- Files affected: `src/sqlcg/lineage/schema_resolver.py` (lines 292–319).
- Return the dict by reference (no `deepcopy`); document this in the
  docstring.
- Acceptance: second call to `as_sources_dict()` on an unmutated resolver
  does **not** invoke `sqlglot.parse_one` (verifiable via `mock.patch` or by
  asserting object identity of the returned dict).

**Step B.3**: Update the `as_sources_dict()` docstring to:
1. Note that the result is cached.
2. Note that the caller must not mutate the returned dict.

- Files affected: `src/sqlcg/lineage/schema_resolver.py` (docstring at line
  272).

### Phase 3 — Fix C (strip `ALTER … SET TAG`)

**Step C.1**: In `_preprocess_snowflake_sql` at
[`snowflake_parser.py:75-132`](../src/sqlcg/parsers/snowflake_parser.py), add a
new clause **after** the existing Gap-4 (`WITH TAG`) block:

```python
# Gap 4b: Strip ALTER TABLE/VIEW ... MODIFY COLUMN ... SET TAG ...; statements.
# sqlglot emits these as exp.Command (unsupported syntax). They only carry tag
# metadata we don't model, so removing them is safe and cleans the error stream.
if "SET TAG" in sql.upper():
    sql = re.sub(
        r"ALTER\s+(?:TABLE|VIEW)\s+[^;]*?MODIFY\s+COLUMN[^;]*?SET\s+TAG[^;]*?;",
        "",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
```

- Files affected: `src/sqlcg/parsers/snowflake_parser.py` (insert ~line 131).
- Update the method docstring (lines 78–81) to list "Gap 4b" with one line.
- Acceptance: parsing the fixture below yields zero "unsupported syntax"
  warnings for the ALTER form.

**Step C.2**: Update the module-level docstring header at
[`snowflake_parser.py:40`](../src/sqlcg/parsers/snowflake_parser.py) to include
"ALTER … SET TAG stripping (Gap 4b)".

## Test Strategy

### Unit tests

**T-A1** — schema_sources excluded from expand:
`tests/unit/test_expand_excludes_schema_sources.py`
- Build a `_extract_column_lineage` call with `sources={"cte_a": <Select>}`
  and `schema_sources={"big_table": <Select>}` where `big_table` has many
  columns.
- Patch `sqlglot.expressions.expand` and assert it is called with a dict
  whose keys are `{"cte_a"}` only (no `"big_table"`).
- Assert the resulting `combined_sources` passed to `sg_lineage` still
  contains both keys (i.e. observable from emitted lineage edges).

**T-A2** — `dependency_filter` filters cross-file sources:
`tests/unit/test_parse_file_dependency_filter.py`
- Construct a `SchemaResolver` and register two cross-file sources via
  `register_cross_file_sources({"foo": <Select>, "bar": <Select>})`.
- Call `AnsiParser.parse_file(path, sql, dependency_filter={"foo"})` on a
  trivial SQL that references neither (so no further `sources_map` mutation
  happens).
- Patch `sqlglot.expressions.expand` and inspect the `sources` kwarg of its
  first call: assert `"foo"` is present and `"bar"` is **absent**.
- Repeat with `dependency_filter=None` and assert both keys reach
  `exp.expand`.
- Repeat with `dependency_filter=set()` and assert neither cross-file key
  reaches `exp.expand`.

**T-A3** — aggregator computes filter from `referenced_tables`:
`tests/unit/test_resolve_pass2_passes_dependency_filter.py`
- Construct a `ParsedFile` with `referenced_tables=[Table(name="Foo"),
  Table(name="BAR")]`.
- Patch `parser.parse_file` and call `aggregator.resolve_pass2(parser,
  parsed)`.
- Assert `parse_file` was called with `dependency_filter={"foo", "bar"}`
  (lowercased).

**T-B1**: `tests/unit/test_schema_resolver_caches_sources.py`
- Build a resolver, load a small CSV via `add_information_schema`.
- Call `as_sources_dict()` twice; assert object identity (`is`) of the
  returned dicts.
- Patch `sqlglot.parse_one` and assert it is called the first time but not
  the second.

**T-B2**: `tests/unit/test_schema_resolver_invalidates_sources_cache.py`
- For each of: `add_create_table`, `add_view_sources`,
  `register_cross_file_sources`, `add_dbt_manifest`, `add_information_schema`
  — call `as_sources_dict()`, mutate via the method, call again, assert the
  returned dict is **not** the same object (identity changed) and reflects
  the mutation.

**T-C**: `tests/unit/test_snowflake_strip_alter_set_tag.py`
- Fixture: the literal statement
  `ALTER VIEW IA_SEMANTIC."Budget voorraad IGDC BIP" MODIFY COLUMN "Week koppelcode" SET TAG IA_SEMANTIC.weekcode='wk';`
- Assert `_preprocess_snowflake_sql` removes it entirely.
- Assert `SnowflakeParser.parse()` on a buffer containing one `CREATE VIEW`
  followed by two such ALTER statements produces:
  - exactly one `CREATE VIEW` statement parsed,
  - zero `exp.Command` nodes in the AST list,
  - zero errors mentioning "unsupported syntax" / "Command".

### Integration / performance test

**T-Perf**: `tests/integration/test_parse_perf_budget.py`
- Parse the two profiled files (the user provided
  `BUDGET_VOORRAAD_IGDC_BIP.sql` and `wtfi_cbs_boodschappenmandje.sql`)
  through `SnowflakeParser.parse()` using a `SchemaResolver` populated from
  the production schema CSV.
- Assert wall time per file `< 2.0 s` (measured via `time.perf_counter` in
  the test, not `pytest-benchmark`).
- Mark with `@pytest.mark.perf` and skip when the fixtures are absent so the
  test suite stays green on developer machines without the corpus.

### Verification that Fix A doesn't regress lineage

- Run the full `tests/unit/test_T09_01_qualify_once.py` and existing
  schema-resolver column-lineage tests. They already assert that a column
  alias such as `SELECT a.x AS y FROM cross_schema_table a` resolves to the
  right source through `sg_lineage(sources=…)` — proving schema_sources
  reach lineage resolution via `sg_lineage`, not via `exp.expand`.

## Acceptance Criteria

- [ ] `exp.expand()` is called with only CTAS / CTE / temp sources (no
      schema_sources entries).
- [ ] `combined_sources` (with schema_sources) is still passed to
      `sg_lineage(sources=…)` (verified by an existing or new test that
      resolves a column through a schema-only table).
- [ ] `AnsiParser.parse_file` and `SnowflakeParser.parse_file` accept an
      optional `dependency_filter: set[str] | None = None` parameter; the
      default value preserves pass-1 / direct-call behaviour.
- [ ] `CrossFileAggregator.resolve_pass2` computes
      `ref_names = {t.name.lower() for t in parsed.referenced_tables if
      t.name}` and passes it as `dependency_filter` to `parser.parse_file`.
- [ ] When `dependency_filter` is provided, the `sources_map` seeded into
      `_parse_statement` contains only the cross-file CTAS bodies whose name
      is in the filter (verified by T-A2).
- [ ] `sources_to_expand` never exceeds `len(referenced_tables)` of the
      current file during pass 2.
- [ ] `as_sources_dict()` returns a cached dict by reference on the second
      call without re-parsing.
- [ ] All five resolver mutation sites invalidate `_sources_cache`.
- [ ] `_preprocess_snowflake_sql` strips standalone `ALTER … SET TAG …;`
      statements and zero `exp.Command` nodes survive parsing of the
      fixture.
- [ ] `BUDGET_VOORRAAD_IGDC_BIP.sql` parses in under 2 s with the schema
      CSV loaded.
- [ ] `wtfi_cbs_boodschappenmandje.sql` parses in under 2 s with the schema
      CSV loaded.
- [ ] No new TODOs in the happy path.
- [ ] Every new method has a grep-confirmed call site (Fix B & C only edit
      existing methods; Fix A edits an existing block — no new methods are
      introduced).

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Removing schema_sources from `expand()` breaks a corner case where a column was only resolvable via inlined schema body | sg_lineage still receives schema_sources via `sources=`; covered by existing column-lineage tests (T-09-01) |
| `dependency_filter` drops a legitimate cross-file source because the referenced table was missed in pass 1 (e.g. dynamic SQL, missed parse) | Pass 1 already populates `referenced_tables` from the same parser path; any miss already affects lineage. Filter is a strict subset of what pass 1 saw — it cannot lose a source that pass 1 captured. Validated by existing pass-2 lineage tests. |
| `dependency_filter` keys vs `cross_file_sources()` keys disagree on casing | Both sides lowercase: `register_cross_file_sources` already lowercases (verified in aggregator.py:43), and the aggregator lowercases `t.name` before building the filter. Add an assertion in T-A2 covering mixed-case input. |
| New `dependency_filter` parameter breaks existing direct callers of `parse_file` | Default value `None` preserves prior behaviour; grep `parse_file(` across `src/` and `tests/` and confirm no positional third-arg callers exist. |
| Returning `_sources_cache` by reference allows caller mutation | Document in docstring; all current callers in `base.py` use the dict read-only |
| `ALTER … SET TAG` regex is over-greedy and eats following statements | Anchor on `;` terminator with non-greedy `[^;]*?`; cover with a multi-statement fixture in T-C |
| `ALTER … SET TAG` regex is under-greedy and misses a real form | Add real DWH variants to the T-C fixture as they appear in the profile output |
| `_sources_cache` is shared across threads | `_lock` already guards reads & writes; cache check is inside the `with self._lock` block |

## Rollout / Rollback

- Single-PR rollout. No config flag.
- Rollback: revert the three files; no data migration required.
- No backwards-compat surface affected — re-indexing is the migration path
  per project policy.
