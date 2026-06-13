# Releasing to PyPI — Runbook

This document covers the end-to-end process for tagging a release and publishing
it to PyPI, including how to backfill versions that were silently skipped due to
a batch-tag-push bug (see [§ Background](#background) below).

## Normal release process

Follow the **Releasing** section in [`CLAUDE.md`](../CLAUDE.md) for the version-bump
steps. The short form:

1. **Bump the version** in the feature branch, before the PR merges:
   - `pyproject.toml` — `version = "X.Y.Z"`
   - `src/sqlcg/__init__.py` — `__version__ = "X.Y.Z"`
   - `uv lock`

2. **Merge the PR to `master`** (squash or `--merge`).

3. **Tag ONE version at a time** on the merge commit:

   ```bash
   git checkout master && git pull --ff-only origin master
   git tag -a vX.Y.Z -m "vX.Y.Z — <one-line summary>" <merge-commit-sha>
   git push origin vX.Y.Z
   ```

   Pushing a single `v*` tag triggers the `push` event in
   [`.github/workflows/release.yml`](../.github/workflows/release.yml).
   The `test` job runs first; if it passes, `publish-pypi` builds and publishes
   the wheel to PyPI via trusted publishing (no API key needed).

4. **Verify** the run at
   `https://github.com/Warhorze/sql-code-graph/actions/workflows/release.yml`.
   Check that both `test` and `publish-pypi` are green.

### Push limit — never batch-push tags

GitHub Actions creates workflow runs for **at most ~3 tags pushed in a single
`git push`**. Additional tags in the same push are silently skipped — no run fires,
nothing is published. This is the root cause of the v1.6.0–v1.14.2 gap (see
[Background](#background)).

**Rule: push tags one at a time** (`git push origin vX.Y.Z`) or at most 2–3 per
push. Never `git push --tags`.

## Re-publishing a tag via `workflow_dispatch`

If a tag was pushed but its run was skipped (batch push) or failed (test failure),
you can re-trigger the pipeline on demand — no new commit or tag needed.

1. Go to
   `https://github.com/Warhorze/sql-code-graph/actions/workflows/release.yml`.
2. Click **Run workflow** (top-right of the workflow list).
3. In the **ref** field, enter the tag you want to publish (e.g. `v1.6.0`).
4. Click **Run workflow**.

The pipeline checks out that exact tag, runs `test`, and if green, publishes the
built wheel to PyPI. The `test` gate is always required — no manual bypass exists.

### Prerequisite: tests must be green at that tag

`publish-pypi` has `needs: test`. If tests fail at the target tag, the publish
step is skipped. Before re-publishing an old tag, verify that the test suite
passes at that commit:

```bash
git checkout vX.Y.Z
uv sync --all-extras
uv run pytest tests/unit tests/integration -v --tb=short
uv run pyright
uv run ruff check .
```

If tests are red at the old tag, you have two options:
- Fix the tests on a new patch release (preferred — keeps history clean).
- Accept that the old version will not be published and start from the next patch.

## Backfilling skipped versions (v1.6.0–v1.14.2)

Versions v1.6.0 through v1.14.2 have tags on origin but were never published to
PyPI because they were pushed as one batch (`git push --tags` on 2026-06-11).

To backfill, use the `workflow_dispatch` approach above, one version at a time:

1. Check that the tests at that tag are green (see prerequisite above).
2. Trigger `workflow_dispatch` with `ref = vX.Y.Z`.
3. Wait for green, then move to the next version.
4. Repeat for each version you want to backfill.

**You decide which versions to backfill.** Every version from v1.6.0 to v1.14.2
has a valid annotated tag on origin. You are not required to publish all of them —
publish whichever ones you want available on PyPI as historical milestones.

Versions v1.15.0 and later are untagged (as of the last check). They will be
published when you tag them individually on the merge commits, following the normal
process above.

## Background

PyPI was stuck at v1.5.1 because of two compounding failures:

1. **Batch-push dropped the trigger (primary cause).** Tags v1.6.0–v1.14.2 were
   pushed as a single batch in June 2026. GitHub Actions silently skipped all but
   the first ~3, producing zero release runs for most of them.

2. **The one manually-dispatched run failed.** A `workflow_dispatch` run for
   v1.14.2 was started manually, but the `test` job failed (the test suite at
   that commit was red), so `publish-pypi` was skipped via the `needs: test` gate.

The `workflow_dispatch` input added in v1.25.4
([`release.yml`](../.github/workflows/release.yml)) routes around the batch-push
limit by providing an on-demand re-publish path for any tag.
