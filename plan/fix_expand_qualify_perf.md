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
- [`src/sqlcg/lineage/schema_resolver.py`](../src/sqlcg/lineage/schema_resolver.py)
  — add a real cache for `as_sources_dict()`.
- [`src/sqlcg/parsers/snowflake_parser.py`](../src/sqlcg/parsers/snowflake_parser.py)
  — strip `ALTER TABLE/VIEW … MODIFY COLUMN … SET TAG …;` statements.

### Non-Goals

- Replicating SQLMesh's full `mapping_schema` pipeline.
- Pre-filtering schema by `depends_on` (deferred — Fix A removes the bottleneck
  without it).
- Switching `as_sources_dict()` to lazy per-table parsing (kept eager because
  the cache makes the cost a one-time hit per indexing job).
- Any change to `sg_lineage(sources=…)` semantics — schema_sources continue to
  feed sg_lineage for column resolution.

## Design

### Fix A — Stop expanding schema_sources

`exp.expand()` performs an `O(N_sources × N_nodes_walked)` substitution that
inlines every entry of the sources dict it receives. Today the dict carries
~10 000 synthetic schema entries that have no business being inlined — they
exist purely so `sg_lineage()` can resolve column ownership via `sources=`.

The new contract:

| Dict | Consumer | After fix |
|---|---|---|
| `sources_to_expand` | `exp.expand()` | **CTAS / CTE / temp bodies only** (`sources` arg, ≤ ~10 entries) |
| `combined_sources` | `sg_lineage(sources=…)` | unchanged — still `{**sources, **schema_sources}` |

Schema-side resolution during qualification continues to flow through
`mapping_schema` (passed to `qualify()` already, see
[`base.py:651-657`](../src/sqlcg/parsers/base.py)). `qualify()` does *not* need
`exp.expand()` to have inlined schema tables — it consults `mapping_schema`
directly. SQLMesh's pipeline confirms this is sufficient.

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

**Step A.1**: In
[`base.py`](../src/sqlcg/parsers/base.py) `_extract_column_lineage`, change the
`sources_to_expand` construction at lines 640–646 to iterate over `sources`
(the parameter), not `combined_sources`.

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

**Step A.2**: Add an inline comment block above the new `if sources:` branch
explaining *why* schema_sources are excluded (with a one-line reference to
SQLMesh's `mapping_schema` approach) so future readers don't re-introduce the
regression.

- Files affected: `src/sqlcg/parsers/base.py`.
- Acceptance: comment present, mentions both `exp.expand` cost and
  `mapping_schema`.

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

**T-A**: `tests/unit/test_expand_excludes_schema_sources.py`
- Build a `_extract_column_lineage` call with `sources={"cte_a": <Select>}`
  and `schema_sources={"big_table": <Select>}` where `big_table` has many
  columns.
- Patch `sqlglot.expressions.expand` and assert it is called with a dict
  whose keys are `{"cte_a"}` only (no `"big_table"`).
- Assert the resulting `combined_sources` passed to `sg_lineage` still
  contains both keys (i.e. observable from emitted lineage edges).

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
| Returning `_sources_cache` by reference allows caller mutation | Document in docstring; all current callers in `base.py` use the dict read-only |
| `ALTER … SET TAG` regex is over-greedy and eats following statements | Anchor on `;` terminator with non-greedy `[^;]*?`; cover with a multi-statement fixture in T-C |
| `ALTER … SET TAG` regex is under-greedy and misses a real form | Add real DWH variants to the T-C fixture as they appear in the profile output |
| `_sources_cache` is shared across threads | `_lock` already guards reads & writes; cache check is inside the `with self._lock` block |

## Rollout / Rollback

- Single-PR rollout. No config flag.
- Rollback: revert the three files; no data migration required.
- No backwards-compat surface affected — re-indexing is the migration path
  per project policy.
