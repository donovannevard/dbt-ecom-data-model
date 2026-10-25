import json

import duckdb
import pytest

from remediation.warehouse import DuckDBReader
from remediation.diagnose import REDACTED, _scrub, build_bundle, failure_rows, pii_columns


def test_pii_columns_merge_policy_and_manifest_tags(policy):
    manifest = {"nodes": {"model.p.c": {"columns": {
        "secret_handle": {"config": {"meta": {"pii": True}}},
        "id": {"config": {"meta": {}}},
    }}}, "sources": {}}
    cols = pii_columns(manifest, policy)
    assert {"secret_handle", "email", "first_name"} <= cols and "id" not in cols


def test_real_manifest_tags_customer_names(real_manifest, policy):
    assert {"first_name", "last_name", "email"} <= pii_columns(real_manifest, policy)


def test_failure_rows_redact_pii_but_keep_nulls(tmp_path):
    db = tmp_path / "dev.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("create schema audit")
        con.execute("create table audit.f as select * from (values (1, 'a@b.com', 'Ann'), (2, null, 'Bo')) t(id, Email, nickname)")
    out = failure_rows(DuckDBReader(db), '"dev"."audit"."f"', 20, {"email"})
    assert out["redacted_columns"] == ["Email"]
    assert [r["Email"] for r in out["rows"]] == [REDACTED, None]
    assert out["rows"][0]["nickname"] == "Ann"


def test_failure_rows_degrade_gracefully(tmp_path):
    assert "unavailable" in failure_rows(DuckDBReader(tmp_path / "missing.duckdb"), '"x"."y"."z"', 5, set())
    assert "unavailable" in failure_rows(None, '"x"."y"."z"', 5, set())  # no database access at all
    assert "unavailable" in failure_rows(DuckDBReader(tmp_path / "x.duckdb"), None, 5, set())


def test_emails_scrubbed_from_messages():
    assert _scrub("Could not convert 'jo.smith+x@example.co.uk' to INT32") == \
        f"Could not convert '{REDACTED}' to INT32"


def test_bundle_from_synthetic_run(tmp_path):
    manifest = {
        "metadata": {"project_name": "p"},
        "nodes": {
            "model.p.m": {"name": "m", "resource_type": "model", "original_file_path": "models/m.sql",
                          "raw_code": "select 1 as id", "config": {"materialized": "view", "meta": {}},
                          "depends_on": {"macros": [], "nodes": []}},
            "test.p.unique_m_id": {"name": "unique_m_id", "resource_type": "test",
                                   "attached_node": "model.p.m", "column_name": "id",
                                   "test_metadata": {"name": "unique", "kwargs": {"column_name": "id", "model": "x"}},
                                   "config": {"severity": "error", "meta": {}},
                                   "depends_on": {"macros": [], "nodes": ["model.p.m"]}},
        },
        "sources": {}, "macros": {}, "child_map": {"model.p.m": []}, "parent_map": {"model.p.m": []},
    }
    run_results = {"metadata": {"dbt_version": "1.x"}, "elapsed_time": 1.0, "results": [
        {"unique_id": "test.p.unique_m_id", "status": "fail", "message": "Got 1 result", "failures": 1},
        {"unique_id": "model.p.m", "status": "success"},
    ]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "run_results.json").write_text(json.dumps(run_results))

    bundle = build_bundle(tmp_path, tmp_path, tmp_path / "none.duckdb", scenario_id="secret")
    [failure] = bundle["failures"]
    assert failure["test"]["type"] == "unique" and "model" not in failure["test"]["arguments"]
    assert failure["model"] == "m" and bundle["models"]["m"]["raw_sql"] == "select 1 as id"
    assert failure["fix_risk"]["tier"] == "quarantine"  # untagged -> default
    assert "unavailable" in failure["failure_rows"]
    # The scenario id is only ever under _meta, which prompts must exclude.
    assert bundle["_meta"]["scenario_id"] == "secret"
    assert "secret" not in json.dumps({k: v for k, v in bundle.items() if k != "_meta"})


def test_source_schema_gives_real_columns_and_redacted_samples(tmp_path):
    from remediation.diagnose import source_schema

    db = tmp_path / "dev.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("create schema extract")
        con.execute("create table extract.sessions as select * from (values (1, 'a@b.com', 3)) t(id, email, pageviews)")
    out = source_schema(DuckDBReader(db), '"dev"."extract"."sessions"', {"email"})
    assert [c["name"] for c in out["columns"]] == ["id", "email", "pageviews"]
    assert out["sample_rows"][0]["email"] == REDACTED and out["sample_rows"][0]["id"] == 1
    assert "unavailable" in source_schema(None, '"x"."y"."z"', set())


def _dup_db(tmp_path):
    db = tmp_path / "dev.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("create table main.payments as select * from (values "
                    "('p1', 10.0, 'a@b.com'), ('p2', 20.0, null), ('p2', 99.0, 'c@d.com'), ('p3', 1.0, null)"
                    ") t(id, amount, email)")
    return db


def test_duplicate_key_rows_show_whether_copies_conflict(tmp_path):
    from remediation.diagnose import duplicate_key_rows

    db = _dup_db(tmp_path)
    test = {"test_metadata": {"name": "unique", "kwargs": {"column_name": "id"}}, "column_name": "id"}
    stored = {"rows": [{"unique_field": "p2", "n_records": 2}]}
    out = duplicate_key_rows(DuckDBReader(db), '"dev"."main"."payments"', test, stored, 20, {"email"})
    assert out["key_columns"] == ["id"] and [r["amount"] for r in out["rows"]] == [20.0, 99.0]
    assert out["rows"][1]["email"] == REDACTED


def test_duplicate_key_rows_for_combination_test(tmp_path):
    from remediation.diagnose import duplicate_key_rows

    db = _dup_db(tmp_path)
    test = {"test_metadata": {"name": "unique_combination_of_columns",
                              "kwargs": {"combination_of_columns": ["id", "amount"]}}}
    out = duplicate_key_rows(DuckDBReader(db), '"dev"."main"."payments"', test,
                             {"rows": [{"id": "p1", "amount": 10.0, "n_records": 2}]}, 20, set())
    assert [r["id"] for r in out["rows"]] == ["p1"]


def test_duplicate_key_rows_only_for_uniqueness_tests(tmp_path):
    from remediation.diagnose import duplicate_key_rows

    test = {"test_metadata": {"name": "not_null", "kwargs": {"column_name": "id"}}}
    assert duplicate_key_rows(DuckDBReader(_dup_db(tmp_path)), '"dev"."main"."payments"', test, {"rows": [{}]}, 20, set()) is None
