"""Minimal .env loader that treats values literally.

Named env_file rather than dotenv on purpose: python-dotenv ships a module
called `dotenv` and Dagster depends on it, so a local `dotenv.py` would shadow
it or be shadowed depending on sys.path order.

Why not `set -a; . ./.env`, which is the usual shell idiom: sourcing makes the
shell parse each line as an assignment, so characters that are significant to
the shell are consumed rather than passed through. A password containing a
backslash loses it, `$VAR` is expanded to something else, and backticks execute.
The failure is silent — the value simply arrives wrong, and the warehouse
reports an authentication error that points nowhere near the real cause.

Parsing it here instead: split on the first `=`, strip one layer of matching
surrounding quotes, and otherwise take the value exactly as written.

Existing environment variables win over the file, which is deliberate: a real
secret exported by CI must never be shadowed by a file that happens to be on
disk.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Which file to read. Overridable so credentials for different warehouses can
# live side by side instead of overwriting each other:
#
#     ENV_FILE=.env.snowflake make verify-warehouse TARGET=snowflake_prod
#     ENV_FILE=.env.redshift  make verify-warehouse TARGET=redshift_prod
#
# profiles.yml reads the same DB_TRANSFORM_USER / DB_TRANSFORM_PASSWORD for
# every warehouse, so without this you can only hold one set at a time.
def default_path() -> Path:
    override = os.environ.get("ENV_FILE")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
    return PROJECT_ROOT / ".env"


DEFAULT_PATH = PROJECT_ROOT / ".env"


def parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()

        # One layer of matching quotes, if present. Anything inside is literal.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        values[key] = value

    return values


def load(path: Path | None = None) -> list[str]:
    """Apply the file to os.environ. Returns the names actually set."""
    if path is None:
        path = default_path()
    applied = []
    for key, value in parse(path).items():
        if os.environ.get(key):
            continue  # already set in the real environment; leave it alone
        os.environ[key] = value
        applied.append(key)
    return applied
