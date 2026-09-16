#!/usr/bin/env python
"""Retrieval evaluation for saddlebag's three-tier search.

The tier order - exact full-text, then semantic, then trigram, each running
only when the one above returned nothing - is argued at length in CLAUDE.md
and has never been measured. "Semantic sits above trigram because a query
that matches nothing lexically is far more often a different wording than a
typo" is a plausible claim about the world, not a result. This script is the
difference between the two.

It is deliberately NOT part of `make check`. The Makefile scopes ruff and
pytest to `src` and `tests`, and pyrefly's project-includes says the same,
so a file here is outside all three. That is the point: this needs a
populated database and an LLM, and the verification gate must never need
either.

It never writes an entry. Sampling and scoring are reads; the only thing it
writes is JSON files in the working directory.

    ./scripts-eval-retrieval.py generate --n 150
    $EDITOR eval-questions-control.json      # write ~15 by hand
    ./scripts-eval-retrieval.py run --json eval-results.json

Generation shells out to `claude -p` through the same `build_command` /
`build_env` the extractor uses. `build_env` is not optional politeness: it
sets the recursion guard, and without it ~20 spawned Claude sessions record
their own events into the very store being measured, which the next
extraction then turns into entries. The eval would corrupt its own corpus.
"""

# ruff: noqa: T201
# T201 bans `print` across this project because the MCP server speaks
# JSON-RPC on stdout, where one stray print corrupts the stream for a whole
# session. That reasoning does not reach this file: it is a standalone CLI
# tool, never imported by the served package, and printing a report to
# stdout is its entire job. Exempted here by name and in one place, so the
# carve-out is greppable rather than achieved by quietly never running ruff
# on the file.

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from saddlebag.config import DEFAULT_EXTRACT_MODEL, load
from saddlebag.domain import Entry, Query
from saddlebag.extract.claude_cli import build_command, build_env
from saddlebag.services import search as search_service
from saddlebag.session import open_session

QUESTIONS_PATH = Path("eval-questions.json")
CONTROL_PATH = Path("eval-questions-control.json")

#: The stub `generate` writes when no control file exists, as data rather
#: than inlined at the write site. Two reasons. It is now the ONLY
#: description of the hand-written control set's shape - the committed
#: file holds real questions, not a template - so guidance that lives only
#: in a printed message is guidance that scrolls away while the file it
#: describes persists. And every row here must be skipped by `_load` (the
#: `_comment` row has no gold_id; the examples' ids start with "<"), which
#: is a property worth being able to load and check rather than assert in
#: a comment.
CONTROL_STUB: list[dict[str, str]] = [
    {
        "_comment": (
            "Replace these rows with ~15 of your own, and delete any you do "
            "not fill in. gold_id must be a real entry id: find one with "
            "`bag search <term> --json` and copy its id field. Write roughly "
            "half keyword and half paraphrase - the per-style tier table is "
            "only meaningful when both regimes are represented, and a "
            "control set written entirely in one style cannot check the "
            "generated set's other half."
        ),
        "question": "",
        "gold_id": "",
        "origin": "",
        "source_title": "",
        "style": "paraphrase",
    },
    {
        "question": "<keyword style: two to five words you would really type>",
        "gold_id": "<paste a real entry id here>",
        "origin": "human",
        "source_title": "<optional, for your own reference>",
        "style": "keyword",
    },
    {
        "question": (
            "<paraphrase style: one sentence about the problem, avoiding "
            "the entry's own wording>"
        ),
        "gold_id": "<paste a real entry id here>",
        "origin": "human",
        "source_title": "<optional, for your own reference>",
        "style": "paraphrase",
    },
]

#: Origins a default search can actually return. Sampling a gold entry from
#: ARCHIVED or HANDOFF would manufacture a guaranteed miss: `find` filters
#: them out unless asked, so no retriever could ever score on them and the
#: headline number would drop for a reason that has nothing to do with
#: retrieval quality.
POOL_ORIGINS = list(search_service.DEFAULT_ORIGINS)

