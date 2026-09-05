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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from remem import memory_file
from remem.domain import Entry, Kind, MemoryDesignation, Origin
from remem.services import kb
from remem.services.write import RuleNeedsSummary, remember, supersede, update
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
    store: Store, owner_id: UUID, project: str | None, slug: str | None,
    working_dir: str | None = None,
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
    store.set_memory_collection(owner_id, project, slug, working_dir)


def designation(store: Store, owner_id: UUID, project: str) -> str | None:
    return store.memory_collection(owner_id, project)


def designations(store: Store, owner_id: UUID) -> list[MemoryDesignation]:
    """Every project this owner has designated, with where it was made from.

    The list `sync --all` walks. It is the store's answer verbatim: which
    of these can actually be synced is a policy question, and it is decided
    in `sync_all` rather than here or in a frontend.
    """
    return store.memory_designations(owner_id)


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
#: ingest's `src:`/`sec:`. The name is the **filename stem**, not the
#: frontmatter `name:`, which is the user's to edit and which two files are
#: free to carry the same value of.
#:
#: A rename therefore moves identity, and on its own that made `classify` see
#: two names rather than one movement: a new entry minted for the new stem and
#: the deleted file regenerated from the old. `_follow_renames` is what closes
#: that, before the cases run - not by finding an identity that survives a
#: rename, but by proving one after the fact from the watermark, which records
#: the exact body remem last wrote under the old name.
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
    #: `(old name, new name)` per file renamed on disk and followed by
    #: re-tagging its entry. Counted separately from `adopted` and
    #: `regenerated` because it is neither: nothing entered the store and
    #: nothing was written back out, one name moved.
    renamed: list[tuple[str, str]] = field(default_factory=list)


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
    # Reserve every name an entry already holds BEFORE minting anything.
    # Seeding `taken` from the files on disk is not enough: an entry tagged
    # `mem:foo` whose file is absent - deleted, a fresh machine, a directory
    # never synced - has reserved nothing yet, so an untagged entry whose
    # title slugifies to `foo` would mint it first and the tagged entry would
    # be refused on every subsequent run, permanently, until someone
    # hand-edited a tag.
    taken |= {name for entry in entries if (name := _name_of(entry)) is not None}

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


