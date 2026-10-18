#!/usr/bin/env python3
"""Import the reference Airflow DAGs and assert their shape.

Run inside an environment that has Airflow and astronomer-cosmos installed
(`make verify-airflow-examples` builds one). Imports each DAG file for real,
so this catches an API change in Airflow or Cosmos, not merely a typo.

    python orchestration/external/parse_check.py orchestration/external/*.py
"""

import importlib.util
import sys
from pathlib import Path

EXPECTATIONS = {
    # file stem -> (minimum task count, a task_id that must be present)
    "airflow_cosmos_dag": (10, "load_raw_landing_tables"),
    "airflow_bash_dag": (3, "dbt_build"),
}


def check(path: Path) -> bool:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    dag = module.dag
    task_ids = {t.task_id for t in dag.tasks}
    min_tasks, required = EXPECTATIONS[path.stem]

    if len(task_ids) < min_tasks:
        print(f"FAILED  {path.name}: {len(task_ids)} tasks, expected >= {min_tasks}")
        return False
    if required not in task_ids:
        print(f"FAILED  {path.name}: no '{required}' task")
        return False

    print(f"OK      {path.name}: dag_id={dag.dag_id!r}, {len(task_ids)} tasks")
    return True


def main() -> int:
    targets = [Path(a) for a in sys.argv[1:] if Path(a).stem in EXPECTATIONS]
    if not targets:
        print("No reference DAG files given.")
        return 1
    return 0 if all(check(p) for p in sorted(targets)) else 1


if __name__ == "__main__":
    sys.exit(main())
