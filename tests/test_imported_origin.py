"""The imported origin, and the two lists that decide what it means.

Pure - no database. The values are spelled literally rather than derived
from the enum, because a test that builds its expectations out of the module
under test pins nothing.
"""

from __future__ import annotations

from remem.domain import INJECTED_ORIGINS, Origin
from remem.services.search import DEFAULT_ORIGINS


def test_the_imported_origin_exists():
    assert Origin.IMPORTED == "imported"


def test_imported_is_searchable_by_default():
    """DEFAULT_ORIGINS is an allowlist. An origin missing from it does not
    narrow results - it disappears from them entirely, silently."""
    assert Origin.IMPORTED in DEFAULT_ORIGINS


def test_imported_is_not_injected_into_context_blocks():
    """kb.resolve filters the query half to INJECTED_ORIGINS, so absence
    here is what keeps imported machine text from crowding out hand-written
    rules. Same treatment as EXTRACTED."""
    assert Origin.IMPORTED not in INJECTED_ORIGINS
