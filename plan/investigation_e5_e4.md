# Investigation Plan: E4 and E5 Root Causes

Plan date: 2026-05-11
Author: architect-planner
Status: INVESTIGATION PLAN — no implementation, no code changes
Estimated effort: 1–2 hours

---

## Purpose

Answer the two open questions from `ARCHITECTURE_REVIEW.md` § 12.6 so that
sprint planning for the next fix sprint can begin:

- **Q-12-1**: Are the 6 E4 parse-failure files DML or DDL?
- **Q-12-2**: What exception types and messages make up the 1,280 E5 errors?

Both questions can be answered entirely by reading `results_full.json` and
inspecting the files it names. No new code is required.

---

## What We Already Know (from `results_full.json`)

Reading the file before writing this plan revealed the following, which narrows
the investigation scope significantly.

### E4 — 6 parse-failure files (already visible in the JSON)

The `error_summary.E4.example_files` array contains all 6 files:

| File | Path prefix | Nature |
|------|-------------|--------|
| `WTDH_CONTRACT.sql` | `ddl/changelogs/BA-TABLES/` | DDL changelog |
| `htwfm_contract_us28254.sql` | `etl/pdi/template/` | ETL/DML |
| `htwfm_employee_us28254.sql` | `etl/pdi/template/` | ETL/DML |
| `us15770-check-controles-herstelactie.sql` | `etl/pdi/template/` | ETL/DML |
| `test.sql` | `etl/sql/int/` | ETL/DML (test file) |
| `outbound_ga4_artikelen.sql` | `etl/sql/interface/` | ETL/DML |

**Preliminary answer to Q-12-1**: 5 of the 6 failing files are in `etl/` paths
(DML/ETL). The path prefix alone is not conclusive — a file under `etl/` could
still contain only DDL. File inspection is required to confirm.

The `error_summary.E4.example_messages` array (5 entries, one per file in the
JSON) also reveals the sqlglot errors:

- `Expecting ).` (syntax parse error)
- `Required keyword: 'true' missing for <class 'sqlglot.expressions.functions.If'>`
  (two files — same error pattern, likely an unsupported IF() dialect variant)
- `Invalid expression / Unexpected token.`
- `Expecting ).` (second instance, different file)

The sixth file (`outbound_ga4_artikelen.sql`) has no example message stored
(only 5 examples are stored for E4, matching 5 of 6 files).

### E5 — 1,280 errors, 5 example messages stored

The `error_summary.E5.example_messages` array (5 entries maximum) shows:

```
col_lineage:Movertype:Cannot find column 'MOVERTYPE' in query.
col_lineage:Movertype:Cannot find column 'MOVERTYPE' in query.
col_lineage:Movertype:Cannot find column 'MOVERTYPE' in query.
col_lineage:Movertype:Cannot find column 'MOVERTYPE' in query.
col_lineage:Rotatie:Cannot find column 'ROTATIE' in query.
```

**Preliminary answer to Q-12-2**: the 5 stored examples are all
`Cannot find column '<PLAIN_COL>' in query.` — the same exception message as E1
(alias_col_ref), but with a plain column name (no table-alias prefix, so they
were classified as E5 rather than E1). This suggests E5 may be a single exception
type: sqlglot `Cannot find column` on a plain col name.

However, 5 examples from 1,280 errors is not enough to confirm this is the only
exception type. The investigation steps below address this gap.

---

## Step-by-Step Instructions

### Step 1 — Confirm E4 file nature (20 minutes)

Open each of the 6 E4 files and determine whether it contains DML statements
(SELECT, INSERT, CREATE TABLE AS SELECT, MERGE) or pure DDL (CREATE TABLE without
AS SELECT, ALTER, DROP).

Files to inspect (all relative to the DWH corpus root):

1. `ddl/changelogs/BA-TABLES/WTDH_CONTRACT.sql`
2. `etl/pdi/template/htwfm_contract_us28254.sql`
3. `etl/pdi/template/htwfm_employee_us28254.sql`
4. `etl/pdi/template/us15770-check-controles-herstelactie.sql`
5. `etl/sql/int/test.sql`
6. `etl/sql/interface/outbound_ga4_artikelen.sql`

