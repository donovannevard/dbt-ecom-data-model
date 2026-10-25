"""Ask Claude to diagnose a diagnostic bundle (Phase 2: read-only).

One structured-output call per bundle: a verdict (code_fix / data_issue /
needs_human), the root cause with evidence, a calibrated confidence and, for
a code fix, exact search-and-replace edits. Nothing here writes to the
project. The edits are applied and proven by the Phase 3 validator.

What the model sees is deliberately limited:
  - the bundle minus `_meta` (scenario bookkeeping that gives the answer away),
    minus `fix_risk` (routing is decided from lineage, not by the model, and
    its own risk view should be independent of it), and minus per-run noise
    (timestamps, invocation ids) that would make identical failures look new;
  - a stable system prompt: rules, protected paths from policy.yaml, and the
    project's macro source, cached across calls.

Every response that was paid for is kept in a local cache (ResponseCache),
keyed by a hash of the entire request, so re-running after an error never
pays twice for the same question.

    python -m remediation.agent --bundle target/remediation/bundle.json --out target/remediation
"""

import argparse
import copy
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, ValidationError

from remediation.config import PACKAGE_DIR, REPO_ROOT, load_policy

MODEL = "claude-opus-5-5"
# Diagnosis is intelligence-sensitive and low-volume: tens of calls, not
# thousands. Opus 5.5 defaults to medium, so this is set explicitly.
EFFORT = "high"
# Non-streaming ceiling the SDK accepts without a long-request error. Edits
# are short; truncation is caught as an error rather than half-parsed.
MAX_TOKENS = 16000
# If a safety classifier declines, re-run server-side on the model Anthropic
# recommends for that refusal category rather than failing the diagnosis.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# The system prompt (rules + macro source, ~11k tokens) is the same for every
# scenario, but diagnoses run minutes apart while someone reviews each one, so
# the default 5-minute prompt cache would expire between calls and every call
# would pay the write premium for nothing. One hour costs more to write once
# and makes every later call in the session a cheap cache read.
PROMPT_CACHE_TTL = "1h"

# USD per million tokens, for the cost line in reports. Estimates only.
# cache_write is the 1-hour rate, matching PROMPT_CACHE_TTL.
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write": 8.00, "cache_read": 0.20},
}

CACHE_DIR = REPO_ROOT / ".remediation-cache" / "claude"

STRIPPED_KEYS = ("_meta", "generated_at")
STRIPPED_RUN_KEYS = ("invocation_id", "elapsed_seconds")
STRIPPED_FAILURE_KEYS = ("fix_risk",)


class Edit(BaseModel):
    path: str
    old: str
    new: str


class Diagnosis(BaseModel):
    verdict: Literal["code_fix", "data_issue", "needs_human"]
    root_cause: str
    evidence: list[str]
    confidence: float
    edits: list[Edit]
    upstream_action: str
    self_assessed_risk: Literal["low", "medium", "high"]
    risk_rationale: str


# -- prompt -------------------------------------------------------------------

def project_context(project_dir: Path) -> str:
    """dbt_project.yml and every project macro, in a fixed order so the cached
    prefix is byte-identical across calls on the same code."""
    parts = ["## Project context", "", "### dbt_project.yml", "```yaml",
             (project_dir / "dbt_project.yml").read_text().rstrip(), "```"]
    for path in sorted((project_dir / "macros").rglob("*.sql")):
        rel = path.relative_to(project_dir).as_posix()
        parts += ["", f"### {rel}", "```sql", path.read_text().rstrip(), "```"]
    return "\n".join(parts) + "\n"


def system_prompt(project_dir: Path, policy: dict) -> str:
    rules = (PACKAGE_DIR / "prompts" / "system.md").read_text()
    protected = ", ".join(f"`{p}`" for p in policy["protected_paths"])
    return rules.replace("{protected_paths}", protected) + "\n" + project_context(project_dir)


def model_view(bundle: dict) -> dict:
    view = {k: v for k, v in copy.deepcopy(bundle).items() if k not in STRIPPED_KEYS}
    for key in STRIPPED_RUN_KEYS:
        view.get("run", {}).pop(key, None)
    for failure in view.get("failures", []):
        for key in STRIPPED_FAILURE_KEYS:
            failure.pop(key, None)
    return view


def user_message(bundle: dict, previous: list[dict] | None = None) -> str:
    text = (
        "Diagnostic bundle from a failed dbt build:\n\n```json\n"
        + json.dumps(model_view(bundle), indent=1)
        + "\n```\n"
    )
    if previous:
        text += ("\n## Previous attempts\n\nYour earlier proposals for this failure were applied and "
                 "rejected by the automated validator. Work out why before proposing again, and do not "
                 "repeat a rejected change.\n")
        for i, attempt in enumerate(previous, 1):
            text += (f"\n### Attempt {i}\n\nEdits:\n```json\n{json.dumps(attempt['edits'], indent=1)}\n```\n\n"
                     f"Rejected: {attempt['rejection']}\n")
    return text + "\nDiagnose the failure and respond in the required format."


