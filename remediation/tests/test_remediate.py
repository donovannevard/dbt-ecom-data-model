from types import SimpleNamespace

import pytest

from remediation import remediate as rem
from remediation.validate import Check, Validation


def _record(verdict="code_fix", edits=None, error=None, cost=0.05):
    if error:
        return {"error": error, "diagnosis": None, "cost_usd": 0}
    return {"error": None, "cost_usd": cost,
            "diagnosis": {"verdict": verdict, "confidence": 0.9,
                          "edits": edits if edits is not None else [{"path": "m.sql", "old": "a", "new": "b"}]}}


@pytest.fixture
def base(tmp_path, monkeypatch):
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "m.sql").write_text("a\n")
    return tmp_path


def _fake_validator(results):
    """Validator stand-in returning the given pass/fail sequence."""
    calls = iter(results)

    class Fake:
        def __init__(self, *a, **k):
            pass

        def validate(self, patched, log_path, confidence):
            ok = next(calls)
            v = Validation(passed=ok, route="auto" if ok else None)
            v.checks.append(Check("build", ok, "" if ok else "test still fails"))
            return v
    return Fake


def test_validated_on_first_attempt_asks_nothing(base, monkeypatch, policy):
    monkeypatch.setattr(rem, "Validator", _fake_validator([True]))
    out = rem.remediate({}, base / "base", base / "p", base / "l", policy, first=_record(),
                        ask=lambda *a: pytest.fail("should not ask"))
    assert out.status == "validated" and len(out.attempts) == 1


def test_rejection_is_fed_back_and_retried(base, monkeypatch, policy):
    monkeypatch.setattr(rem, "Validator", _fake_validator([False, True]))
    seen = []

    def ask(bundle, project, previous):
        seen.append(previous)
        return _record()
    out = rem.remediate({}, base / "base", base / "p", base / "l", policy, first=_record(), ask=ask)
    assert out.status == "validated" and len(out.attempts) == 2
    assert seen[0][0]["rejection"] == "build: test still fails"
    assert out.cost_usd == pytest.approx(0.10)


def test_gives_up_after_max_retries(base, monkeypatch, policy):
    monkeypatch.setattr(rem, "Validator", _fake_validator([False] * 3))
    out = rem.remediate({}, base / "base", base / "p", base / "l", policy,
                        first=_record(), ask=lambda *a: _record(), max_retries=2)
    assert out.status == "rejected" and len(out.attempts) == 3


def test_unappliable_edits_count_as_a_rejection(base, monkeypatch, policy):
    monkeypatch.setattr(rem, "Validator", _fake_validator([True]))
    bad = _record(edits=[{"path": "m.sql", "old": "zzz", "new": "b"}])
    out = rem.remediate({}, base / "base", base / "p", base / "l", policy,
                        first=bad, ask=lambda b, p, previous: _record())
    assert out.status == "validated" and "edits:" in out.attempts[0]["validation"]


@pytest.mark.parametrize("record, status", [(_record(verdict="data_issue", edits=[]), "not_a_code_fix"),
                                            (_record(error="declined"), "error")])
def test_non_fixes_stop_immediately(base, monkeypatch, policy, record, status):
    monkeypatch.setattr(rem, "Validator", _fake_validator([]))
    out = rem.remediate({}, base / "base", base / "p", base / "l", policy, first=record,
                        ask=lambda *a: pytest.fail("should not ask"))
    assert out.status == status