For each file, record:

- **Contains DML?** yes / no (does it have SELECT, INSERT, MERGE, or CTAS?)
- **Parse error line** (from the `example_messages` entry for that file)
- **Probable cause** (dialect mismatch, truncated file, non-standard syntax)

Cross-reference each error message against the file content at the reported line
number to confirm the sqlglot error is accurate.

The key question: **does the parse failure block ETL column lineage that would
otherwise be extractable?** A DDL-only file failing to parse has no impact on
column lineage coverage. A DML file failing to parse means every column in that
file is missing from the graph.

Record the answer as: `<filename>: DML=yes|no, lineage_impact=high|none`

### Step 2 — Widen the E5 sample (30–45 minutes)

The JSON stores only 5 example messages. To determine whether `Cannot find column`
is the only E5 exception type, run a targeted query against the full JSON or
re-run the diagnostic script with wider sampling.

**Option A — edit the hardcoded cap and re-run on a subset**

The script does not expose CLI flags for the example cap. The limits are
hardcoded constants in `scripts/collect_parse_errors.py` (look for `< 5` for
messages and `< 10` for files). Edit those constants directly in the script,
then re-run on the `ddl/changelogs/IA-DATAPRODUCTS/` subdirectory only
(all 10 E5 example files are there, so a full-corpus re-run is not needed):

```bash
uv run python scripts/collect_parse_errors.py \
  /home/ignwrad/Projects/dwh/ddl/changelogs/IA-DATAPRODUCTS \
  --dialect snowflake \
  --output results_e5_sample.json
```

Then inspect `error_summary.E5.example_messages` in `results_e5_sample.json`.

**Option B — direct file inspection**

The E5 `example_files` list names 10 files, all in
`ddl/changelogs/IA-DATAPRODUCTS/`. Open 3–5 of those files and identify the
column that triggers the error. The E5 message pattern is
`col_lineage:<col>:Cannot find column '<COL>' in query.` — the column name is
embedded in the message. Inspect the SQL around that column to understand why
sqlglot cannot resolve it (likely: column defined in a CTE or subquery that
`qualify()` cannot expand without cross-file sources).

Regardless of which option is used, the goal is to answer:

1. Is `Cannot find column` the only exception class in E5, or are there others
   (e.g., `AttributeError`, `RecursionError`, sqlglot `OptimizeError`)?
2. For `Cannot find column` errors: is the column name always plain (no `.`), or
   do any have a table prefix that slipped through the E1 guard?
3. For `Cannot find column` errors: what SQL pattern triggers them? (CTE alias,
   subquery alias, window function, lateral join?)

Collect at least 10 distinct E5 messages before concluding.

### Step 3 — Classify E5 by exception type (15 minutes)

Organise the messages collected in Step 2 into a frequency table:

| Exception message pattern | Count (of sampled) | Example column | SQL pattern |
|---|---|---|---|
| `Cannot find column '<col>' in query.` | ? | `MOVERTYPE` | ? |
| (other patterns) | ? | — | — |

If all sampled messages are `Cannot find column`, state: "E5 appears to be a
single exception type." If other patterns exist, list them separately — they will
require different fixes.

### Step 4 — Identify the SQL pattern triggering E5 (15 minutes)

For `Cannot find column` errors, inspect the SQL of 2–3 affected files to
understand the common pattern. Based on the example files (all in
`ddl/changelogs/IA-DATAPRODUCTS/` and named `AGG_*`), these are likely aggregate
views or materialized tables with many columns sourced from CTEs or multi-table
joins. The hypothesis is:

> `qualify()` inside `sg_lineage()` cannot expand CTE references in files where
> the CTE is defined outside the current SQL string (cross-file dependency), so
> the column reference is left unresolved and sqlglot raises `Cannot find column`.

