"""Shared loading for policy.yaml and the dbt manifest."""

import fnmatch
import json
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
POLICY_PATH = PACKAGE_DIR / "policy.yaml"
SCENARIO_DIR = PACKAGE_DIR / "scenarios"


def load_policy(path: Path = POLICY_PATH) -> dict:
    policy = yaml.safe_load(path.read_text())
    tiers = policy["tiers"]
    if policy["default_tier"] != tiers[-1]:
        # Defaulting to anything less than the strictest tier would let an
        # unmapped file auto-merge.
        raise ValueError("default_tier must be the most restrictive tier")
    return policy


def load_manifest(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `dbt parse` first")
    return json.loads(path.read_text())


def matches_any(path: str, globs: list[str]) -> bool:
    # fnmatch's `*` already crosses `/`, so `.github/**` and `requirements*.txt`
    # both behave as written.
    return any(fnmatch.fnmatchcase(path, g) for g in globs)
