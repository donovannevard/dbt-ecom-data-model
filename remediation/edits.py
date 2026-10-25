"""Exact search-and-replace edits: the patch format the model proposes.

Each edit replaces `old` with `new` in `path`, and `old` must occur exactly
once in the file at the moment the edit applies. Edits apply in order, each
seeing the previous ones' effects, all-or-nothing.

Chosen over unified diffs because a diff's line numbers and context lines are
easy for a model to get subtly wrong, and a mis-anchored hunk can apply
somewhere unintended. An exact-once anchor either matches or is rejected.
"""

from pathlib import Path


class EditError(ValueError):
    pass


def _check_path(root: Path, path: str) -> Path:
    target = (root / path).resolve()
    if root.resolve() not in target.parents:
        raise EditError(f"{path}: outside the project")
    return target


def _apply_one(root: Path, edit: dict, dry: dict[str, str]) -> None:
    path = edit["path"]
    target = _check_path(root, path)
    if path not in dry:
        if not target.is_file():
            raise EditError(f"{path}: file not found")
        dry[path] = target.read_text()
    count = dry[path].count(edit["old"]) if edit["old"] else 0
    if count != 1:
        raise EditError(f"{path}: `old` must occur exactly once, found {count}")
    dry[path] = dry[path].replace(edit["old"], edit["new"])


def check_edits(root: Path, edits: list[dict]) -> list[str]:
    """Problems that would stop these edits applying; empty if they apply."""
    staged: dict[str, str] = {}
    for i, edit in enumerate(edits):
        try:
            _apply_one(root, edit, staged)
        except EditError as exc:
            return [f"edit {i + 1}: {exc}"]
    return []


def apply_edits(root: Path, edits: list[dict]) -> list[str]:
    """Apply in order, all-or-nothing; returns the files changed."""
    staged: dict[str, str] = {}
    for i, edit in enumerate(edits):
        try:
            _apply_one(root, edit, staged)
        except EditError as exc:
            raise EditError(f"edit {i + 1}: {exc}") from None
    for path, text in staged.items():
        (root / path).write_text(text)
    return sorted(staged)
