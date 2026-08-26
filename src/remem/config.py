"""Settings resolution: environment variable, then config file, then default."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from platformdirs import user_config_path

DEFAULT_DSN = "postgresql://remem:remem@localhost:5433/remem"
DEFAULT_HANDLE = "brandon"
DEFAULT_MAX_CHARS = 6000
DEFAULT_FUZZY_THRESHOLD = 0.3


@dataclass(frozen=True, slots=True)
class Config:
    dsn: str
    user_handle: str
    max_chars: int
    fuzzy_threshold: float


def default_config_path() -> Path:
    return user_config_path("remem") / "config.toml"


def _read_file(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        # A broken or missing config file must never stop remem from running.
        return {}


def load(
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Config:
    env = os.environ if env is None else env
    path = config_path if config_path is not None else Path(
        env.get("REMEM_CONFIG", default_config_path())
    )
    data = _read_file(path)

    def pick(env_key: str, file_key: str, default):
        if env_key in env:
            return env[env_key]
        return data.get(file_key, default)

    max_chars = pick("REMEM_MAX_CHARS", "max_chars", DEFAULT_MAX_CHARS)
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS

    threshold = pick("REMEM_FUZZY_THRESHOLD", "fuzzy_threshold",
                     DEFAULT_FUZZY_THRESHOLD)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = DEFAULT_FUZZY_THRESHOLD
    if not 0.0 < threshold <= 1.0:
        # Outside this range the setting is meaningless: 0 matches everything,
        # above 1 matches nothing. Fall back rather than silently disable search.
        threshold = DEFAULT_FUZZY_THRESHOLD

    return Config(
        fuzzy_threshold=threshold,
        dsn=str(pick("REMEM_DSN", "dsn", DEFAULT_DSN)),
        user_handle=str(pick("REMEM_USER_ID", "user_handle", DEFAULT_HANDLE)),
        max_chars=max_chars,
    )
