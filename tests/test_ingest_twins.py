"""A file that comes in entirely new while an anchor with the same
filename already has live chunks under another src: path.

The two ordinary causes are a moved file and a document ingested twice
under two identities (an absolute path once, a relative one later). Both
print the same healthy-looking "N new" report without this. A twin is a
question for the user, never an automatic supersede.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from remem.domain import Entry, Kind, Origin
from remem.services import ingest


def _anchor(src: str) -> Entry:
    return Entry(
        id=uuid4(), kind=Kind.DOC, title="Doc", body="", project="proj",
        owner_id=uuid4(), tags=[f"src:{src}"], origin=Origin.INGESTED,
    )


# ---- pure: no marker, runs on CI ----


def test_same_filename_under_another_path_is_a_twin():
    twin = ingest.find_twin(Path("docs/a.md"), [_anchor("notes/docs/a.md")])
    assert twin is not None
    assert twin[0] == "notes/docs/a.md"


def test_the_files_own_path_is_not_its_twin():
    assert ingest.find_twin(Path("docs/a.md"), [_anchor("docs/a.md")]) is None


def test_same_stem_different_extension_is_not_a_twin():
    assert ingest.find_twin(Path("docs/a.md"), [_anchor("docs/a.txt")]) is None


def test_no_anchors_no_twin():
    assert ingest.find_twin(Path("docs/a.md"), []) is None


# ---- db: the check wired into ingest_file ----


@pytest.fixture
def store(conn):
    from remem.backends.postgres.migrate import migrate
    from remem.backends.postgres.store import PostgresStore
    migrate(conn)
    return PostgresStore(conn)


@pytest.fixture
def owner(store):
    return store.ensure_principal("brandon")


@pytest.fixture
def other(store):
    return store.ensure_principal("someone-else")


def _write(root: Path, rel: str) -> None:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# Doc\n\nlead\n\n## One\n\nbody\n")


@pytest.mark.db
def test_a_new_file_with_a_twin_is_reported_with_its_live_count(store, owner, tmp_path):
    _write(tmp_path, "notes/docs/a.md")
    _write(tmp_path, "docs/a.md")
    ingest.ingest_file(store, owner.id, Path("notes/docs/a.md"),
                       project="proj", root=tmp_path)

    report = ingest.ingest_file(store, owner.id, Path("docs/a.md"),
                                project="proj", root=tmp_path)

    assert report.created == 2
    assert report.twins == [("docs/a.md", "notes/docs/a.md", 2)]


@pytest.mark.db
def test_a_re_ingest_of_an_existing_file_reports_no_twin(store, owner, tmp_path):
    _write(tmp_path, "docs/a.md")
    ingest.ingest_file(store, owner.id, Path("docs/a.md"), project="proj", root=tmp_path)

    report = ingest.ingest_file(store, owner.id, Path("docs/a.md"),
                                project="proj", root=tmp_path)

    assert report.unchanged == 2
    assert report.twins == []


@pytest.mark.db
def test_the_twin_check_never_sees_another_owners_chunks(store, owner, other, tmp_path):
    _write(tmp_path, "notes/docs/a.md")
    _write(tmp_path, "docs/a.md")
    ingest.ingest_file(store, other.id, Path("notes/docs/a.md"),
                       project="proj", root=tmp_path)

    report = ingest.ingest_file(store, owner.id, Path("docs/a.md"),
                                project="proj", root=tmp_path)

    assert report.twins == []


@pytest.mark.db
def test_a_dry_run_still_reports_the_twin(store, owner, tmp_path):
    _write(tmp_path, "notes/docs/a.md")
    _write(tmp_path, "docs/a.md")
    ingest.ingest_file(store, owner.id, Path("notes/docs/a.md"),
                       project="proj", root=tmp_path)

    report = ingest.ingest_file(store, owner.id, Path("docs/a.md"),
                                project="proj", root=tmp_path, dry_run=True)

    assert report.twins == [("docs/a.md", "notes/docs/a.md", 2)]
