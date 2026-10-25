import pytest

from remediation.edits import EditError, apply_edits, check_edits


@pytest.fixture
def root(tmp_path):
    (tmp_path / "m.sql").write_text("select a, b from t\n")
    return tmp_path


def test_apply_in_order(root):
    edits = [{"path": "m.sql", "old": "a, b", "new": "a, c"}, {"path": "m.sql", "old": "a, c", "new": "x"}]
    assert check_edits(root, edits) == []
    assert apply_edits(root, edits) == ["m.sql"]
    assert (root / "m.sql").read_text() == "select x from t\n"


@pytest.mark.parametrize("edit, problem", [
    ({"path": "m.sql", "old": "zzz", "new": ""}, "found 0"),
    ({"path": "m.sql", "old": "", "new": "x"}, "found 0"),
    ({"path": "missing.sql", "old": "a", "new": "b"}, "not found"),
    ({"path": "../escape.sql", "old": "a", "new": "b"}, "outside the project"),
])
def test_rejections(root, edit, problem):
    assert problem in check_edits(root, [edit])[0]


def test_ambiguous_anchor(root):
    (root / "m.sql").write_text("a a\n")
    assert "found 2" in check_edits(root, [{"path": "m.sql", "old": "a", "new": "b"}])[0]


def test_all_or_nothing(root):
    with pytest.raises(EditError):
        apply_edits(root, [{"path": "m.sql", "old": "a, b", "new": "q"}, {"path": "m.sql", "old": "nope", "new": ""}])
    assert (root / "m.sql").read_text() == "select a, b from t\n"