Confirm or refute this hypothesis by looking at the SQL structure of one
`AGG_BESTELADVIEZEN_WEEK_SEGMENT_FORMULE_VOORRAADLOCATIE.sql` file. If the file
contains a CTE that defines `MOVERTYPE`, the fix is internal alias resolution.
If the file references a view or table defined elsewhere, the fix requires
`sources=` expansion (cross-file).

---

## What to Record

The architect-reviewer needs the following facts to update `ARCHITECTURE_REVIEW.md` § 12:

### For Q-12-1 (E4)

```
E4 findings:
- Files with DML content: <list, or "none">
- Files that are DDL-only: <list>
- Lineage impact: high (DML files affected) | none (all DDL)
- Sqlglot error patterns:
  - IF() dialect error (files: ...): <description of cause>
  - Syntax parse error (files: ...): <description of cause>
  - Unexpected token (files: ...): <description of cause>
- Recommended action: <"block on FIX-DDL-SKIP" | "add as separate fix ticket" | "low priority, DDL only">
```

### For Q-12-2 (E5)

```
E5 findings:
- Distinct exception types found: <1 or more>
- Dominant type: Cannot find column (count: N / N sampled)
- Other types (if any): <list with counts>
- SQL pattern: <CTE reference | cross-file view | subquery alias | other>
- Hypothesis confirmed/refuted: <CTE cross-file | internal alias | other>
- Fix direction: <sources= expansion | qualify() workaround | other>
- E1/E5 boundary note: flag any messages where "Cannot find column" AND the
  column name contains ".". The script increments E5 for BOTH branches of the
  E1/E5 guard (lines ~237–249 of collect_parse_errors.py), so some counted E5s
  may be E1-pattern errors (table-alias prefix). Record how many samples, if
  any, fall into this category.
```

---

## Success Criteria

The investigation is complete when all of the following are established:

- [ ] **Q-12-1 answered**: each of the 6 E4 files is classified as DML or DDL,
      with confirmation from file content (not just path prefix)
- [ ] **E5 exception type confirmed**: at least 10 distinct E5 messages sampled,
      with a frequency breakdown by exception class
- [ ] **E5 SQL pattern identified**: at least 1 concrete SQL example showing
      what structure causes `Cannot find column` at the E5 classification point
- [ ] **Fix direction for E5 stated**: one of (a) cross-file `sources=` expansion,
      (b) internal `qualify()` workaround, or (c) unknown — requires further
      debugging
- [ ] **Section 12.6 in ARCHITECTURE_REVIEW.md closed**: both Q-12-1 and Q-12-2
      replaced with documented answers (done by architect-reviewer, not this
      investigation)

Once these criteria are met, the sprint-planner has enough information to write
fix tickets for E5 and (if DML) E4.

---

## Risks and Notes

- The script stores only 5 example messages per error category. If Option A
  (re-run with wider sampling) is chosen for Step 2, the script must be checked
  first to see if that cap is a configurable constant or hardcoded. Do not
  modify production code — the script is a diagnostic one-off.

- The 6 E4 files are already fully listed in `results_full.json`
  (`error_summary.E4.example_files`). There is no need to re-run the full
  corpus to answer Q-12-1 — direct file inspection is sufficient.

- `etl/sql/int/test.sql` is likely a developer test file, not a production ETL
  file. If it is a test/scratch file with no deployed lineage, its parse failure
  has no practical impact even if it contains DML. Note this distinction in
  the findings.

- The E5 example files are all in `ddl/changelogs/IA-DATAPRODUCTS/`. If Step 4
  confirms these are aggregate views with CTE references resolved cross-file,
  the `sources=` fix direction is already implied by Architecture finding R-02.
  The investigation should confirm this rather than assume it.

---

## Artifacts

| Artifact | Owner | Action |
|----------|-------|--------|
| This plan | architect-planner | Written and committed |
| Investigation findings | developer | Written as a short memo (prose or table) in the conversation, not a new file |
| `ARCHITECTURE_REVIEW.md` § 12.6 | architect-reviewer | Closes Q-12-1 and Q-12-2 with confirmed answers |
| Follow-on sprint plan | sprint-planner | Written after § 12.6 is closed |
