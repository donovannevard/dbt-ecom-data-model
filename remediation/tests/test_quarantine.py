import json

import pytest

from remediation import quarantine as q
from remediation.github import LocalTracker, read_marker

SRC = "source.p.extract.payments"


def _manifest():
    def test(name, kind, kwargs):
        return {"name": name, "resource_type": "test", "depends_on": {"nodes": [SRC]},
                "test_metadata": {"name": kind, "kwargs": kwargs}}
    return {
        "metadata": {"adapter_type": "duckdb"},
        "nodes": {
            "test.p.conflict": test("conflict", q.CONFLICT_TEST, {"key_columns": ["id"]}),
            "test.p.dup": test("dup", q.DUPLICATE_TEST, {"combination_of_columns": ["id"]}),
            "model.p.stg_payments": {"name": "stg_payments", "resource_type": "model"},
        },
        "sources": {SRC: {"source_name": "extract", "name": "payments"}},
        "child_map": {SRC: ["model.p.stg_payments", "test.p.conflict"], "model.p.stg_payments": []},
    }


def _entries(tmp_path, conflict_status="warn", rows=None):
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    (target / "run_results.json").write_text(json.dumps({"results": [
        {"unique_id": "test.p.conflict", "status": conflict_status, "failures": 2 if conflict_status == "warn" else 0,
         "relation_name": '"dev"."audit"."conflict"'},
        {"unique_id": "test.p.dup", "status": "warn", "failures": 1, "relation_name": None},
    ]}))
    entries = q.collect(target, _manifest())
    rows = rows if rows is not None else [{"id": "p1", "amount": 1, "email": "a@b.com"},
                                          {"id": "p1", "amount": 9, "email": None}]
    q.load_rows(entries, lambda relation, limit: rows, {"email"})
    return entries


def test_collect_finds_conflict_and_duplicate_checks(tmp_path):
    entries = _entries(tmp_path)
    assert [(e.source, e.kind, e.flagged) for e in entries] == [
        ("extract.payments", "conflict", True), ("extract.payments", "duplicate", True)]


def test_rows_are_redacted(tmp_path):
    conflict = _entries(tmp_path)[0]
    assert conflict.rows[0]["email"] == q.REDACTED and conflict.rows[1]["email"] is None


def test_lifecycle_open_refresh_change_close(tmp_path, monkeypatch):
    monkeypatch.delenv("REMEDIATION_WEBHOOK_URL", raising=False)
    tracker, m = LocalTracker(tmp_path / "tracker"), _manifest()

    assert q.sync(_entries(tmp_path), tracker, m, "run 1") == ["opened #1 for extract.payments"]
    [issue] = tracker.list_open("issue", q.LABEL)
    assert read_marker(issue["body"], "quarantine") == "extract.payments"
    assert "`stg_payments`" in issue["body"] and "How to resolve" in issue["body"]

    assert q.sync(_entries(tmp_path), tracker, m, "run 2") == ["refreshed #1 for extract.payments"]
    assert tracker.list_open("issue", q.LABEL)[0]["comments"] == []  # no spam

    more = [{"id": "p1", "amount": 1}, {"id": "p1", "amount": 9}, {"id": "p2", "amount": 3}, {"id": "p2", "amount": 4}]
    assert "keys changed" in q.sync(_entries(tmp_path, rows=more), tracker, m, "run 3")[0]

    assert q.sync(_entries(tmp_path, conflict_status="pass", rows=[]), tracker, m, "run 4") == \
        ["closed #1 for extract.payments (resolved)"]
    assert tracker.list_open("issue", q.LABEL) == []


def test_unrun_checks_never_close_an_issue(tmp_path):
    tracker, m = LocalTracker(tmp_path / "tracker"), _manifest()
    q.sync(_entries(tmp_path), tracker, m, "run 1")
    assert q.sync([], tracker, m, "a run without source tests") == ["nothing to do"]
    assert len(tracker.list_open("issue", q.LABEL)) == 1


def test_webhook_is_called_on_change_only(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv("REMEDIATION_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.setattr(q.urllib.request, "urlopen", lambda req, timeout: sent.append(json.loads(req.data)))
    tracker, m = LocalTracker(tmp_path / "tracker"), _manifest()
    q.sync(_entries(tmp_path), tracker, m, "run 1")
    q.sync(_entries(tmp_path), tracker, m, "run 2")
    assert len(sent) == 1 and "Quarantine: extract.payments" in sent[0]["text"]


def test_a_failing_webhook_never_raises(monkeypatch):
    monkeypatch.setenv("REMEDIATION_WEBHOOK_URL", "https://hooks.example/x")

    def boom(*a, **k):
        raise OSError("unreachable")
    monkeypatch.setattr(q.urllib.request, "urlopen", boom)
    q.notify("hello")  # must not raise


def test_snowflake_uppercase_columns_still_give_the_keys():
    e = q.Entry(source="extract.payments", source_uid=SRC, kind="conflict", status="warn", failures=2,
                relation="r", key_columns=["id"],
                rows=[{"ID": "p1", "AMOUNT": 1}, {"ID": "p1", "AMOUNT": 9}, {"ID": "p2", "AMOUNT": 3}])
    assert q._keys(e) == ["p1", "p2"]