def request_params(bundle: dict, project_dir: Path, policy: dict, previous: list[dict] | None = None) -> dict:
    """Everything sent to the API except the response type, as plain data."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
        "system": [{"type": "text", "text": system_prompt(project_dir, policy),
                    "cache_control": {"type": "ephemeral", "ttl": PROMPT_CACHE_TTL}}],
        "messages": [{"role": "user", "content": user_message(bundle, previous)}],
    }


# -- local response cache ---------------------------------------------------------

def cache_key(params: dict) -> str:
    # The output schema is part of the question: change Diagnosis, and old
    # answers no longer fit.
    keyed = {**params, "output_schema": Diagnosis.model_json_schema()}
    return hashlib.sha256(json.dumps(keyed, sort_keys=True).encode()).hexdigest()


class ResponseCache:
    """Paid-for responses on disk, one JSON file per request hash.

    Holds the full request (prompt included) and the response, so an entry is
    also an audit record of exactly what was asked. Entries older than the TTL
    are ignored and pruned. Separate from the API's own prompt caching, which
    discounts a repeated prefix for minutes; this skips the call entirely.
    """

    def __init__(self, directory: Path = CACHE_DIR, ttl_hours: float = 24):
        self.directory = directory
        self.ttl = dt.timedelta(hours=ttl_hours)

    @classmethod
    def from_policy(cls, policy: dict, directory: Path = CACHE_DIR) -> "ResponseCache":
        return cls(directory, policy["agent"]["response_cache_hours"])

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def _expired(self, entry: dict) -> bool:
        saved = dt.datetime.fromisoformat(entry["saved_at"])
        return dt.datetime.now(dt.timezone.utc) - saved > self.ttl

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if self._expired(entry):
            path.unlink(missing_ok=True)
            return None
        return entry

    def put(self, key: str, params: dict, snapshot: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {"key": key, "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "request": params, "response": snapshot}
        self._path(key).write_text(json.dumps(entry, indent=1) + "\n")
        self.prune()

    def prune(self) -> None:
        for path in self.directory.glob("*.json"):
            try:
                if self._expired(json.loads(path.read_text())):
                    path.unlink()
            except (OSError, ValueError, KeyError):
                path.unlink(missing_ok=True)


# -- call -----------------------------------------------------------------------

def _usage(response) -> dict:
    u = response.usage
    usage = {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
    }
    price = PRICES.get(response.model)
    usage["cost_usd"] = None if price is None else round(
        (usage["input_tokens"] * price["input"]
         + usage["output_tokens"] * price["output"]
         + usage["cache_creation_input_tokens"] * price["cache_write"]
         + usage["cache_read_input_tokens"] * price["cache_read"]) / 1e6, 4)
    return usage


def _fallback_used(response) -> bool:
    return any(getattr(i, "type", None) == "fallback_message"
               for i in (getattr(response.usage, "iterations", None) or []))


def snapshot(response) -> dict:
    """The parts of an API response we keep, as plain JSON-able data."""
    details = getattr(response, "stop_details", None)
    return {
        "served_by": response.model,
        "request_id": getattr(response, "_request_id", None),
        "stop_reason": response.stop_reason,
        "refusal_category": getattr(details, "category", None) if details else None,
        "fallback_used": _fallback_used(response),
        "usage": _usage(response),
        "text": next((b.text for b in response.content if getattr(b, "type", None) == "text"), None),
    }


def record_from_snapshot(snap: dict) -> dict:
    """Turn a response (fresh or cached) into a result record. One code path,
    so a cached answer is judged exactly as it was the first time."""
    record = {"schema_version": 1, "model": MODEL, "effort": EFFORT,
              "diagnosis": None, "error": None, "warnings": [],
              **{k: snap[k] for k in ("served_by", "request_id", "stop_reason", "fallback_used", "usage")},
              "cost_usd": snap["usage"].get("cost_usd")}
    if snap["stop_reason"] == "refusal":
        record["error"] = f"declined ({snap.get('refusal_category') or 'no category'})"
        return record
    if snap["stop_reason"] == "max_tokens":
        record["error"] = f"output truncated at max_tokens={MAX_TOKENS}"
        return record
    try:
        diagnosis = Diagnosis.model_validate_json(snap["text"] or "").model_dump()
    except ValidationError:
        record["error"] = "response did not parse as a Diagnosis"
        return record

    if not 0.0 <= diagnosis["confidence"] <= 1.0:
        record["warnings"].append(f"confidence {diagnosis['confidence']} clamped to [0, 1]")
        diagnosis["confidence"] = min(max(diagnosis["confidence"], 0.0), 1.0)
    if diagnosis["verdict"] != "code_fix" and diagnosis["edits"]:
        # Only a code_fix may carry edits; anything else would be a patch the
        # model itself doesn't stand behind.
        record["warnings"].append(f"{len(diagnosis['edits'])} edit(s) discarded: verdict is {diagnosis['verdict']}")
        diagnosis["edits"] = []
    if diagnosis["verdict"] == "code_fix" and not diagnosis["edits"]:
        record["warnings"].append("code_fix verdict with no edits")
    record["diagnosis"] = diagnosis
    return record


def diagnose(bundle: dict, project_dir: Path, client=None, policy: dict | None = None,
             cache: ResponseCache | None = None, fresh: bool = False,
             previous: list[dict] | None = None) -> dict:
    """Returns a result record; `diagnosis` is None and `error` set on failure.

    With a cache, an identical request answered within the TTL is replayed
    instead of sent (unless fresh=True). Only responses the API returned are
    cached: exceptions mean nothing was answered, so a retry is the point.
    """
    policy = policy or load_policy()
    params = request_params(bundle, project_dir, policy, previous)
    key = cache_key(params)

    if cache and not fresh:
        entry = cache.get(key)
        if entry:
            record = record_from_snapshot(entry["response"])
            record["cost_usd"] = 0.0
            record["cache"] = {"hit": True, "key": key, "saved_at": entry["saved_at"]}
            return record

    client = client or anthropic.Anthropic()
    error_record = {"schema_version": 1, "model": MODEL, "effort": EFFORT,
                    "diagnosis": None, "warnings": [], "cost_usd": 0.0}
    try:
        response = client.beta.messages.parse(**params, output_format=Diagnosis)
    except TypeError as exc:
        # Raised by the SDK before any request is sent when it finds no
        # credentials at all. Nothing was billed.
        if "authentication method" not in str(exc):
            raise
        return {**error_record, "error": "no API key found: export ANTHROPIC_API_KEY in this terminal, "
                                         "or add it to .env (nothing was sent or billed)"}
    except anthropic.AuthenticationError:
        return {**error_record, "error": "authentication failed: set ANTHROPIC_API_KEY"}
    except anthropic.BadRequestError as exc:
        return {**error_record, "error": f"bad request: {exc.message}"}
    except anthropic.RateLimitError:
        return {**error_record, "error": "rate limited after SDK retries"}
    except anthropic.APIStatusError as exc:
        return {**error_record, "error": f"API error {exc.status_code}: {exc.message}"}
    except anthropic.APIConnectionError:
        return {**error_record, "error": "could not reach the API"}

    snap = snapshot(response)
    if cache:
        cache.put(key, params, snap)
    record = record_from_snapshot(snap)
    record["cache"] = {"hit": False, "key": key}
    return record


# -- report -----------------------------------------------------------------------

def render(record: dict, title: str = "Claude diagnosis") -> str:
    lines = [f"## {title}", ""]
    if record.get("error"):
        lines += [f"**Error:** {record['error']}", ""]
    d = record.get("diagnosis")
    if d:
        lines += [
            f"**Verdict:** `{d['verdict']}`, confidence {d['confidence']:.2f}, "
            f"self-assessed risk {d['self_assessed_risk']}", "",
            f"**Root cause:** {d['root_cause']}", "",
        ]
        if d["evidence"]:
            lines += ["**Evidence:**"] + [f"- {e}" for e in d["evidence"]] + [""]
        if d["upstream_action"]:
            lines += [f"**Upstream action:** {d['upstream_action']}", ""]
        for i, e in enumerate(d["edits"], 1):
            lines += [f"**Edit {i}:** `{e['path']}`", "```diff"]
            lines += [f"- {line}" for line in e["old"].splitlines()]
            lines += [f"+ {line}" for line in e["new"].splitlines()]
            lines += ["```", ""]
        lines += [f"**Risk rationale:** {d['risk_rationale']}", ""]
    for w in record.get("warnings", []):
        lines.append(f"> warning: {w}")
    if record.get("usage"):
        u = record["usage"]
        served = record.get("served_by", MODEL)
        tokens = (f"{u['input_tokens']} in + {u['cache_read_input_tokens']} cached + "
                  f"{u['cache_creation_input_tokens']} cache-write + {u['output_tokens']} out")
        cache = record.get("cache") or {}
        if cache.get("hit"):
            cost = f"replayed from local cache (saved {cache['saved_at']}), $0 this run"
        else:
            cost = f"~${u['cost_usd']:.4f}" if u.get("cost_usd") is not None else "cost n/a"
        lines += ["", f"<sub>{served}{' (fallback)' if record.get('fallback_used') else ''}, {tokens}, {cost}</sub>"]
    return "\n".join(lines) + "\n"


def write_record(record: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "diagnosis.json").write_text(json.dumps(record, indent=2) + "\n")
    (out_dir / "diagnosis.md").write_text(render(record))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, default=Path("."),
                        help="checkout the bundle was built from (for the macro catalogue)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fresh", action="store_true", help="ignore the local response cache")
    args = parser.parse_args(argv)

    bundle = json.loads(args.bundle.read_text())
    if not bundle.get("failures"):
        print("bundle has no failures; nothing to diagnose")
        return 0
    policy = load_policy()
    record = diagnose(bundle, args.project_dir.resolve(), policy=policy,
                      cache=ResponseCache.from_policy(policy), fresh=args.fresh)
    write_record(record, args.out)
    print(render(record))
    return 1 if record["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
