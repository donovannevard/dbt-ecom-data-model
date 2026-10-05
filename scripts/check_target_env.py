#!/usr/bin/env python3
"""Report which environment variables a dbt target needs, and which are missing.

dbt Core reads credentials from the process environment only -- it does NOT
read a .env file (dbt Cloud and Dagster do, which is a common source of
confusion). Without the variables exported, dbt fails with a terse
"Env var required but not provided", which does not say which one.

    python scripts/check_target_env.py snowflake_prod

Exits non-zero if anything required is missing, so it works as a Make
precondition.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env_file import load as load_env_file  # noqa: E402

PROFILES = Path(__file__).resolve().parent.parent / "profiles.yml"

# env_var('NAME')            -> required
# env_var('NAME', 'default') -> optional, dbt substitutes the default
REQUIRED = re.compile(r"env_var\(\s*'([A-Z0-9_]+)'\s*\)")
OPTIONAL = re.compile(r"env_var\(\s*'([A-Z0-9_]+)'\s*,")

# Values copied straight from a .example template and never filled in. These are
# technically "set", so a presence check passes and the run then fails with an
# authentication error that says nothing about the real cause. Worth catching
# here instead.
PLACEHOLDERS = {"xxx", "xxxx", "changeme", "change_me", "todo", "...", "<fill-in>", "your-value-here"}


def classify(name: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return "missing"
    if raw.strip().lower() in PLACEHOLDERS:
        return "placeholder"
    return "set"


def target_block(text: str, target: str) -> str:
    """The indented YAML block for one target, read textually.

    Deliberately not parsed with a YAML library: the values contain Jinja that
    a loader would either choke on or silently render, and all this needs is
    the raw env_var() calls.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == f"{target}:"), None
    )
    if start is None:
        return ""
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = []
    for line in lines[start + 1 :]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        block.append(line)
    return "\n".join(block)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_target_env.py <target>", file=sys.stderr)
        return 2

    # Same loader the dbt invocations use, so this check and the run that
    # follows it can never disagree about what a variable contains.
    from env_file import default_path

    load_env_file()
    env_path = default_path()
    print(f"  (credentials file: {env_path.name}"
          f"{'' if env_path.exists() else ' — NOT FOUND'})")

    target = sys.argv[1]
    block = target_block(PROFILES.read_text(), target)
    if not block:
        print(f"Target '{target}' not found in {PROFILES.name}.", file=sys.stderr)
        return 2

    required = sorted(set(REQUIRED.findall(block)))
    optional = sorted(set(OPTIONAL.findall(block)))

    if not required and not optional:
        print(f"Target '{target}' needs no credentials.")
        return 0

    states = {v: classify(v) for v in required}
    missing = [v for v, st in states.items() if st == "missing"]
    placeholder = [v for v, st in states.items() if st == "placeholder"]

    labels = {
        "set": "set",
        "missing": "MISSING",
        "placeholder": "PLACEHOLDER — still the template default",
    }
    for var in required:
        print(f"  {var:34} {labels[states[var]]}")
    for var in optional:
        st = classify(var)
        print(f"  {var:34} {'set' if st == 'set' else 'not set (default will apply)'}")

    if missing or placeholder:
        print()
        if missing:
            print(f"{len(missing)} required variable(s) missing for target '{target}'.")
        if placeholder:
            print(
                f"{len(placeholder)} variable(s) still hold a placeholder value: "
                + ", ".join(placeholder)
            )
            print("These would reach the warehouse and fail as an authentication error.")
        print()
        todo = missing + placeholder

        # Point at the template for this warehouse rather than the generic one.
        warehouse = target.split("_")[0] if "_" in target else None
        if warehouse in ("snowflake", "redshift"):
            template = f".env.{warehouse}.example"
            credfile = f".env.{warehouse}"
            selector = f"ENV_FILE={credfile} "
        else:
            template, credfile, selector = ".env.<warehouse>.example", ".env", ""

        print("Set them in the shell:")
        print("    " + "  ".join(f"export {v}=..." for v in todo[:2]) + " ...")
        print()
        print(f"or put them in {credfile} (gitignored) and let make load it:")
        print(f"    cp {template} {credfile}     # then edit")
        print(f"    {selector}make verify-warehouse TARGET={target}")
        print()
        print("Note: dbt itself does not read .env. Only the make targets do.")
        return 1

    print()
    print(f"All required variables present for target '{target}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
