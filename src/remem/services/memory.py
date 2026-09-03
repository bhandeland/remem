"""Claude Code's file-based memory directory, as a generated view of a
designated collection.

Every policy decision lives here: which collection is exported, what a
conflict is, and which of the six cases a given file falls into. The CLI
resolves a working directory and a project and prints counts.

The directory has a second writer that cannot be told to stop - Claude Code
writes memories there unprompted, mid-session - so the sync adopts before it
regenerates. See docs/superpowers/specs/2026-09-02-claude-memory-design.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from remem import memory_file
from remem.domain import Entry, Kind, Origin
from remem.services import kb
from remem.services.write import remember, supersede, update
from remem.store import Store


class NotDesignated(Exception):
    """This project has no memory collection, so nothing is exported.

    Not an error state to repair - it is the default, and the opt-in gate.
    Raised only by callers that were asked to sync a specific project.
    """


class NoProject(Exception):
    """There is no project to designate against.

    `resolve_project` returns None outside a git repository, and the
    designation is keyed by project - the column is `not null`. Without this
    the store raises a NotNullViolation traceback out of a command a person
    typed, which is the store answering a question the service should have.
    """


class CollectionTooLarge(Exception):
    """The collection resolves at kb.RESOLVE_LIMIT, so membership is a guess.

    Carries the slug and the limit so the frontend prints the number rather
    than hardcoding it a second time.
    """

    def __init__(self, slug: str, limit: int) -> None:
        super().__init__(slug, limit)
        self.slug = slug
        self.limit = limit


def designate(
    store: Store, owner_id: UUID, project: str | None, slug: str | None
) -> None:
    """Point a project's memory export at a collection, or clear it.

    The collection must already exist. Creating one here would give it an
    empty CollectionQuery, and an empty query matches nothing forever - so
    the friendly version of this command would silently guarantee that no
    memory is ever exported. `kb create` says so through kb.advisories();
    this does not get to bypass it.
    """
    if project is None:
        raise NoProject()
    if slug is not None:
        kb.get(store, owner_id, slug)  # raises CollectionNotFound
    store.set_memory_collection(owner_id, project, slug)


def designation(store: Store, owner_id: UUID, project: str) -> str | None:
    return store.memory_collection(owner_id, project)


#: Dot-prefixed so Claude Code does not index it as a memory.
WATERMARK_NAME = ".remem-sync.json"


class Case(StrEnum):
    ADOPT_NEW = "adopt_new"
    HEAL = "heal"
    ADOPT_EDIT = "adopt_edit"
    REGENERATE = "regenerate"
    CONFLICT = "conflict"
    DELETE = "delete"
    UNCHANGED = "unchanged"


@dataclass(slots=True, frozen=True)
class Watermark:
    entry_id: str
    body_sha: str
    #: Never compared. Shown by `remem memory status` so a stale directory is
    #: visible; comparing it would reintroduce the clocks this whole scheme
    #: exists to avoid.
    exported_at: str


def classify(
    *,
    file_sha: str | None,
    entry_sha: str | None,
    mark: Watermark | None,
) -> Case:
    """Which of the six cases this name falls into.

    Pure, and separated from the sync deliberately: this is the part with all
    the combinations, and a pure function means every one of them is tested
    on CI without a database.

    Body comparison alone cannot answer "which side moved" - it says the two
    differ, not who changed. The watermark is what makes it answerable.
    """
    if file_sha is None and entry_sha is None:
        return Case.UNCHANGED  # nothing on either side; nothing to do
    if file_sha is None:
        return Case.REGENERATE  # entry with no file - write it
    if entry_sha is None:
        # No entry. Delete only what we wrote and know to be untouched;
        # anything else is an edit worth keeping.
        if mark is not None and mark.body_sha == file_sha:
            return Case.DELETE
        return Case.ADOPT_EDIT if mark is not None else Case.ADOPT_NEW
    if file_sha == entry_sha:
        # Agreement. Either it never moved, or both sides moved to the same
        # text - which is agreement too, not a conflict worth a user's time.
        if mark is not None and mark.body_sha == file_sha:
            return Case.UNCHANGED
        return Case.HEAL
    if mark is None:
        # They differ and there is no watermark, so nothing says which moved.
        return Case.CONFLICT
    file_moved = mark.body_sha != file_sha
    entry_moved = mark.body_sha != entry_sha
    if file_moved and entry_moved:
        return Case.CONFLICT
    return Case.ADOPT_EDIT if file_moved else Case.REGENERATE


def load_watermarks(directory: Path) -> dict[str, Watermark]:
    """Missing or unreadable is an empty mapping, not an error.

    Degrading here is safe because of how classify() treats a missing mark:
    a file that still matches its entry heals, and one that differs becomes a
    conflict a human resolves. Losing this file costs attention, never data.
    """
    path = directory / WATERMARK_NAME
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Watermark] = {}
    for name, row in raw.items():
        try:
            out[name] = Watermark(
                entry_id=row["entry_id"],
                body_sha=row["body_sha"],
                exported_at=row["exported_at"],
            )
        except (TypeError, KeyError):
            continue
    return out


def save_watermarks(directory: Path, marks: dict[str, Watermark]) -> None:
    path = directory / WATERMARK_NAME
    payload = {
        name: {
            "entry_id": m.entry_id,
            "body_sha": m.body_sha,
            "exported_at": m.exported_at,
        }
        for name, m in sorted(marks.items())
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


#: Identity. One file, one entry, one tag - following handoff's `topic:` and
#: ingest's `src:`/`sec:`. The slug survives a rename of the file itself and
#: is what `[[wiki-links]]` resolve against.
MEM_TAG_PREFIX = "mem:"

#: Written beside a memory when both sides moved, so the store's version is
#: not lost while the file keeps the user's edit. The suffix is excluded from
#: the directory scan: a sidecar is remem's report of a conflict, not a
#: memory, and adopting one would mint a second entry from the same knowledge
#: and then regenerate a file for it on every sync thereafter.
CONFLICT_SUFFIX = ".remem-conflict.md"


@dataclass(slots=True)
class Report:
    adopted: int = 0
    healed: int = 0
    edited: int = 0
    regenerated: int = 0
    deleted: int = 0
    unchanged: int = 0
    conflicts: list[str] = field(default_factory=list)
    #: The subset of `conflicts` that actually has a `.remem-conflict.md` on
    #: disk. Not every reported conflict writes one - --dry-run writes none,
    #: and a file edited for an entry that left the collection has no store
    #: version to write - so a frontend that pointed at the sidecar
    #: unconditionally would name a file that is not there.
    sidecars: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


def _name_of(entry: Entry) -> str | None:
    for tag in entry.tags:
        if tag.startswith(MEM_TAG_PREFIX):
            return tag[len(MEM_TAG_PREFIX):]
    return None


#: Names are filenames, so the alphabet is deliberately narrow: lowercase
#: alphanumerics and hyphens, nothing else. 64 characters is well under every
#: filesystem's limit and long enough that a truncated title is still
#: recognisable in a directory listing.
MAX_NAME = 64

_NOT_NAME_CHARS = re.compile(r"[^a-z0-9]+")


def _slugify(title: str) -> str:
    return _NOT_NAME_CHARS.sub("-", title.lower()).strip("-")[:MAX_NAME].strip("-")


def _mint_name(entry: Entry, taken: set[str]) -> str:
    """A file name for an entry that has never been exported.

    Derived from the title, because that is the only thing about an entry a
    human would recognise in a directory listing. A title that slugifies to
    nothing (punctuation, or a script with no ASCII in it) falls back to the
    entry id, which is stable and unique but tells the reader nothing - the
    ugly name is the signal that the title was unusable.

    Uniqueness is checked against every name already in play this run, files
    on disk included: two entries sharing a file would make each sync adopt
    one over the other, forever.
    """
    base = _slugify(entry.title) or f"entry-{entry.id.hex[:8]}"
    if base not in taken:
        return base
    # Two entries whose titles slugify alike. The discriminator comes from the
    # id rather than a counter so it does not depend on iteration order: the
    # same entry gets the same name whichever of the pair is seen first.
    for width in (8, 16, 32):
        suffix = f"-{entry.id.hex[:width]}"
        candidate = base[:MAX_NAME - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    raise AssertionError(f"no unique name for {entry.id}")  # pragma: no cover


def _adopt_names(
    store: Store,
    owner_id: UUID,
    entries: list[Entry],
    *,
    taken: set[str],
    report: Report,
    dry_run: bool,
) -> dict[str, Entry]:
    """Give every collection entry a `mem:` name, minting one where needed.

    Without this the export is only ever the entries the sync itself adopted
    off disk, and a collection of hand-written rules and notes - the shape the
    spec actually recommends - exports nothing at all. Minting here rather
    than in the case loop keeps the six cases untouched: an entry that leaves
    this function with a name and no file is simply a REGENERATE.

    The tag is written back so identity is stable. A name re-minted on every
    run is a file deleted and rewritten on every run.
    """
    named: dict[str, Entry] = {}
    for entry in entries:
        name = _name_of(entry)
        if name is None:
            name = _mint_name(entry, taken)
            if not dry_run:
                try:
                    entry = update(
                        store, owner_id, entry.id,
                        # update() changes only the fields it is given and
                        # writes the same entry back. supersede() would mint a
                        # replacement and rewrite this entry's history for what
                        # is bookkeeping, not a change of knowledge.
                        tags=[*entry.tags, f"{MEM_TAG_PREFIX}{name}"],
                    )
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    # Nothing is dropped silently: an entry that cannot be
                    # named cannot be exported, and the user is told which.
                    report.failures.append((str(entry.id), f"could not name: {exc}"))
                    continue
        if name in named:
            # Two entries carrying the same `mem:` tag - only reachable by
            # hand-editing tags, since minting checks uniqueness. Keep the
            # first and report the second rather than letting one silently
            # win the file.
            report.failures.append(
                (name, f"entry {entry.id} shares this name with {named[name].id}")
            )
            continue
        taken.add(name)
        named[name] = entry
    return named


def sync(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    directory: Path,
    dry_run: bool = False,
) -> Report:
    """Adopt what Claude wrote, then regenerate the directory from the store.

    Adoption comes first on purpose. The directory has a second writer that
    cannot be told to stop, so regenerating without adopting would destroy
    every memory written since the last run.
    """
    slug = designation(store, owner_id, project)
    if slug is None:
        raise NotDesignated(project)

    report = Report()
    marks = load_watermarks(directory)
    index: dict[str, str] = {}
    index_lines: dict[str, str] = {}
    index_path = directory / "MEMORY.md"
    if index_path.exists():
        text = index_path.read_text()
        index = memory_file.parse_index(text)
        index_lines = memory_file.index_lines(text)

    # --- read both sides ------------------------------------------------
    files: dict[str, memory_file.MemoryFile] = {}
    if directory.exists():
        for path in sorted(directory.glob("*.md")):
            if path.name == "MEMORY.md" or path.name.endswith(CONFLICT_SUFFIX):
                continue
            try:
                files[path.stem] = memory_file.parse(
                    path.read_text(),
                    name=path.stem,
                    title=index.get(path.name, ""),
                )
            except (memory_file.MalformedMemoryFile, OSError) as exc:
                # Collect rather than abort: one bad file must not cost the
                # other thirty-seven.
                report.failures.append((path.stem, str(exc)))

    # Computed here rather than off report.failures later: from this point on
    # failures collect naming problems too, and those are not files that
    # failed to parse.
    unreadable = {name for name, _ in report.failures}

    resolved = kb.resolve(store, owner_id, slug)
    if len(resolved) >= kb.RESOLVE_LIMIT:
        # kb.resolve was written for the context block, where a cap is a
        # display concern. Here an entry past the cap is indistinguishable
        # from an entry that left the collection: the delete gate passes (the
        # file still matches its watermark) and the file goes. So refuse the
        # whole run, the way ingest refuses a file with too many chunks,
        # rather than paginating quietly around a limit that would then
        # decide which memories exist.
        raise CollectionTooLarge(slug, kb.RESOLVE_LIMIT)
    entries = _adopt_names(
        store, owner_id, resolved,
        taken=set(files) | unreadable, report=report, dry_run=dry_run,
    )

    # --- classify and apply ---------------------------------------------
    now = datetime.now(timezone.utc).isoformat()
    # What MEMORY.md will list, kept in step as the loop runs rather than
    # re-resolved at the end: an entry adopted or superseded during this run
    # is already in hand here, and a second resolve would have to be capped,
    # ordered and de-tagged all over again to say the same thing.
    live: dict[str, Entry] = dict(entries)
    for name in sorted(set(files) | set(entries)):
        if name in unreadable:
            # Unreadable is not absent. A file we could not parse has no
            # file_sha, and classify would read that as "no file" and
            # regenerate this one from the entry - overwriting whatever the
            # user has on disk. An unparseable file is the strongest possible
            # signal that something happened to it which remem did not do, so
            # it is reported and otherwise left completely alone: no write, no
            # watermark, no counter.
            continue
        mf = files.get(name)
        entry = entries.get(name)
        case = classify(
            file_sha=None if mf is None else memory_file.body_sha(mf.body),
            entry_sha=None if entry is None else memory_file.body_sha(entry.body),
            mark=marks.get(name),
        )

        if case is Case.CONFLICT:
            report.conflicts.append(name)
            if not dry_run and entry is not None:
                (directory / f"{name}{CONFLICT_SUFFIX}").write_text(
                    memory_file.render(_as_file(entry, name))
                )
                # Recorded, not assumed: --dry-run writes no sidecar, and
                # neither does the orphan branch below, so the frontend has to
                # be told which conflicts actually have a file to point at.
                report.sidecars.append(name)
            continue

        if case is Case.UNCHANGED:
            report.unchanged += 1
            continue

        if case is Case.ADOPT_NEW:
            report.adopted += 1
            if not dry_run:
                new = remember(
                    store, owner_id,
                    title=mf.title or memory_file.title_from_name(name),
                    body=mf.body,
                    summary=mf.description,
                    kind=Kind.NOTE,
                    project=project,
                    tags=_tags_for(mf, name),
                    origin=Origin.AGENT,
                )
                live[name] = new
                # Deliberately not pinned. kb.resolve returns pinned members
                # regardless of the collection's query, so pinning here would
                # make the query decorative and an entry could never leave the
                # collection - which is the one thing that deletes a file.
                # Pins stay the user's escape hatch, via `remem kb pin`.
                marks[name] = Watermark(
                    entry_id=str(new.id),
                    body_sha=memory_file.body_sha(mf.body),
                    exported_at=now,
                )
            continue

        if case is Case.ADOPT_EDIT:
            if entry is None:
                # The file was edited for an entry that has left the
                # collection. Re-adopting would duplicate the entry that
                # still exists outside it, and deleting would destroy the
                # edit, so this is a conflict: report it, touch nothing.
                report.conflicts.append(name)
                continue
            report.edited += 1
            if not dry_run:
                new = supersede(
                    store, owner_id, entry.id,
                    title=mf.title or entry.title,
                    body=mf.body,
                    summary=mf.description,
                )
                live[name] = new
                marks[name] = Watermark(
                    entry_id=str(new.id),
                    body_sha=memory_file.body_sha(mf.body),
                    exported_at=now,
                )
            continue

        if case is Case.HEAL:
            report.healed += 1
            if not dry_run:
                marks[name] = Watermark(
                    entry_id=str(entry.id),
                    body_sha=memory_file.body_sha(entry.body),
                    exported_at=now,
                )
            continue

        if case is Case.REGENERATE:
            report.regenerated += 1
            if not dry_run:
                directory.mkdir(parents=True, exist_ok=True)
                # An Entry has nowhere to store the metadata keys remem does
                # not own, so they are read back off the file being replaced.
                # A file remem creates from scratch simply has none.
                (directory / f"{name}.md").write_text(
                    memory_file.render(_as_file(entry, name, source=mf))
                )
                marks[name] = Watermark(
                    entry_id=str(entry.id),
                    body_sha=memory_file.body_sha(entry.body),
                    exported_at=now,
                )
            continue

        if case is Case.DELETE:
            report.deleted += 1
            if not dry_run:
                (directory / f"{name}.md").unlink(missing_ok=True)
                marks.pop(name, None)
            live.pop(name, None)
            continue

        # A Case added later and not handled above would otherwise fall out of
        # this loop counted as nothing at all - the silent-vanishing failure
        # CLAUDE.md already documents for search.DEFAULT_ORIGINS.
        raise AssertionError(case)

    if not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            memory_file.render_index(
                [_as_file(e, n) for n, e in live.items()],
                # A file remem could not parse keeps its MEMORY.md line. The
                # index is rebuilt from live entries, and that file has none -
                # so without this, "reported and otherwise left completely
                # alone" would still cost the file its index line, and Claude
                # Code would stop loading a file remem deliberately did not
                # touch.
                carried={
                    f"{n}.md": line
                    for n, line in (
                        (n, index_lines.get(f"{n}.md")) for n, _ in report.failures
                    )
                    if line is not None and n not in live
                },
            )
        )
        save_watermarks(directory, marks)

    return report


def _tags_for(mf: memory_file.MemoryFile, name: str) -> list[str]:
    tags = [f"{MEM_TAG_PREFIX}{name}"]
    if mf.type:
        tags.append(f"type:{mf.type}")
    return tags


@dataclass(slots=True)
class Status:
    project: str
    collection: str | None
    directory: Path | None
    entries: int = 0
    files: int = 0
    #: Files whose checksum no longer matches their watermark - what the next
    #: sync would adopt or flag.
    stale: int = 0
    #: Entries in both the memory collection and the project's knowledge
    #: base. Not an error: overlapping deliberately is a legitimate choice.
    #: Reported because MEMORY.md and the SessionStart block are both loaded
    #: every session, and RulesExceedBudget fails silently on every harness,
    #: so a number you can see is the cheapest guard available.
    overlap: int = 0
    overlap_bytes: int = 0
    #: `<name>.remem-conflict.md` sidecars still on disk from a past sync.
    #: Nothing ever deletes one - deliberately, because auto-deleting a
    #: sidecar risks destroying the copy the user needs - so this is the
    #: only place an unresolved conflict stays visible between syncs.
    conflicts: int = 0


def status(
    store: Store,
    owner_id: UUID,
    *,
    project: str,
    directory: Path | None,
    kb_slug: str | None = None,
) -> Status:
    slug = designation(store, owner_id, project)
    out = Status(project=project, collection=slug, directory=directory)
    if slug is None:
        return out

    entries = kb.resolve(store, owner_id, slug)
    out.entries = len(entries)

    if directory is not None and directory.exists():
        marks = load_watermarks(directory)
        for path in directory.glob("*.md"):
            if path.name == "MEMORY.md":
                continue
            if path.name.endswith(CONFLICT_SUFFIX):
                out.conflicts += 1
                continue
            out.files += 1
            mark = marks.get(path.stem)
            try:
                mf = memory_file.parse(
                    path.read_text(), name=path.stem, title="",
                )
            except (memory_file.MalformedMemoryFile, OSError):
                out.stale += 1
                continue
            if mark is None or mark.body_sha != memory_file.body_sha(mf.body):
                out.stale += 1

    if kb_slug:
        try:
            kb_ids = {e.id for e in kb.resolve(store, owner_id, kb_slug)}
        except kb.CollectionNotFound:
            return out
        shared = [e for e in entries if e.id in kb_ids]
        out.overlap = len(shared)
        out.overlap_bytes = sum(len(e.body.encode("utf-8")) for e in shared)
    return out


def _as_file(
    entry: Entry, name: str, source: memory_file.MemoryFile | None = None
) -> memory_file.MemoryFile:
    """The file remem would write for this entry.

    Identity here is `name`, the filename stem, and the index links to
    `<name>.md`. The frontmatter `name:` is a different thing: it is the
    user's, and a file that already carries one keeps it, because rewriting
    it would edit a field remem does not own on every regenerate. An `Entry`
    also has nowhere to store the metadata keys remem does not own, so those
    are read back off the file being replaced; a file remem creates from
    scratch simply has none.
    """
    type_ = None
    for tag in entry.tags:
        if tag.startswith("type:"):
            type_ = tag[len("type:"):]
    return memory_file.MemoryFile(
        name=name if source is None else source.name,
        title=entry.title,
        description=entry.summary or "",
        type=type_,
        body=entry.body,
        extra=dict({} if source is None else source.extra),
    )
