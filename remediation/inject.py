"""Apply a fault-injection scenario to a scratch checkout.

Scenarios live in remediation/scenarios/*.yaml. Each one breaks the project in
a specific, realistic way — bad source data or a code regression — and states
which tests should fail, which verdicts are acceptable and what risk tier a
fix carries. The expectations are for scoring; they are never shown to the
model (see diagnose.py).

Injection only ever runs against a directory carrying a SCRATCH_MARKER file,
so a broken seed or model can never be written into a real checkout by
accident. chaos.py creates and marks those directories.

    python -m remediation.inject list
    python -m remediation.inject apply <scenario> --root <scratch dir>
"""

import argparse
import csv
import io
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from remediation.config import SCENARIO_DIR

SCRATCH_MARKER = ".remediation-scratch"
KINDS = ("data", "code")
VERDICTS = ("code_fix", "data_issue", "needs_human")


class ScenarioError(ValueError):
    pass


@dataclass
class Scenario:
    id: str
    title: str
    kind: str
    description: str
    faults: list[dict]
    expect: dict

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        raw = yaml.safe_load(path.read_text())
        scenario = cls(**{k: raw[k] for k in cls.__dataclass_fields__})
        scenario._check_shape(path)
        return scenario

    @property
    def absorbed(self) -> bool:
        """The project handles this fault itself: warnings, no failures."""
        return not self.expect.get("failing_tests")

    def _check_shape(self, path: Path) -> None:
        problems = []
        if self.id != path.stem:
            problems.append(f"id {self.id!r} does not match filename")
        if self.kind not in KINDS:
            problems.append(f"kind must be one of {KINDS}")
        if not self.faults:
            problems.append("no faults")
        for fault in self.faults:
            if fault.get("op") not in FAULT_OPS:
                problems.append(f"unknown op {fault.get('op')!r}")
        if not self.expect.get("failing_tests") and not self.expect.get("warning_tests"):
            problems.append("expect needs failing_tests, or warning_tests for a fault the project absorbs")
        if self.absorbed:
            # Nothing fails, so there is nothing to diagnose or route.
            if self.expect.get("verdicts") or self.expect.get("tier"):
                problems.append("an absorbed scenario (no failing_tests) takes no verdicts or tier")
        elif not set(self.expect.get("verdicts", [])) <= set(VERDICTS) or not self.expect.get("verdicts"):
            problems.append(f"expect.verdicts must be a non-empty subset of {VERDICTS}")
        if self.kind == "code" and not self.expect.get("fix_files"):
            problems.append("code scenarios need expect.fix_files")
        if problems:
            raise ScenarioError(f"{path.name}: " + "; ".join(problems))


def load_all(directory: Path = SCENARIO_DIR) -> dict[str, Scenario]:
    scenarios = {}
    for path in sorted(directory.glob("*.yaml")):
        scenario = Scenario.load(path)
        scenarios[scenario.id] = scenario
    return scenarios


# -- fault operations ---------------------------------------------------------
# Each op validates against the target tree before changing anything, and
# check_fault() runs that validation alone, so the unit tests can prove every
# scenario still applies to the current code without building anything.

def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(path.read_text(), newline="")))
    return rows[0], rows[1:]


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    path.write_text(out.getvalue())


def _check_rows(fault: dict, rows: list, indices: list[int]) -> None:
    for i in indices:
        if not 0 <= i < len(rows):
            raise ScenarioError(f"{fault['file']}: row {i} out of range (0..{len(rows) - 1})")


def _duplicate_row(root: Path, fault: dict, dry_run: bool) -> None:
    path = root / fault["file"]
    header, rows = _read_csv(path)
    _check_rows(fault, rows, [fault["row"]])
    if dry_run:
        return
    copies = [list(rows[fault["row"]])] * fault.get("times", 1)
    _write_csv(path, header, rows[: fault["row"] + 1] + copies + rows[fault["row"] + 1:])


def _set_value(root: Path, fault: dict, dry_run: bool) -> None:
    path = root / fault["file"]
    header, rows = _read_csv(path)
    if fault["column"] not in header:
        raise ScenarioError(f"{fault['file']}: no column {fault['column']!r}")
    _check_rows(fault, rows, fault["rows"])
    if dry_run:
        return
    col = header.index(fault["column"])
    for i in fault["rows"]:
        rows[i][col] = str(fault["value"])
    _write_csv(path, header, rows)


def _replace(root: Path, fault: dict, dry_run: bool) -> None:
    path = root / fault["file"]
    text = path.read_text()
    count = text.count(fault["old"])
    if count != 1:
        # Zero means the code moved on and the scenario has rotted; more than
        # one means the injection is ambiguous. Both must be fixed in the YAML.
        raise ScenarioError(f"{fault['file']}: expected 1 occurrence of anchor, found {count}")
    if not dry_run:
        path.write_text(text.replace(fault["old"], fault["new"]))


FAULT_OPS = {
    "duplicate_row": _duplicate_row,
    "set_value": _set_value,
    "replace": _replace,
}


def check_fault(root: Path, fault: dict) -> None:
    if not (root / fault["file"]).is_file():
        raise ScenarioError(f"{fault['file']}: file not found")
    FAULT_OPS[fault["op"]](root, fault, dry_run=True)


def apply(scenario: Scenario, root: Path) -> list[str]:
    """Apply every fault; returns the files changed."""
    root = root.resolve()
    if not (root / SCRATCH_MARKER).exists():
        raise ScenarioError(
            f"{root} is not a scratch checkout (no {SCRATCH_MARKER}); "
            "use `python -m remediation.chaos` to create one"
        )
    # Faults apply in order, each seeing the previous ones' effects, so each is
    # checked just before it runs. Any failure restores every touched file:
    # a scenario never half-applies.
    files = sorted({f["file"] for f in scenario.faults})
    originals = {f: (root / f).read_bytes() for f in files if (root / f).is_file()}
    try:
        for fault in scenario.faults:
            check_fault(root, fault)
            FAULT_OPS[fault["op"]](root, fault, dry_run=False)
    except Exception:
        for f, content in originals.items():
            (root / f).write_bytes(content)
        raise
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    ap = sub.add_parser("apply")
    ap.add_argument("scenario")
    ap.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)

    scenarios = load_all()
    if args.command == "list":
        for s in scenarios.values():
            print(f"{s.id:<34} {s.kind:<5} {s.expect.get('tier', 'absorbed'):<11} {s.title}")
        return 0
    if args.scenario not in scenarios:
        print(f"unknown scenario {args.scenario!r}; see `list`", file=sys.stderr)
        return 2
    for path in apply(scenarios[args.scenario], args.root):
        print(f"injected {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