#: How deep to look when scoring. Hit@1 and Hit@5 are the headline; MRR is
#: computed over this window, so a gold entry at rank 9 still contributes
#: something rather than reading identically to a total miss.
K = 10

#: How much of an entry the generator sees. Enough to ask a real question
#: about, short enough that a batch of eight fits comfortably in one prompt.
MAX_BODY_CHARS = 1200

#: Length bands per style - the caps on what the generator is allowed to
#: hand back. Model output is untrusted here exactly as it is in
#: `extract/base.py`: shape-checked, capped, and anything that fails is
#: reported rather than dropped in silence. There are two bands because the
#: styles are different shapes and one cannot hold both - a paraphrased
#: question is a sentence, while a keyword query is two to five words, and
#: the sentence band's 12-character floor would throw away half of those as
#: though they had come back empty.
BOUNDS = {
    "paraphrase": (12, 300),
    "keyword": (4, 60),
}

#: Two styles, because one of them cannot measure the thing this script
#: exists to measure. A paraphrase-only set was generated first and every
#: question in it fell through to the semantic tier - mean title-token
#: overlap 18%, exact tier 0 for 8 - which makes cascade, semantic and
#: blended identical by construction rather than by result. Real queries
#: arrive in both regimes: sometimes you remember the error string, and
#: sometimes only the shape of the problem. Measuring the tier order needs
#: both.
PARAPHRASE_PROMPT = """\
You are building an evaluation set for a knowledge-base search engine.

For EACH entry below, write ONE question that a developer might type into \
search weeks later, when they remember the problem but not the wording.

Hard rules:
- Do NOT reuse distinctive words from the entry's title or body. If the \
entry says "pgvector cosine distance", ask about "finding similar notes by \
meaning", not "pgvector cosine". Reusing the wording tests string overlap, \
not retrieval.
- Ask about the PROBLEM or SITUATION, not the solution's vocabulary.
- One sentence. No preamble. Never mention that this is a test.
- The question must be answerable ONLY by that entry - not generic.

Return ONLY a JSON array, no prose, no code fence:
[{"id": "<the entry id exactly as given>", "question": "<your question>"}]
"""

KEYWORD_PROMPT = """\
You are building an evaluation set for a knowledge-base search engine.

For EACH entry below, write ONE short KEYWORD QUERY - the two to five words \
a developer would actually type into search when they half-remember this \
entry and want it back.

Hard rules:
- Two to five words. Not a sentence, no question mark, no preamble.
- Use the words someone would REMEMBER: an error fragment, a function or \
flag name, a distinctive pair of nouns. Unlike the other style, reusing the \
entry's own vocabulary is EXPECTED here - that is what makes it a keyword \
query.
- Still specific: "database error" could match anything, "btrim trailing \
newline" could not. Prefer the distinctive term over the common one.
- Never mention that this is a test.

Return ONLY a JSON array, no prose, no code fence:
[{"id": "<the entry id exactly as given>", "question": "<your query>"}]
"""

PROMPTS = {"paraphrase": PARAPHRASE_PROMPT, "keyword": KEYWORD_PROMPT}
STYLES = list(PROMPTS)


