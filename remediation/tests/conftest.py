import pytest

from remediation.config import REPO_ROOT, load_manifest, load_policy


def synthetic_manifest() -> dict:
    """A small graph exercising every escalation path risk.py handles.

        stg_a (auto) -> int_a (auto) -> fct_rev (quarantine)
        stg_b (auto) -> dim_b (auto)
        macro outer -> macro inner;  dim_b calls outer, fct_rev calls shared
        test on dim_b calls test_macro
    """
    def model(name, tier=None, file=None, patch=None, macros=()):
        meta = {"remediation_tier": tier} if tier else {}
        return {
            "name": name, "resource_type": "model",
            "original_file_path": file or f"models/{name}.sql",
            "patch_path": f"proj://{patch}" if patch else None,
            "config": {"meta": meta}, "depends_on": {"macros": list(macros), "nodes": []},
        }

    def macro(name, file, deps=()):
        return {"package_name": "proj", "original_file_path": file, "depends_on": {"macros": list(deps)}}

    nodes = {
        "model.proj.stg_a": model("stg_a", "auto", patch="models/schema.yml"),
        "model.proj.int_a": model("int_a", "auto"),
        "model.proj.fct_rev": model("fct_rev", "quarantine", macros=["macro.proj.shared"]),
        "model.proj.stg_b": model("stg_b", "auto", patch="models/schema_b.yml"),
        "model.proj.dim_b": model("dim_b", "auto", patch="models/schema_b.yml", macros=["macro.proj.outer"]),
        "model.proj.untagged": model("untagged"),
        "model.proj.typo": model("typo", "autoo"),
        "test.proj.check_dim_b": {
            "name": "check_dim_b", "resource_type": "test", "original_file_path": "models/schema_b.yml",
            "config": {"meta": {}}, "attached_node": "model.proj.dim_b",
            "depends_on": {"macros": ["macro.proj.test_macro"], "nodes": ["model.proj.dim_b"]},
        },
    }
    return {
        "metadata": {"project_name": "proj"},
        "nodes": nodes,
        "sources": {},
        "macros": {
            "macro.proj.inner": macro("inner", "macros/inner.sql"),
            "macro.proj.outer": macro("outer", "macros/outer.sql", ["macro.proj.inner"]),
            "macro.proj.shared": macro("shared", "macros/shared.sql"),
            "macro.proj.test_macro": macro("test_macro", "macros/test_macro.sql"),
        },
        "child_map": {
            "model.proj.stg_a": ["model.proj.int_a"],
            "model.proj.int_a": ["model.proj.fct_rev"],
            "model.proj.stg_b": ["model.proj.dim_b"],
            "model.proj.dim_b": ["test.proj.check_dim_b"],
        },
        "parent_map": {},
    }


@pytest.fixture
def policy():
    return load_policy()


@pytest.fixture
def manifest():
    return synthetic_manifest()


@pytest.fixture(scope="session")
def real_manifest():
    path = REPO_ROOT / "target" / "manifest.json"
    if not path.exists():
        pytest.skip("target/manifest.json missing — run `dbt parse`")
    return load_manifest(path)
