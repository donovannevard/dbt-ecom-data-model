"""Risk tier for a set of changed files, computed from dbt lineage.

The tier of a change is the most restrictive tier of every dbt node the
changed files map to AND every node downstream of those. A one-line fix to a
staging model that feeds fct_transactions is a revenue change, whatever the
staging model's own tag says.

The model never decides its own risk. It is computed here from the BASE
branch's manifest, so a patch cannot lower its own tier by editing
meta.remediation_tier — the validator rejects that edit separately.

    python -m remediation.risk assess models/analytics/dim_date.sql
    python -m remediation.risk tiers
    python -m remediation.risk codeowners --check
"""

import argparse
import json
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from remediation.config import REPO_ROOT, load_manifest, load_policy, matches_any

CODEOWNERS_PATH = REPO_ROOT / ".github" / "CODEOWNERS"

# Never built, so changes to them cannot reach data: excluded from the
# downstream walk. A test's tier is its attached model's (see _macro_callers).
SKIPPED_PREFIXES = ("test.", "analysis.")


@dataclass
class Reason:
    file: str
    tier: str
    detail: str
    # Lineage from the touched node to the node that set the tier, inclusive.
    path: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    tier: str
    reasons: list[Reason]

    def to_dict(self) -> dict:
        return {"tier": self.tier, "reasons": [asdict(r) for r in self.reasons]}


class RiskModel:
    def __init__(self, manifest: dict, policy: dict):
        self.manifest = manifest
        self.policy = policy
        self.tiers: list[str] = policy["tiers"]
        self.default = policy["default_tier"]
        self.child_map: dict[str, list[str]] = manifest["child_map"]
        self._all = {
            **manifest["nodes"],
            **manifest.get("sources", {}),
            **manifest.get("exposures", {}),
        }
        self._file_index = self._index_files()
        self._macro_index = self._index_macro_files()

    # -- indexing -----------------------------------------------------------

    def _index_files(self) -> dict[str, set[str]]:
        """Map each repo-relative file to the node ids defined or patched in it."""
        index: dict[str, set[str]] = {}
        for uid, node in self._all.items():
            if uid.startswith(SKIPPED_PREFIXES):
                # A test lives in a YAML file that also patches its model; the
                # model's own entry covers that file.
                continue
            paths = {node.get("original_file_path")}
            patch = node.get("patch_path")  # "<project>://models/x.yaml"
            if patch:
                paths.add(patch.split("://", 1)[-1])
            for p in paths - {None}:
                index.setdefault(p, set()).add(uid)
        return index

    def _index_macro_files(self) -> dict[str, set[str]]:
        index: dict[str, set[str]] = {}
        for uid, macro in self.manifest.get("macros", {}).items():
            if macro.get("package_name") != self.manifest["metadata"].get("project_name"):
                continue
            index.setdefault(macro["original_file_path"], set()).add(uid)
        return index

    def _macro_callers(self, macro_ids: set[str]) -> set[str]:
        """Nodes that call any of these macros, directly or through other macros."""
        macros = self.manifest.get("macros", {})
        reached = set(macro_ids)
        frontier = deque(macro_ids)
        while frontier:
            current = frontier.popleft()
            for uid, macro in macros.items():
                if current in macro["depends_on"]["macros"] and uid not in reached:
                    reached.add(uid)
                    frontier.append(uid)
        callers = set()
        for uid, node in self.manifest["nodes"].items():
            if reached & set(node.get("depends_on", {}).get("macros", [])):
                if uid.startswith("test."):
                    # A macro a test calls changes what that test checks; tier
                    # it as the model the test is attached to.
                    callers.update(node["depends_on"]["nodes"])
                elif not uid.startswith("analysis."):
                    callers.add(uid)
        return callers

    # -- tiers --------------------------------------------------------------

    def _rank(self, tier: str) -> int:
        return self.tiers.index(tier)

    def own_tier(self, uid: str) -> str:
        node = self._all.get(uid, {})
        meta = node.get("config", {}).get("meta") or node.get("meta") or {}
        tier = meta.get("remediation_tier", self.default)
        if tier not in self.tiers:
            # An unknown value is a typo, not a new tier: fail safe.
            return self.default
        return tier

    def _strictest_downstream(self, start: str) -> tuple[str, list[str]]:
        """BFS from start; return the strictest tier reached and the shortest
        lineage path to the first node carrying it."""
        best_tier, best_path = self.own_tier(start), [start]
        if best_tier == self.tiers[-1]:
            return best_tier, best_path
        parent = {start: None}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for child in self.child_map.get(current, []):
                if child in parent or child.startswith(SKIPPED_PREFIXES):
                    continue
                parent[child] = current
                tier = self.own_tier(child)
                if self._rank(tier) > self._rank(best_tier):
                    best_tier, best_path = tier, self._trace(parent, child)
                    if best_tier == self.tiers[-1]:
                        return best_tier, best_path
                queue.append(child)
        return best_tier, best_path

    @staticmethod
    def _trace(parent: dict, node: str) -> list[str]:
        path = []
        while node is not None:
            path.append(node)
            node = parent[node]
        return path[::-1]

    def nodes_for_file(self, path: str) -> set[str]:
        nodes = set(self._file_index.get(path, set()))
        if path in self._macro_index:
            nodes |= self._macro_callers(self._macro_index[path])
        return nodes

    def assess_node(self, uid: str) -> Assessment:
        tier, path = self._strictest_downstream(uid)
        return Assessment(tier, [Reason(uid, tier, _describe(path, tier), _short(path))])

    def assess_files(self, paths: list[str]) -> Assessment:
        reasons: list[Reason] = []
        for path in paths:
            if matches_any(path, self.policy["protected_paths"]):
                reasons.append(Reason(path, self.default, "protected path"))
                continue
            nodes = self.nodes_for_file(path)
            if not nodes:
                reasons.append(Reason(path, self.default, "maps to no dbt node"))
                continue
            file_tier, file_path = self.tiers[0], []
            for uid in sorted(nodes):
                tier, lineage = self._strictest_downstream(uid)
                if self._rank(tier) > self._rank(file_tier) or not file_path:
                    file_tier, file_path = tier, lineage
            detail = _describe(file_path, file_tier)
            if path in self._macro_index:
                detail = f"macro called by {len(nodes)} node(s); {detail}"
            reasons.append(Reason(path, file_tier, detail, _short(file_path)))
        if not reasons:
            return Assessment(self.default, [Reason("", self.default, "no files")])
        tier = max((r.tier for r in reasons), key=self._rank)
        return Assessment(tier, reasons)

    def auto_files(self) -> list[str]:
        """Files a change to which, alone, would be auto tier."""
        candidates = set(self._file_index) | set(self._macro_index)
        return sorted(
            p for p in candidates
            if self.assess_files([p]).tier == self.tiers[0]
        )