@dataclass
class Question:
    question: str
    gold_id: str
    origin: str
    source_title: str
    #: "generated" or "control". Kept per question rather than per file so a
    #: merged run can still report the two populations separately, which is
    #: the only thing that can tell a good retriever from an easy question set.
    kind: str = "generated"
    #: "paraphrase" or "keyword". Orthogonal to `kind`: a hand-written
    #: control question has a style too, and the tier a question reaches is
    #: predicted by its style, not by who wrote it.
    style: str = "paraphrase"

    @staticmethod
    def from_dict(d: dict[str, Any], kind: str) -> Question:
        return Question(
            question=str(d["question"]),
            gold_id=str(d["gold_id"]),
            origin=str(d.get("origin", "?")),
            source_title=str(d.get("source_title", "")),
            kind=kind,
            style=str(d.get("style", "paraphrase")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "gold_id": self.gold_id,
            "origin": self.origin,
            "source_title": self.source_title,
            "style": self.style,
        }


#: Prefix of the tag that names the file an ingested chunk came from.
#: `ingest` mints `src:<path>` and `sec:<slug>` per chunk; the first is
#: document identity and the second is section identity.
SRC_PREFIX = "src:"


def _doc_key(entry: Entry) -> str:
    """What document an entry belongs to, for document-level scoring.

    The `src:` tag when there is one, and otherwise the entry's own id. A
    hand-written note is a document of one section, so this makes
    document-level scoring equal section-level scoring for every origin
    that is not chunked - the new number can never flatter them, and the
    two columns are directly comparable across origins.

    Keyed on the tag rather than on the title's `Doc § Section` prefix
    because the tag is what `ingest` actually treats as identity: a title
    containing a literal `§`, or a document retitled between ingests,
    would both break a string split while the tag stays correct.
    """
    for tag in entry.tags:
        if tag.startswith(SRC_PREFIX):
            return tag
    return str(entry.id)


@dataclass
class Scored:
    """One question run against one variant."""

    question: Question
    variant: str
    #: 1-indexed position of the gold entry, or None for a miss inside K.
    rank: int | None
    #: Which tier produced the top hit. Only meaningful for the cascade -
    #: the single-tier variants report themselves, which keeps the field
    #: non-null everywhere and stops a reader inferring a cascade result
    #: from a forced one.
    tier: str | None
    #: 1-indexed position of the first hit from the gold's DOCUMENT, or
    #: None. Last because it is the only field here with a default, and a
    #: defaulted field cannot precede an undefaulted one. Measured because
    #: a section-level number alone reads as an ingest defect when it is a
    #: granularity artifact: on 2026-09-15 ingested chunks scored 43.6%
    #: against the exact section and 69.1% against the right document, the
    #: latter being exactly what hand-written entries score. 28 of 31
    #: misses lost to another ingested chunk, 14 of those to a sibling
    #: section of the same file.
    doc_rank: int | None = None


@dataclass
class VariantReport:
    variant: str
    n: int = 0
    hit1: int = 0
    hit5: int = 0
    #: Document-level hit@1, tracked beside the section-level one rather
    #: than instead of it. Neither subsumes the other: the section number
    #: is what a reader wants when they asked for one specific passage,
    #: and the document number is what says whether search found the
    #: right source of knowledge at all.
    doc_hit1: int = 0
    reciprocal: float = 0.0
    tiers: Counter[str] = field(default_factory=Counter)
    by_origin: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    by_kind: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    by_style: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    #: Document-level hit@1 split by origin - the one place the two
    #: granularities can be compared per origin, which is what shows that
    #: ingested content is only weak at the section grain.
    doc_by_origin: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )
    #: Tier attribution split by style - the single most informative output
    #: here. "Keyword queries are answered by exact, paraphrases by semantic"
    #: is the claim the cascade's ordering rests on, and this is the table
    #: that either shows it or does not.
    tiers_by_style: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )

    def add(self, s: Scored) -> None:
        self.n += 1
        if s.tier:
            self.tiers[s.tier] += 1
            self.tiers_by_style[s.question.style][s.tier] += 1
        hit = 1 if s.rank == 1 else 0
        self.hit1 += hit
        if s.rank is not None and s.rank <= 5:
            self.hit5 += 1
        if s.rank is not None:
            self.reciprocal += 1.0 / s.rank
        self.by_origin[s.question.origin].append(hit)
        self.by_kind[s.question.kind].append(hit)
        self.by_style[s.question.style].append(hit)
        doc_hit = 1 if s.doc_rank == 1 else 0
        self.doc_hit1 += doc_hit
        self.doc_by_origin[s.question.origin].append(doc_hit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "n": self.n,
            "hit@1": self.hit1,
            "hit@5": self.hit5,
            "hit@1_rate": _rate(self.hit1, self.n),
            "hit@5_rate": _rate(self.hit5, self.n),
            "doc_hit@1_rate": _rate(self.doc_hit1, self.n),
            "mrr@%d" % K: round(self.reciprocal / self.n, 4) if self.n else 0.0,
            "answered_by_tier": dict(self.tiers),
            "hit@1_by_origin": {
                o: _rate(sum(v), len(v)) for o, v in sorted(self.by_origin.items())
            },
            "doc_hit@1_by_origin": {
                o: _rate(sum(v), len(v)) for o, v in sorted(self.doc_by_origin.items())
            },
            "hit@1_by_kind": {
                k: _rate(sum(v), len(v)) for k, v in sorted(self.by_kind.items())
            },
            "hit@1_by_style": {
                k: _rate(sum(v), len(v)) for k, v in sorted(self.by_style.items())
            },
            "answered_by_tier_by_style": {
                k: dict(v) for k, v in sorted(self.tiers_by_style.items())
            },
        }


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


