import duckdb
import pytest

from remediation.explore import compare, diff_rows, duckdb_command, freeze


def _dbt_like(path, rows):
    """A database shaped like dbt-duckdb output: catalog `dev`, a view whose
    SQL names that catalog, and a snapshot-style dbt_ column."""
    dev = path.parent / "dev.duckdb"
    dev.unlink(missing_ok=True)
    with duckdb.connect(str(dev)) as con:
        con.execute("create schema extract")
        con.execute("create table extract.payments (id varchar, amount int, dbt_valid_from timestamp)")
        con.executemany("insert into extract.payments values (?, ?, now())", rows)
        con.execute("create view main.stg_payments as select id, amount from dev.extract.payments")
    dev.rename(path)
    return path


@pytest.fixture
def pair(tmp_path):
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    src_a = _dbt_like(tmp_path / "a" / "src.duckdb", [("p1", 10), ("p2", 20)])
    src_b = _dbt_like(tmp_path / "b" / "src.duckdb", [("p1", 10), ("p2", 20), ("p2", 20)])  # duplicate
    freeze(src_a, tmp_path / "clean.duckdb")
    freeze(src_b, tmp_path / "broken.duckdb")
    return tmp_path / "clean.duckdb", tmp_path / "broken.duckdb"


def test_frozen_views_work_under_any_alias(pair):
    # dbt-duckdb views name the `dev` catalog in their SQL. On DuckDB 1.x that
    # breaks them under any other alias; 2.0 resolves them. Frozen snapshots
    # hold tables, so they work either way.
    with duckdb.connect() as con:
        con.execute(f"attach '{pair[0]}' as clean (read_only)")
        assert con.execute("select count(*) from clean.main.stg_payments").fetchone()[0] == 2
        views = con.execute("select count(*) from duckdb_views() where database_name = 'clean' "
                            "and not internal").fetchone()[0]
        assert views == 0


def test_compare_counts_duplicates_and_ignores_dbt_columns(pair):
    results = {r["table"]: r for r in compare(*pair)}
    payments = results["extract.payments"]
    # dbt_valid_from differs between builds but is ignored; the duplicate is not.
    assert payments["status"] == "changed" and (payments["only_a"], payments["only_b"]) == (0, 1)
    assert results["main.stg_payments"]["only_b"] == 1


def test_identical_snapshots_compare_same(pair):
    assert all(r["status"] == "same" for r in compare(pair[0], pair[0]))


def test_diff_rows_shows_the_extra_row(pair):
    cols, only_a, only_b = diff_rows(*pair, "extract.payments")
    assert cols == ["id", "amount"] and only_a == [] and only_b == [("p2", 20)]


def test_shell_attaches_existing_snapshots_read_only(tmp_path, pair):
    cmd = duckdb_command(pair[0].parent, ui=True)
    assert cmd[0] == "duckdb" and cmd[-1] == "-ui"
    assert "AS clean (READ_ONLY)" in cmd[2] and "AS broken (READ_ONLY)" in cmd[2] and "fixed" not in cmd[2]


def test_warehouse_ui_attaches_build_baseline_and_scenarios(tmp_path):
    from remediation.explore import snapshot_dev, warehouse_attachments, warehouse_ui_command

    dev = tmp_path / "dev.duckdb"
    duckdb.connect(str(dev)).close()
    (tmp_path / "dev.duckdb.wal").write_text("pending")
    snap = snapshot_dev(dev, tmp_path / "snap")
    assert snap != dev and (tmp_path / "snap" / "dev.duckdb.wal").exists()  # copy, never the live file

    root = tmp_path / ".chaos"
    for rel in ["_baseline/clean.duckdb", "a_scenario/broken.duckdb", "a_scenario/fixed.duckdb", "other/broken.duckdb"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).touch()
    (root / "empty_dir").mkdir()
    aliases = [a for a, _ in warehouse_attachments(snap, root)]
    assert aliases == ["dev", "clean", "a_scenario_broken", "a_scenario_fixed", "other_broken"]

    cmd = warehouse_ui_command(warehouse_attachments(snap, root))
    assert cmd[-1] == "-ui" and '(READ_ONLY)' in cmd[2] and cmd[2].endswith('USE "dev";')


def test_warehouse_ui_without_a_build_shows_scenarios_only(tmp_path):
    from remediation.explore import warehouse_attachments

    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "broken.duckdb").touch()
    assert [a for a, _ in warehouse_attachments(None, tmp_path)] == ["s_broken"]
