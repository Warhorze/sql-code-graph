"""Unit tests for the default file-discovery backup/snapshot ignore (issue #27a).

These pin the conservative pattern set wired into ``load_ignore_spec`` and the
walker: a backup-named SQL *file* is excluded at discovery, while a legitimately
named (incl. dated-mart and bare ``_eenmalig``) file is NOT — and the whole
default is configurable via ``[sqlcg.file_discovery]``.
"""

from pathlib import Path

from sqlcg.core.config import get_file_ignore_defaults
from sqlcg.indexer.walker import walk_sql_files
from sqlcg.utils.ignore import is_ignored, load_ignore_spec

# --- file names that MUST be dropped (unambiguous backup markers) ---
BACKUP_NAMES = [
    "wtdh_artikel_bck.sql",
    "foo_bck_us39553.sql",
    "bar_bck_archive.sql",
    "baz_backup.sql",
    "qux_backup_20240716.sql",
]

# --- file names that MUST be kept (legitimate, conservative-by-default) ---
LEGIT_NAMES = [
    "wtda_artikel_eenmalig.sql",  # one-time-load production table — NOT a backup
    "wtfs_voorraad_dagstand_eenmalig.sql",
    "artikel_eenmalig.sql",
    "wtfs_voorraad_dagstand.sql",  # legitimate dated/snapshot mart
    "sales_20240716.sql",  # bare date suffix — never a default exclusion
    "queries.sql",
    "backup_helper.sql",  # leading "backup" is not the suffix marker
]


def _make_tree(tmp_path: Path, names: list[str]) -> Path:
    for n in names:
        (tmp_path / n).write_text("SELECT 1;")
    return tmp_path


def test_default_patterns_present_and_conservative():
    """The default set ships the _bck/_backup markers but NOT bare _eenmalig/date."""
    defaults = get_file_ignore_defaults(Path("/nonexistent"))
    assert "*_bck.sql" in defaults
    assert "*_bck_*.sql" in defaults
    assert "*_backup.sql" in defaults
    assert "*_backup_*.sql" in defaults
    # dated forms are opt-in only — must be absent by default
    assert "*_eenmalig_[0-9]*.sql" not in defaults
    # bare _eenmalig must never be a default (legitimate production table)
    assert all("_eenmalig" not in p or "[0-9]" in p for p in defaults)


def test_backup_files_excluded_at_discovery(tmp_path):
    """walk_sql_files drops backup-named files with no user .sqlcgignore."""
    _make_tree(tmp_path, BACKUP_NAMES + ["queries.sql"])
    spec = load_ignore_spec(tmp_path)
    found = {f.name for f in walk_sql_files(tmp_path, spec, use_git=False)}
    assert found == {"queries.sql"}
    for b in BACKUP_NAMES:
        assert b not in found


def test_legitimate_files_not_excluded(tmp_path):
    """Legit names (incl. bare _eenmalig and dated marts) survive discovery."""
    _make_tree(tmp_path, LEGIT_NAMES)
    spec = load_ignore_spec(tmp_path)
    found = {f.name for f in walk_sql_files(tmp_path, spec, use_git=False)}
    assert found == set(LEGIT_NAMES)


def test_mixed_tree_keeps_only_legit(tmp_path):
    """A mixed tree yields exactly the legit set; every backup file is dropped."""
    _make_tree(tmp_path, BACKUP_NAMES + LEGIT_NAMES)
    spec = load_ignore_spec(tmp_path)
    found = {f.name for f in walk_sql_files(tmp_path, spec, use_git=False)}
    assert found == set(LEGIT_NAMES)


def test_exclude_backups_false_disables_defaults(tmp_path):
    """[sqlcg.file_discovery] exclude_backups = false keeps backup files."""
    _make_tree(tmp_path, BACKUP_NAMES + ["queries.sql"])
    (tmp_path / ".sqlcg.toml").write_text("[sqlcg.file_discovery]\nexclude_backups = false\n")
    spec = load_ignore_spec(tmp_path)
    found = {f.name for f in walk_sql_files(tmp_path, spec, use_git=False)}
    assert found == set(BACKUP_NAMES + ["queries.sql"])


def test_exclude_dated_backups_opt_in(tmp_path):
    """exclude_dated_backups = true adds the dated *_eenmalig_<ts> form."""
    names = ["wtda_artikel_eenmalig.sql", "wtda_artikel_eenmalig_20240716.sql"]
    _make_tree(tmp_path, names)
    (tmp_path / ".sqlcg.toml").write_text("[sqlcg.file_discovery]\nexclude_dated_backups = true\n")
    spec = load_ignore_spec(tmp_path)
    found = {f.name for f in walk_sql_files(tmp_path, spec, use_git=False)}
    # bare _eenmalig stays; dated _eenmalig_<ts> is dropped
    assert found == {"wtda_artikel_eenmalig.sql"}


def test_user_sqlcgignore_extends_defaults(tmp_path):
    """User .sqlcgignore patterns apply ON TOP of the built-in backup defaults."""
    _make_tree(tmp_path, ["wtdh_artikel_bck.sql", "scratch.sql", "queries.sql"])
    (tmp_path / ".sqlcgignore").write_text("scratch.sql\n")
    spec = load_ignore_spec(tmp_path)
    found = {f.name for f in walk_sql_files(tmp_path, spec, use_git=False)}
    # both the built-in backup default AND the user pattern are honoured
    assert found == {"queries.sql"}


def test_is_ignored_matches_backup_path(tmp_path):
    """is_ignored returns True for a backup file under the default spec."""
    spec = load_ignore_spec(tmp_path)
    assert is_ignored(tmp_path / "ddl" / "wtdh_artikel_bck.sql", tmp_path, spec)
    assert not is_ignored(tmp_path / "ddl" / "wtda_artikel_eenmalig.sql", tmp_path, spec)