# ---------------------------------------------------------------- sampling


def _pool(store: Any, owner_id: UUID) -> list[Entry]:
    """Every entry a default search could return.

    A filters-only Query (no text) is a listing query, and calling the store
    directly rather than `find` is deliberate: `find` clamps limit to
    MAX_LIMIT=200, which is right for a person running `bag search` and
    wrong for enumerating a corpus.
    """
    hits = store.search(Query(text=None, origins=POOL_ORIGINS, limit=100_000), owner_id)
    return [h.entry for h in hits]


def _stratified(entries: list[Entry], n: int, rng: random.Random) -> list[Entry]:
    """An even split across origins, not a proportional one.

    Proportional sampling of this corpus is ~70% ingested document chunks,
    which would drown out the hand-written entries - and whether *those* are
    findable is the question worth answering. Strata that cannot fill their
    share (there are only ~25 extracted entries) give back what they cannot
    use, and the remainder is redistributed to whichever strata still have
    entries left rather than silently shrinking the sample.
    """
    buckets: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        buckets[str(e.origin)].append(e)
    for v in buckets.values():
        rng.shuffle(v)

    chosen: list[Entry] = []
    names = sorted(buckets)
    target = max(1, n // max(1, len(names)))
    for name in names:
        take = min(target, len(buckets[name]))
        chosen.extend(buckets[name][:take])
        buckets[name] = buckets[name][take:]

    # Redistribute whatever the short strata could not supply.
    leftovers = [e for name in names for e in buckets[name]]
    rng.shuffle(leftovers)
    chosen.extend(leftovers[: max(0, n - len(chosen))])
    rng.shuffle(chosen)
    return chosen[:n]


# -------------------------------------------------------------- generation


def _entry_block(e: Entry) -> str:
    body = e.body.strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + " [...]"
    parts = [f"id: {e.id}", f"title: {e.title}"]
    if e.summary:
        parts.append(f"summary: {e.summary}")
    parts.append(f"body: {body}")
    return "\n".join(parts)


def _strip_fence(text: str) -> str:
    """Models wrap JSON in a code fence perhaps a third of the time."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    return m.group(1).strip() if m else t


def _parse_batch(
    raw: str, allowed: dict[str, Entry], style: str
) -> tuple[list[Question], int]:
    """Shape-check untrusted model output. Returns (questions, rejected)."""
    try:
        data = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        return [], len(allowed)
    if not isinstance(data, list):
        return [], len(allowed)

    low, high = BOUNDS[style]
    out: list[Question] = []
    rejected = 0
    for item in data:
        if not isinstance(item, dict):
            rejected += 1
            continue
        eid = str(item.get("id", "")).strip()
        q = str(item.get("question", "")).strip()
        # An id outside this batch means the model invented one; a question
        # outside its style's band is either empty or the wrong shape - a
        # paragraph where a keyword query was asked for, or the reverse.
        if eid not in allowed or not (low <= len(q) <= high):
            rejected += 1
            continue
        e = allowed[eid]
        out.append(
            Question(
                question=q,
                gold_id=eid,
                origin=str(e.origin),
                source_title=e.title,
                style=style,
            )
        )
    return out, rejected


def _generate_batch(
    batch: list[Entry], style: str, model: str, timeout: int
) -> tuple[list[Question], int]:
    allowed = {str(e.id): e for e in batch}
    payload = "\n\n---\n\n".join(_entry_block(e) for e in batch)
    try:
        result = subprocess.run(
            build_command(PROMPTS[style], model),
            input=f"Entries:\n\n{payload}\n",
            capture_output=True,
            text=True,
            timeout=timeout,
            env=build_env(os.environ),
        )
    except FileNotFoundError:
        sys.exit("claude is not on PATH; question generation needs the Claude Code CLI")
    except subprocess.TimeoutExpired:
        print(f"  batch timed out after {timeout}s, skipping", file=sys.stderr)
        return [], len(batch)
    if result.returncode != 0:
        print(f"  claude exited {result.returncode}, skipping batch", file=sys.stderr)
        return [], len(batch)
    return _parse_batch(result.stdout, allowed, style)


def cmd_generate(args: argparse.Namespace) -> int:
    cfg = load()
    rng = random.Random(args.seed)
    with open_session(cfg) as s:
        pool = _pool(s.store, s.owner.id)
    if not pool:
        sys.exit("no entries to sample - is this the right database?")

    sample = _stratified(pool, args.n, rng)
    print(f"pool {len(pool)} entries, sampling {len(sample)}")
    print(
        "  strata: "
        + ", ".join(
            f"{o}={c}"
            for o, c in sorted(Counter(str(e.origin) for e in sample).items())
        )
    )

    # Half the sample in each style. Both halves come off the same shuffled,
    # stratified sample, so neither style is skewed toward one origin.
    #
    # Split rather than paired - one of each style per entry - because a
    # pair shares a gold entry, so an entry that is simply hard to retrieve
    # would depress both styles together and read as a style-independent
    # floor. Independent halves keep the two measurements from sharing that
    # error.
    half = len(sample) // 2
    plan = [("paraphrase", sample[:half]), ("keyword", sample[half:])]

    questions: list[Question] = []
    rejected = 0
    for style, entries in plan:
        batches = [
            entries[i : i + args.batch] for i in range(0, len(entries), args.batch)
        ]
        for i, batch in enumerate(batches, 1):
            print(
                f"  {style} batch {i}/{len(batches)} ({len(batch)} entries)...",
                flush=True,
            )
            got, bad = _generate_batch(batch, style, args.model, args.timeout)
            questions.extend(got)
            rejected += bad

    QUESTIONS_PATH.write_text(
        json.dumps([q.to_dict() for q in questions], indent=2) + "\n"
    )
    styles = dict(sorted(Counter(q.style for q in questions).items()))
    print(f"\nwrote {len(questions)} questions to {QUESTIONS_PATH} {styles}")
    if rejected:
        # Named, never silent: a generation run that quietly dropped a third
        # of its batches produces a small sample that looks like a choice.
        print(f"rejected {rejected} (unparseable, invented id, or bad length)")

    if not CONTROL_PATH.exists():
        # CONTROL_STUB carries the format and the instructions; this prints
        # only why the file matters and where to look. Splitting them that
        # way is deliberate - the two used to say overlapping things, and
        # the half a user still has in front of them an hour later is the
        # file, not the terminal.
        CONTROL_PATH.write_text(json.dumps(CONTROL_STUB, indent=2) + "\n")
        print(
            f"wrote a stub {CONTROL_PATH} - the file itself explains how to "
            "fill it in. Without control questions the run reports generated "
            "ones only, and cannot tell a good retriever from an easy "
            "question set."
        )
    return 0


# ----------------------------------------------------------------- running


def _load(path: Path, kind: str) -> list[Question]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        sys.exit(f"{path} is not valid JSON")
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        gold = str(d.get("gold_id", ""))
        # Skip the stub row the generator writes, so an unedited control
        # file degrades to "no control" rather than to one broken question.
        if gold.startswith("<") or not gold:
            continue
        try:
            out.append(Question.from_dict(d, kind))
        except KeyError:
            continue
    return out


def _rank_of(gold: str, ids: Iterable[str]) -> int | None:
    for i, got in enumerate(ids, 1):
        if got == gold:
            return i
    return None


def _rrf(*rankings: list[str], k: int = 60) -> list[str]:
    """Reciprocal rank fusion - what the 'blended' variant does.

    RRF uses only positions, never scores, which is the honest way to run
    this comparison: `Hit.rank` means ts_rank in the exact tier and cosine
    distance in the semantic one, and CLAUDE.md is right that those are not
    comparable. Fusing positions sidesteps that entirely, so a blended loss
    cannot be blamed on mixing incommensurable numbers.
    """
    score: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for i, eid in enumerate(ranking, 1):
            score[eid] += 1.0 / (k + i)
    return [eid for eid, _ in sorted(score.items(), key=lambda kv: -kv[1])]


def _run_variant(
    variant: str,
    q: Question,
    store: Any,
    owner_id: UUID,
    cfg: Any,
    embedder: Any,
    gold_docs: dict[str, str] | None = None,
) -> Scored:
    base = Query(text=q.question, origins=POOL_ORIGINS, limit=K)
    #: entry id -> document key, filled in from whatever hits each tier
    #: returned. Collected as they go by rather than re-fetched, because
    #: every tier already holds the full Entry; only the GOLD needs a
    #: lookup, and `cmd_run` prefetches those once for the whole run.
    docs: dict[str, str] = {}

    def _ids(hits: list[Any]) -> list[str]:
        out = []
        for h in hits:
            eid = str(h.entry.id)
            docs[eid] = _doc_key(h.entry)
            out.append(eid)
        return out

    def exact_ids() -> list[str]:
        return _ids(store.search(base, owner_id))

    def semantic_ids() -> list[str]:
        if embedder is None:
            return []
        vec = embedder.embed([q.question])[0]
        return _ids(
            store.semantic_search(
                base, owner_id, vec, cfg.embed_model, cfg.semantic_threshold
            )
        )

    if variant == "cascade":
        hits = search_service.find(
            store,
            owner_id,
            Query(text=q.question, limit=K),
            fuzzy_threshold=cfg.fuzzy_threshold,
            embedder=embedder,
            semantic_threshold=cfg.semantic_threshold,
            embed_model=cfg.embed_model,
        )
        ids = _ids(hits)
        tier = str(hits[0].match) if hits else "none"
    elif variant == "exact":
        ids, tier = exact_ids(), "exact"
    elif variant == "semantic":
        ids, tier = semantic_ids(), "semantic"
    elif variant == "fuzzy":
        # The trigram tier, forced. In the cascade it runs only when exact
        # AND semantic both returned nothing, which on this instrument is
        # never - `answered_by_tier` has reported zero fuzzy answers in
        # every run - so its standalone quality was pure assumption until
        # something measured it. That is the whole point of the variant:
        # not to propose reordering the chain, but to know what the
        # bottom of it is actually worth when it does fire.
        ids = _ids(store.fuzzy_search(base, owner_id, cfg.fuzzy_threshold))
        tier = "fuzzy"
    elif variant == "blended":
        ids, tier = _rrf(exact_ids(), semantic_ids())[:K], "blended"
    else:
        raise ValueError(variant)

    # `_rank_of` is plain equality over an iterable, so it scores the
    # document grain unchanged - one ranking function, not two that could
    # drift. A gold whose document is unknown (an id that no longer
    # resolves) scores None, the same silence a missing entry already
    # produces at section level.
    gold_doc = (gold_docs or {}).get(q.gold_id)
    doc_rank = (
        _rank_of(gold_doc, [docs.get(i, "") for i in ids])
        if gold_doc is not None
        else None
    )
    return Scored(
        question=q,
        variant=variant,
        rank=_rank_of(q.gold_id, ids),
        tier=tier,
        doc_rank=doc_rank,
    )


#: Order matters only for presentation: the cascade first as the shipped
#: behaviour, then each tier alone, then the fusion. `--variant` builds its
#: argparse choices from this list, so a new variant reaches the CLI by
#: being added here and nowhere else.
VARIANTS = ["cascade", "exact", "semantic", "fuzzy", "blended"]


def cmd_run(args: argparse.Namespace) -> int:
    questions = _load(QUESTIONS_PATH, "generated") + _load(CONTROL_PATH, "control")
    if not questions:
        sys.exit(f"no questions - run `{sys.argv[0]} generate` first")
    if args.limit:
        questions = questions[: args.limit]

    kinds = Counter(q.kind for q in questions)
    print(f"{len(questions)} questions ({dict(kinds)})")
    if "control" not in kinds:
        print(
            "WARNING: no control questions. Generated scores cannot be "
            f"sanity-checked - fill in {CONTROL_PATH}."
        )

    cfg = load()
    variants = VARIANTS if args.variant == "all" else [args.variant]
    reports: dict[str, VariantReport] = {v: VariantReport(v) for v in variants}

    with open_session(cfg) as s:
        embedder = search_service.shared_embedder(cfg.embed_model)
        if embedder is None:
            print(
                "WARNING: no embedder - semantic and blended variants will "
                "score zero, and the cascade will be measuring two tiers."
            )
        # Gold document identity, resolved once per DISTINCT gold entry.
        # Doing it inside `_run_variant` would cost one lookup per
        # (question, variant) - 660 for 165 answers across four variants -
        # and every variant would be asking the same question of the same
        # rows. A gold id that no longer resolves is simply left out, and
        # its question scores no document rank rather than a wrong one.
        gold_docs: dict[str, str] = {}
        for q in questions:
            if q.gold_id in gold_docs:
                continue
            entry = s.store.get_entry(UUID(q.gold_id), s.owner.id)
            if entry is not None:
                gold_docs[q.gold_id] = _doc_key(entry)

        for n, q in enumerate(questions, 1):
            if n % 25 == 0:
                print(f"  {n}/{len(questions)}...", flush=True)
            for v in variants:
                reports[v].add(
                    _run_variant(v, q, s.store, s.owner.id, cfg, embedder, gold_docs)
                )

    _report(reports, kinds)
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "k": K,
                    "questions": len(questions),
                    "by_kind": dict(kinds),
                    "variants": [r.to_dict() for r in reports.values()],
                },
                indent=2,
            )
            + "\n"
        )
        print(f"\nwrote {args.json}")
    return 0


def _report(reports: dict[str, VariantReport], kinds: Counter[str]) -> None:
    print(
        f"\n{'variant':<10} {'n':>4} {'hit@1':>8} {'hit@5':>8} {'MRR':>7} {'doc@1':>8}"
    )
    print("-" * 51)
    for r in reports.values():
        print(
            f"{r.variant:<10} {r.n:>4} "
            f"{_rate(r.hit1, r.n):>8.1%} {_rate(r.hit5, r.n):>8.1%} "
            f"{(r.reciprocal / r.n if r.n else 0):>7.3f} "
            f"{_rate(r.doc_hit1, r.n):>8.1%}"
        )

    cascade = reports.get("cascade")
    if cascade and cascade.tiers:
        print("\nwhich tier answered (cascade):")
        for tier, count in cascade.tiers.most_common():
            print(f"  {tier:<10} {count:>4}  {_rate(count, cascade.n):>6.1%}")

    if cascade and len(cascade.tiers_by_style) > 1:
        # The table the second style was added for: if keyword queries do not
        # reach the exact tier, nothing in this run can speak to the tier
        # ordering, and the cascade/blended comparison below is vacuous.
        print("\nwhich tier answered, by question style (cascade):")
        for style in sorted(cascade.tiers_by_style):
            counts = cascade.tiers_by_style[style]
            total = sum(counts.values())
            spread = ", ".join(
                f"{t} {_rate(c, total):.0%}" for t, c in counts.most_common()
            )
            print(f"  {style:<11} {spread}")

    if cascade and len(cascade.by_style) > 1:
        print("\nhit@1 by question style (cascade):")
        for style, vals in sorted(cascade.by_style.items()):
            print(f"  {style:<11} {_rate(sum(vals), len(vals)):>6.1%}  (n={len(vals)})")

    if cascade:
        # Both grains, per origin, side by side. A chunked origin's section
        # number in isolation reads as a retrieval defect when it is a
        # granularity artifact; the document column says whether search
        # found the right source at all. For an unchunked origin the two
        # columns are equal by construction - see `_doc_key` - which is
        # what makes them comparable across origins rather than flattering
        # the ones that were never split.
        print("\nhit@1 by origin (cascade), section and document:")
        for origin, vals in sorted(cascade.by_origin.items()):
            dv = cascade.doc_by_origin.get(origin, [])
            doc = _rate(sum(dv), len(dv)) if dv else 0.0
            print(
                f"  {origin:<10} section {_rate(sum(vals), len(vals)):>6.1%}   "
                f"document {doc:>6.1%}  (n={len(vals)})"
            )

    # The comparison the control set exists for.
    if cascade and "control" in kinds and "generated" in kinds:
        gen = cascade.by_kind.get("generated", [])
        ctl = cascade.by_kind.get("control", [])
        g, c = _rate(sum(gen), len(gen)), _rate(sum(ctl), len(ctl))
        print(f"\ngenerated hit@1 {g:.1%} vs control hit@1 {c:.1%}")
        if g > c + 0.15:
            print(
                "  Generated questions score materially higher. The generated "
                "set is probably reusing source vocabulary - treat the "
                "headline number as inflated."
            )

    if "blended" in reports and "cascade" in reports:
        b, c = reports["blended"], reports["cascade"]
        delta = _rate(b.hit1, b.n) - _rate(c.hit1, c.n)
        verdict = (
            "blending wins"
            if delta > 0.02
            else ("cascade holds" if delta < -0.02 else "no material difference")
        )
        print(f"\nblended minus cascade, hit@1: {delta:+.1%} - {verdict}")
        # Split by style, because the overall number can hide the answer: if
        # blending helps keyword queries and hurts paraphrases, a single
        # figure averages the two into "no difference" and the interesting
        # result disappears.
        for style in sorted(set(b.by_style) & set(c.by_style)):
            bs, cs = b.by_style[style], c.by_style[style]
            d = _rate(sum(bs), len(bs)) - _rate(sum(cs), len(cs))
            print(f"  {style:<11} {d:+.1%}  (n={len(cs)})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="build the question set with claude -p")
    g.add_argument("--n", type=int, default=150)
    g.add_argument("--batch", type=int, default=8)
    g.add_argument("--model", default=DEFAULT_EXTRACT_MODEL)
    g.add_argument("--timeout", type=int, default=180)
    g.add_argument("--seed", type=int, default=0)
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("run", help="score the question set")
    r.add_argument("--variant", choices=[*VARIANTS, "all"], default="all")
    r.add_argument("--limit", type=int, default=0, help="smoke-test on the first N")
    r.add_argument("--json", default="")
    r.set_defaults(func=cmd_run)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