def _short(path: list[str]) -> list[str]:
    return [uid.split(".")[-1] for uid in path]


def _describe(path: list[str], tier: str) -> str:
    names = _short(path)
    if len(names) <= 1:
        return f"{names[0] if names else '?'} is tagged {tier}"
    return f"{names[0]} feeds {names[-1]} ({tier}) via {' -> '.join(names)}"


# -- CODEOWNERS -------------------------------------------------------------

def render_codeowners(model: RiskModel) -> str:
    owner = model.policy["codeowner"]
    lines = [
        "# GENERATED by `python -m remediation.risk codeowners --write`. Do not",
        "# edit by hand: remediation/tests checks this file matches the manifest.",
        "#",
        "# Every file needs a code-owner review except the auto-tier files listed",
        "# below, which have no owner. Unowned paths fail safe: a new file is",
        "# owned by default, so it cannot be auto-merged until risk.py says so.",
        "# Pair with branch protection on main ('Require review from Code Owners').",
        "",
        f"* {owner}",
        "",
        "# Auto tier: the file and everything downstream of it is auto.",
    ]
    lines += [f"/{p}" for p in model.auto_files()]
    return "\n".join(lines) + "\n"


# -- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "target" / "manifest.json")
    sub = parser.add_subparsers(dest="command", required=True)
    assess = sub.add_parser("assess", help="tier for a set of changed files")
    assess.add_argument("files", nargs="+")
    sub.add_parser("tiers", help="tier of every model, seed and snapshot")
    owners = sub.add_parser("codeowners", help="generate .github/CODEOWNERS")
    mode = owners.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    model = RiskModel(load_manifest(args.manifest), load_policy())

    if args.command == "assess":
        print(json.dumps(model.assess_files(args.files).to_dict(), indent=2))
    elif args.command == "tiers":
        for uid in sorted(model.manifest["nodes"]):
            if uid.split(".")[0] in ("model", "seed", "snapshot"):
                a = model.assess_node(uid)
                print(f"{a.tier:<11} {uid.split('.')[-1]:<48} {a.reasons[0].detail}")
    elif args.command == "codeowners":
        rendered = render_codeowners(model)
        if args.check:
            current = CODEOWNERS_PATH.read_text() if CODEOWNERS_PATH.exists() else ""
            if current != rendered:
                print("CODEOWNERS is stale: run `python -m remediation.risk codeowners --write`")
                return 1
            print("CODEOWNERS up to date")
        elif args.write:
            CODEOWNERS_PATH.write_text(rendered)
            print(f"wrote {CODEOWNERS_PATH.relative_to(REPO_ROOT)}")
        else:
            print(rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
