from __future__ import annotations

import warnings
from importlib.metadata import entry_points

GROUP = "remem.agents"


class UnknownAgent(Exception):
    """Raised when no adapter is registered under the requested name."""


def discover() -> dict[str, type]:
    found: dict[str, type] = {}
    for ep in entry_points(group=GROUP):
        try:
            found[ep.name] = ep.load()
        except Exception as exc:  # a broken third-party adapter must not break remem
            warnings.warn(
                f"remem agent adapter '{ep.name}' ({ep.value}) failed to load: {exc!r}",
                stacklevel=2,
            )
            continue
    return found


def get(name: str) -> type:
    found = discover()
    if name not in found:
        known = ", ".join(sorted(found)) or "none"
        raise UnknownAgent(f"unknown agent '{name}'. Available: {known}")
    return found[name]
