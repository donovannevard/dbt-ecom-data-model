"""Diagnose -> patch -> validate, retrying with the validator's feedback.

The loop Phase 4 will run before it creates a branch:

  1. Ask Claude (or take an existing diagnosis) for a verdict and edits.
  2. Anything other than a code fix ends here: there is nothing to patch.
  3. Copy the base project, apply the edits, and validate the copy.
  4. On rejection, ask again with every rejected attempt and its reason,
     up to validator.max_retries more times.

Each retry is a new API call (cached like any other). None of this touches a
branch, so these attempts don't count towards max_ci_failures.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from remediation.agent import diagnose
from remediation.edits import EditError, apply_edits
from remediation.validate import Validation, Validator

COPY_IGNORE = shutil.ignore_patterns("target", "logs", "*.duckdb", "*.duckdb.wal")


@dataclass
class Outcome:
    status: str  # validated | not_a_code_fix | rejected | error
    record: dict | None = None  # the final diagnosis record
    validation: Validation | None = None
    attempts: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "cost_usd": round(self.cost_usd, 4),
            "validation": self.validation.to_dict() if self.validation else None,
        }


def prepare_patch(base: Path, dest: Path, edits: list[dict]) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(base, dest, ignore=COPY_IGNORE)
    apply_edits(dest, edits)


def remediate(bundle: dict, base: Path, patched: Path, log_path: Path, policy: dict, *,
              base_db: Path | None = None, first: dict | None = None, ask=None,
              max_retries: int | None = None, previous: list[dict] | None = None) -> Outcome:
    """base: the built project the failure came from. patched: where the
    candidate copy is made (replaced on each attempt). first: an existing
    diagnosis record to try before asking for anything. ask: callable(bundle,
    project, previous) -> record; defaults to agent.diagnose with no cache.
    """
    ask = ask or (lambda b, p, previous: diagnose(b, p, policy=policy, previous=previous))
    retries = policy["validator"]["max_retries"] if max_retries is None else max_retries
    validator = Validator(base, policy, base_db)
    out = Outcome(status="error")
    # previous: earlier attempts that failed somewhere else (CI, in Phase 5);
    # the model sees them alongside any validator rejections from this round.
    feedback: list[dict] = list(previous or [])
    record = first

    for attempt in range(retries + 1):
        if record is None:
            record = ask(bundle, base, feedback or None)
        out.record = record
        out.cost_usd += record.get("cost_usd") or 0
        d = record.get("diagnosis")
        entry = {"attempt": attempt + 1, "verdict": d and d["verdict"], "error": record.get("error"),
                 "cost_usd": record.get("cost_usd") or 0}
        out.attempts.append(entry)
        if record.get("error") or not d:
            out.status = "error"
            return out
        if d["verdict"] != "code_fix" or not d["edits"]:
            out.status = "not_a_code_fix"
            return out

        try:
            prepare_patch(base, patched, d["edits"])
        except EditError as exc:
            rejection = f"edits: {exc}"
            out.validation = None
        else:
            out.validation = validator.validate(patched, log_path, d["confidence"])
            rejection = out.validation.rejection
            if out.validation.passed:
                entry["validation"] = "passed"
                out.status = "validated"
                return out
        entry["validation"] = rejection
        feedback.append({"edits": d["edits"], "rejection": rejection})
        record = None  # next attempt asks again, with feedback

    out.status = "rejected"
    return out


def save(outcome: Outcome, out_dir: Path) -> None:
    from remediation.validate import render

    (out_dir / "remediation.json").write_text(json.dumps(outcome.to_dict(), indent=2) + "\n")
    if outcome.validation:
        (out_dir / "validation.md").write_text(render(outcome.validation))
