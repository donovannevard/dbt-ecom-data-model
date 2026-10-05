#!/usr/bin/env python3
"""Run a command with .env applied, without shell interpretation of values.

    python scripts/with_env.py dbt build --target redshift_prod

dbt Core does not read .env itself. This is the shim that makes it behave as
though it did, without the value-mangling that sourcing the file in a shell
causes (see scripts/dotenv.py).
"""

import os
import sys

from env_file import load

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: with_env.py <command> [args...]", file=sys.stderr)
        raise SystemExit(2)

    load()
    os.execvp(sys.argv[1], sys.argv[1:])
