"""Settings resolution: environment variable, then config file, then default."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from platformdirs import user_config_path

DEFAULT_DSN = "postgresql://remem:remem@localhost:5433/remem"
DEFAULT_HANDLE = "brandon"
DEFAULT_MAX_CHARS = 6000
DEFAULT_FUZZY_THRESHOLD = 0.3

# Pinned, not inherited. `claude -p` with no --model uses whatever the user's
# default is, so extraction cost and behaviour would drift whenever they
# switch models for unrelated reasons.
#
# Sonnet rather than Haiku, measured on a real 40KB transcript: Haiku returned
# 1 of 3 usable entries - a platitude, and an open question recorded as a
# durable rule - while Sonnet and Opus each returned 2 of 2. Haiku's JSON was
# valid every time; what it got wrong was judgment. Deciding what will still be
# true in a month is not a compression task.
DEFAULT_EXTRACT_MODEL = "sonnet"

# A session past this many turns is expensive to keep going and cheap to hand
# off. 150/50 matches the shell hook this replaced.
DEFAULT_TURN_WARN_AT = 150
DEFAULT_TURN_WARN_EVERY = 50

# How long a session must be quiet before extraction reads it. The trigger
# is idleness rather than a session-end hook because two of the three
# harnesses remem targets do not have one - and because a hook that never
# fires strands the session forever, while a clock always ticks.
DEFAULT_IDLE_MINUTES = 20

DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# Cosine similarity, so 1.0 is identical and 0 is unrelated. 0.55 is the
# floor at which bge-small stops returning things a reader would call
# related. Too low and the semantic tier answers every query with its four
# nearest entries, which is worse than answering nothing: an empty result
# says "not stored", while four irrelevant ones say "stored, and this is
# what we have".
DEFAULT_SEMANTIC_THRESHOLD = 0.55


@dataclass(frozen=True, slots=True)
class Config:
    dsn: str
    user_handle: str
    max_chars: int
    fuzzy_threshold: float
    extract_model: str
    turn_warn_at: int
    turn_warn_every: int
    idle_minutes: int
    embed_model: str
    semantic_threshold: float


def default_config_path() -> Path:
    return user_config_path("remem") / "config.toml"


def _read_file(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError, tomllib.TOMLDecodeError:
        # A broken or missing config file must never stop remem from running.
        return {}


def load(
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Config:
    env = os.environ if env is None else env
    path = (
        config_path
        if config_path is not None
        else Path(env.get("REMEM_CONFIG", default_config_path()))
    )
    data = _read_file(path)

    def pick(env_key: str, file_key: str, default):
        if env_key in env:
            return env[env_key]
        return data.get(file_key, default)

    max_chars = pick("REMEM_MAX_CHARS", "max_chars", DEFAULT_MAX_CHARS)
    try:
        max_chars = int(max_chars)
    except TypeError, ValueError:
        max_chars = DEFAULT_MAX_CHARS

    threshold = pick(
        "REMEM_FUZZY_THRESHOLD", "fuzzy_threshold", DEFAULT_FUZZY_THRESHOLD
    )
    try:
        threshold = float(threshold)
    except TypeError, ValueError:
        threshold = DEFAULT_FUZZY_THRESHOLD
    if not 0.0 < threshold <= 1.0:
        # Outside this range the setting is meaningless: 0 matches everything,
        # above 1 matches nothing. Fall back rather than silently disable search.
        threshold = DEFAULT_FUZZY_THRESHOLD

    # REMEM_CAPTURE_MODEL is read for one release, and warns. The two silent
    # options are both wrong: reading it quietly leaves a user believing
    # they pinned a model when the name they set no longer exists, and
    # ignoring it quietly switches their model without telling them.
    # stderr, not stdout - a hook's stdout is the context block.
    if "REMEM_EXTRACT_MODEL" in env:
        extract_model = str(env["REMEM_EXTRACT_MODEL"])
    elif "REMEM_CAPTURE_MODEL" in env:
        print(
            "REMEM_CAPTURE_MODEL is renamed to REMEM_EXTRACT_MODEL; "
            "set REMEM_EXTRACT_MODEL instead.",
            file=sys.stderr,
        )
        extract_model = str(env["REMEM_CAPTURE_MODEL"])
    elif "extract_model" in data:
        extract_model = str(data["extract_model"])
    elif "capture_model" in data:
        print(
            "capture_model in config.toml is renamed to extract_model; "
            "set extract_model instead.",
            file=sys.stderr,
        )
        extract_model = str(data["capture_model"])
    else:
        extract_model = DEFAULT_EXTRACT_MODEL
    extract_model = extract_model.strip()
    if not extract_model:
        # An empty value would become `--model ''`, which claude rejects.
        extract_model = DEFAULT_EXTRACT_MODEL

    def positive_int(env_key: str, file_key: str, default: int) -> int:
        value = pick(env_key, file_key, default)
        try:
            value = int(value)
        except TypeError, ValueError:
            return default
        # A non-positive threshold would warn on every prompt forever, which
        # is how a warning gets ignored.
        return value if value > 0 else default

    turn_warn_at = positive_int(
        "REMEM_TURN_WARN_AT", "turn_warn_at", DEFAULT_TURN_WARN_AT
    )
    turn_warn_every = positive_int(
        "REMEM_TURN_WARN_EVERY", "turn_warn_every", DEFAULT_TURN_WARN_EVERY
    )

    # positive_int, not int: a zero window makes every session extractable
    # the instant its first event lands, which is extraction racing a
    # session that is still being worked in.
    idle_minutes = positive_int(
        "REMEM_IDLE_MINUTES", "idle_minutes", DEFAULT_IDLE_MINUTES
    )

    embed_model = str(
        pick("REMEM_EMBED_MODEL", "embed_model", DEFAULT_EMBED_MODEL)
    ).strip()
    if not embed_model:
        embed_model = DEFAULT_EMBED_MODEL

    semantic = pick(
        "REMEM_SEMANTIC_THRESHOLD", "semantic_threshold", DEFAULT_SEMANTIC_THRESHOLD
    )
    try:
        semantic = float(semantic)
    except TypeError, ValueError:
        semantic = DEFAULT_SEMANTIC_THRESHOLD
    if not 0.0 < semantic <= 1.0:
        # Same reasoning as fuzzy_threshold: outside this range the setting is
        # meaningless, and falling back beats silently disabling the tier.
        semantic = DEFAULT_SEMANTIC_THRESHOLD

    return Config(
        extract_model=extract_model,
        fuzzy_threshold=threshold,
        dsn=str(pick("REMEM_DSN", "dsn", DEFAULT_DSN)),
        user_handle=str(pick("REMEM_USER_ID", "user_handle", DEFAULT_HANDLE)),
        max_chars=max_chars,
        turn_warn_at=turn_warn_at,
        turn_warn_every=turn_warn_every,
        idle_minutes=idle_minutes,
        embed_model=embed_model,
        semantic_threshold=semantic,
    )