def _follow_renames(
    store: Store,
    owner_id: UUID,
    *,
    entries: dict[str, Entry],
    files: dict[str, memory_file.MemoryFile],
    marks: dict[str, Watermark],
    unreadable: set[str],
    report: Report,
    dry_run: bool,
) -> None:
    """Move a name that was renamed on disk, before the six cases run.

    Identity is the filename stem, so a rename is two names to `classify`,
    not one movement: the new stem is an ADOPT_NEW and the old is a
    REGENERATE. That mints a second entry holding the same knowledge and
    writes the file the user deleted straight back out. Both gates behave
    correctly throughout - nothing is deleted, nothing is overwritten - which
    is exactly why the duplication survived so long unnoticed.

    A rename is provable rather than guessed. The watermark records the body
    remem itself last wrote under the old name, so a new file whose body
    hashes to that same value *is* the old file, moved. Nothing looser is
    allowed here: matching on titles or on near-identical bodies would start
    re-tagging entries on a coincidence, which is a worse failure than the
    duplicate it set out to fix.

    Running as a pre-pass keeps `classify` untouched - six cases, pure, and
    tested without a database on CI. By the time the loop runs, a followed
    rename is an ordinary UNCHANGED under the new name.

    The floor, stated rather than papered over: a rename *and* an edit in the
    same interval is not detectable this way, because the proof is byte
    equality with the watermark. That still duplicates, and it is much rarer
    than a plain rename.
    """
    # An unreadable file is present but unparseable, so its name is absent
    # from `files` while its file is emphatically not gone. Reading it as the
    # source of a rename would re-tag an entry away from a file that is
    # sitting right there.
    missing = sorted(
        name for name in entries
        if name not in files and name in marks and name not in unreadable
    )
    arrivals = [
        name for name in files if name not in entries and name not in marks
    ]
    if not missing or not arrivals:
        return

    by_sha: dict[str, list[str]] = {}
    for name in arrivals:
        by_sha.setdefault(memory_file.body_sha(files[name].body), []).append(name)

    # Both directions of ambiguity are resolved before anything is written,
    # because acting on the unambiguous pairs first would let the order names
    # happen to sort in decide which of an ambiguous pair got claimed.
    claims = {
        old: sorted(by_sha[marks[old].body_sha])
        for old in missing
        if marks[old].body_sha in by_sha
    }
    claimed_by: dict[str, list[str]] = {}
    for old, candidates in claims.items():
        for new in candidates:
            claimed_by.setdefault(new, []).append(old)

    for old, candidates in claims.items():
        if len(candidates) > 1:
            # Two files with the same body, one missing name. Picking either
            # makes the entry follow a coin flip.
            report.failures.append((
                old,
                f"looks renamed but {' and '.join(candidates)} are identical "
                f"copies of it - rename cannot be followed, so nothing was "
                f"re-tagged; delete one or re-sync once they differ",
            ))
            continue
        new = candidates[0]
        rivals = claimed_by[new]
        if len(rivals) > 1:
            # The mirror image: two entries whose watermarks hold the same
            # body, one new file. Same refusal, reported against each.
            report.failures.append((
                old,
                f"looks renamed to {new}, but {' and '.join(sorted(rivals))} "
                f"both match it - rename cannot be followed, so nothing was "
                f"re-tagged",
            ))
            continue

        entry = entries[old]
        if not dry_run:
            try:
                entry = update(
                    store, owner_id, entry.id,
                    # update(), not supersede(), for the reason _adopt_names
                    # gives: the knowledge did not change, only the name it
                    # is filed under, and a replacement entry would rewrite
                    # this entry's history for bookkeeping.
                    tags=[
                        *(t for t in entry.tags if t != f"{MEM_TAG_PREFIX}{old}"),
                        f"{MEM_TAG_PREFIX}{new}",
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                report.failures.append(
                    (old, f"could not follow rename to {new}: {exc}")
                )
                continue
        # Moved in memory even under --dry-run, so the preview the user reads
        # is the run they would get: leaving the old name in place would
        # report the rename and an ADOPT_NEW and a REGENERATE for the same
        # one file. Only the store write and the watermark file are gated.
        entries[new] = entry
        del entries[old]
        marks[new] = marks.pop(old)
        report.renamed.append((old, new))


@dataclass(slots=True)
class AllOutcome:
    """What `sync_all` did about one designation.

    Exactly one of `report` and `skipped` is set. A skip is not a failure of
    the sync - it is remem declining to guess which directory a project
    means, which is the only honest answer when the designation predates the
    recorded working directory or that directory has since gone.
    """

    project: str
    collection: str
    directory: Path | None = None
    report: Report | None = None
    skipped: str | None = None


def sync_all(
    store: Store,
    owner_id: UUID,
    *,
    resolve_directory: Callable[[Path], Path | None],
    dry_run: bool = False,
) -> list[AllOutcome]:
    """Sync every designated project, one outcome each.

    `resolve_directory` is the adapter capability that turns a working
    directory into a memory directory - passed in rather than imported,
    the same way `sync` takes the directory itself, because which harness
    owns a memory directory is not something this service decides.

    Collects rather than aborts, for the same reason the file loop inside
    `sync` does: one project whose collection was deleted must not cost the
    other seven their sync. Every skip carries the reason, because a silent
    short list is indistinguishable from having nothing to do.
    """
    out: list[AllOutcome] = []
    for d in designations(store, owner_id):
        if d.working_dir is None:
            out.append(AllOutcome(
                d.project, d.collection,
                skipped=(
                    "no working directory recorded - re-run `remem memory "
                    "designate` from the project's directory"
                ),
            ))
            continue
        cwd = Path(d.working_dir)
        if not cwd.is_dir():
            out.append(AllOutcome(
                d.project, d.collection,
                skipped=f"{cwd} no longer exists",
            ))
            continue
        directory = resolve_directory(cwd)
        if directory is None:
            out.append(AllOutcome(
                d.project, d.collection,
                skipped=f"no memory directory for {cwd}",
            ))
            continue
        try:
            report = sync(
                store, owner_id, project=d.project, directory=directory,
                dry_run=dry_run,
            )
        except (NotDesignated, CollectionTooLarge, kb.CollectionNotFound,
                OSError) as exc:
            out.append(AllOutcome(
                d.project, d.collection, directory=directory,
                skipped=f"{type(exc).__name__}: {exc}",
            ))
            continue
        out.append(AllOutcome(
            d.project, d.collection, directory=directory, report=report,
        ))
    return out


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
    # Before the cases, not inside them: a rename is one movement that
    # `classify` can only see as two independent names.
    _follow_renames(
        store, owner_id, entries=entries, files=files, marks=marks,
        unreadable=unreadable, report=report, dry_run=dry_run,
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
            if dry_run:
                report.edited += 1
                continue
            try:
                new = supersede(
                    store, owner_id, entry.id,
                    title=mf.title or entry.title,
                    body=mf.body,
                    # `mf.description` is "" for a missing or blank
                    # `description:` line (memory_file.parse's contract),
                    # not None - and supersede's "carry the old summary"
                    # default is keyed on None. Passing "" straight through
                    # would overwrite a rule's real summary with nothing,
                    # or - for a rule edited with its description line
                    # removed - raise RuleNeedsSummary mid-sync for no
                    # reason, since the old summary was right there to
                    # carry.
                    summary=mf.description or None,
                )
            except RuleNeedsSummary:
                # A rule that predates the summary requirement has nothing
                # to carry - `or None` above still resolves to None, and
                # this is the one case it cannot paper over: memory.sync
                # has no --summary flag to ask the user with. Reported like
                # a malformed file rather than raising (not counted as
                # `edited` - nothing was): an unattended sync (this runs
                # from a SessionStart hook) must not crash for every other
                # entry in the same run, and no watermark is written for
                # this name, so the next sync retries the same supersede
                # rather than silently dropping the edit. `remem update
                # --summary` on the entry directly resolves it for good.
                report.failures.append((
                    name,
                    "needs a summary before this edit can sync - "
                    "run `remem update <id> --summary '...'` on the "
                    "entry, then sync again",
                ))
                continue
            report.edited += 1
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
    if type_ is None and source is not None:
        # Same footing as `extra`, and for the same reason. The tag is
        # minted at adoption from what parse() could see, so an entry
        # adopted while parse() still dropped the flat dialect's top-level
        # `type:` has none - and rebuilding from the entry alone would then
        # strip a type the user never touched. The entry wins when it has
        # something to say; the file answers when it does not.
        type_ = source.type
    return memory_file.MemoryFile(
        name=name if source is None else source.name,
        title=entry.title,
        description=entry.summary or "",
        type=type_,
        body=entry.body,
        extra=dict({} if source is None else source.extra),
    )
