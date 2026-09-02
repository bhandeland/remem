from __future__ import annotations

import json

from remem import jsonfile


def test_read_json_returns_empty_for_a_missing_file(tmp_path):
    data, warnings = jsonfile.read_json(tmp_path / "nope.json", set())
    assert data == {}
    assert warnings == []


def test_read_json_backs_up_and_warns_on_invalid_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json")
    backed_up: set = set()

    data, warnings = jsonfile.read_json(path, backed_up)

    assert data == {}
    assert len(warnings) == 1
    assert "backed up" in warnings[0]
    assert list(tmp_path.glob("settings.json.bak*"))


def test_write_json_preserves_unrelated_keys_via_the_caller(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "dark"}))
    backed_up: set = set()

    data, _ = jsonfile.read_json(path, backed_up)
    data["hooks"] = {}
    jsonfile.write_json(path, data, backed_up)

    assert json.loads(path.read_text()) == {"theme": "dark", "hooks": {}}


def test_backup_once_does_not_back_up_the_same_file_twice(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    backed_up: set = set()

    jsonfile.backup_once(path, backed_up)
    jsonfile.backup_once(path, backed_up)

    assert len(list(tmp_path.glob("settings.json.bak*"))) == 1


def test_backup_once_returns_where_the_copy_went(tmp_path):
    # The caller is what tells the user, so the path has to come back out.
    path = tmp_path / "settings.json"
    path.write_text("{}")
    made = jsonfile.backup_once(path, set())
    assert made is not None and made.exists()


def test_backup_once_returns_none_when_there_was_nothing_to_back_up(tmp_path):
    assert jsonfile.backup_once(tmp_path / "missing.json", set()) is None


def test_read_document_never_leaves_a_backup_behind(tmp_path):
    """The reason this exists separately from read_json. A diagnostic that
    litters .bak files beside a user's config is one people stop running,
    and `remem doctor` reads config files it must not touch."""
    path = tmp_path / "settings.json"
    path.write_text("{ not json at all")
    assert jsonfile.read_document(path) == {}
    assert list(tmp_path.iterdir()) == [path]


def test_read_document_tolerates_absence_and_a_non_object(tmp_path):
    assert jsonfile.read_document(tmp_path / "nope.json") == {}
    listy = tmp_path / "listy.json"
    listy.write_text("[1, 2, 3]")
    # A top-level array names no hooks, so {} is the honest answer.
    assert jsonfile.read_document(listy) == {}
