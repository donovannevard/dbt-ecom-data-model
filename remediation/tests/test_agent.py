import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from remediation import agent
from remediation.config import REPO_ROOT


def _response(payload=None, stop_reason="end_turn", model=agent.MODEL, parsed=True):
    text = json.dumps(payload) if (payload and parsed) else "not json"
    usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                            cache_read_input_tokens=6000, iterations=None)
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(model=model, stop_reason=stop_reason, stop_details=None, usage=usage,
                           content=content, _request_id="req_1")


class FakeClient:
    def __init__(self, response=None, raises=None):
        self.calls = []
        self._response, self._raises = response, raises
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._response


def _payload(**over):
    base = {"verdict": "code_fix", "root_cause": "rc", "evidence": ["e"], "confidence": 0.8,
            "edits": [{"path": "models/x.sql", "old": "a", "new": "b"}], "upstream_action": "",
            "self_assessed_risk": "low", "risk_rationale": "r"}
    return {**base, **over}


BUNDLE = {
    "run": {"status_counts": {"fail": 1}},
    "failures": [{"name": "unique_x_id", "model": "x", "fix_risk": {"tier": "auto"}}],
    "models": {"x": {"file": "models/x.sql", "raw_sql": "select 1"}},
    "_meta": {"scenario_id": "giveaway_scenario_name"},
}


def test_model_never_sees_meta_or_routing():
    text = agent.user_message(BUNDLE)
    assert "giveaway_scenario_name" not in text and "_meta" not in text and "fix_risk" not in text
    assert "unique_x_id" in text
    assert BUNDLE["failures"][0]["fix_risk"]  # caller's bundle not mutated


def test_system_prompt_is_deterministic_and_carries_policy(policy):
    a = agent.system_prompt(REPO_ROOT, policy)
    assert a == agent.system_prompt(REPO_ROOT, policy)  # byte-stable: cacheable
    assert "`remediation/**`" in a and "{protected_paths}" not in a
    assert "### macros/week_bounds.sql" in a and "Never modify an existing test" in a


def test_request_shape(policy):
    client = FakeClient(_response(_payload()))
    agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy)
    [call] = client.calls
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == [agent.FALLBACK_BETA]
    assert call["thinking"] == {"type": "adaptive"} and call["output_config"] == {"effort": "high"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert call["output_format"] is agent.Diagnosis


def test_success_record(policy):
    record = agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(_response(_payload())), policy=policy)
    assert record["error"] is None and record["diagnosis"]["verdict"] == "code_fix"
    assert record["usage"]["cost_usd"] == pytest.approx((1000 * 4 + 500 * 20 + 6000 * 0.2) / 1e6, abs=1e-4)


def test_edits_on_non_code_verdict_are_discarded(policy):
    record = agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(_response(_payload(verdict="data_issue"))), policy=policy)
    assert record["diagnosis"]["edits"] == [] and "discarded" in record["warnings"][0]


def test_confidence_clamped(policy):
    record = agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(_response(_payload(confidence=1.7))), policy=policy)
    assert record["diagnosis"]["confidence"] == 1.0 and record["warnings"]


@pytest.mark.parametrize("stop_reason, message", [("refusal", "declined"), ("max_tokens", "truncated")])
def test_bad_stop_reasons_are_errors(policy, stop_reason, message):
    record = agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(_response(_payload(), stop_reason)), policy=policy)
    assert record["diagnosis"] is None and message in record["error"]


def test_unparsed_output_is_an_error(policy):
    record = agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(_response(_payload(), parsed=False)), policy=policy)
    assert "did not parse" in record["error"]


def test_api_errors_are_recorded_not_raised(policy):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=request), body=None)
    record = agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(raises=err), policy=policy)
    assert "ANTHROPIC_API_KEY" in record["error"]


def test_render_shows_verdict_and_edits(policy):
    record = agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(_response(_payload())), policy=policy)
    md = agent.render(record)
    assert "`code_fix`" in md and "- a" in md and "+ b" in md


# -- local response cache ------------------------------------------------------

@pytest.fixture
def cache(tmp_path):
    return agent.ResponseCache(tmp_path / "cache", ttl_hours=24)


def test_identical_request_is_replayed_not_resent(policy, cache):
    client = FakeClient(_response(_payload()))
    first = agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy, cache=cache)
    second = agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy, cache=cache)
    assert len(client.calls) == 1
    assert second["diagnosis"] == first["diagnosis"] and second["cache"]["hit"]
    assert first["cost_usd"] > 0 and second["cost_usd"] == 0.0
    assert "replayed from local cache" in agent.render(second)


def test_cache_entry_holds_the_full_prompt_and_response(policy, cache):
    agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(_response(_payload())), policy=policy, cache=cache)
    [path] = list(cache.directory.glob("*.json"))
    entry = json.loads(path.read_text())
    assert "unique_x_id" in entry["request"]["messages"][0]["content"]
    assert entry["request"]["system"][0]["text"].startswith("You diagnose")
    assert json.loads(entry["response"]["text"])["verdict"] == "code_fix"


def test_expired_entries_are_ignored_and_pruned(policy, tmp_path):
    expired = agent.ResponseCache(tmp_path / "c", ttl_hours=0)
    client = FakeClient(_response(_payload()))
    agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy, cache=expired)
    agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy, cache=expired)
    assert len(client.calls) == 2


def test_fresh_skips_the_read_but_refreshes_the_entry(policy, cache):
    client = FakeClient(_response(_payload()))
    agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy, cache=cache)
    agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy, cache=cache, fresh=True)
    assert len(client.calls) == 2 and len(list(cache.directory.glob("*.json"))) == 1


def test_paid_refusals_are_cached_too(policy, cache):
    client = FakeClient(_response(_payload(), stop_reason="refusal"))
    for _ in range(2):
        record = agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy, cache=cache)
    assert len(client.calls) == 1 and "declined" in record["error"]


def test_exceptions_are_never_cached(policy, cache):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.APIConnectionError(request=request)
    agent.diagnose(BUNDLE, REPO_ROOT, client=FakeClient(raises=err), policy=policy, cache=cache)
    assert not cache.directory.exists() or not list(cache.directory.glob("*.json"))


def test_per_run_noise_does_not_change_the_key(policy):
    a = {**BUNDLE, "generated_at": "2026-10-20T10:00:00", "run": {"invocation_id": "1", "elapsed_seconds": 2.0}}
    b = {**BUNDLE, "generated_at": "2026-10-20T11:00:00", "run": {"invocation_id": "2", "elapsed_seconds": 3.1}}
    key = lambda bundle: agent.cache_key(agent.request_params(bundle, REPO_ROOT, policy))  # noqa: E731
    assert key(a) == key(b)
    changed = {**a, "failures": [{"name": "not_null_x_id"}]}
    assert key(changed) != key(a)


def test_missing_credentials_is_a_clear_error_not_a_crash(policy, cache, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)

    def never_send(request):
        raise AssertionError("a request was sent: credentials were found after all")

    client = anthropic.Anthropic(http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(never_send)))
    record = agent.diagnose(BUNDLE, REPO_ROOT, client=client, policy=policy, cache=cache)
    assert "no API key found" in record["error"] and record["cost_usd"] == 0.0
    assert not list(cache.directory.glob("*.json"))  # nothing cached
