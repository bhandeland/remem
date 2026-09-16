"""`agent_id` is required on every transcript identity lookup.

A session's own transcript and each of its subagents share a `session_id`,
and `agent_id` None is how the session's row is spelled. With a default of
None, a caller that forgets the argument does not fail - it reads, or
overwrites, the SESSION row. Required and keyword-only means forgetting it is
a TypeError at the call, and passing it positionally cannot slide into the
wrong slot. No database: this is a fact about the signatures.
"""

from __future__ import annotations

import inspect

import pytest

from saddlebag.backends.postgres.store import PostgresStore
from saddlebag.store import Store


@pytest.mark.parametrize("cls", [Store, PostgresStore])
@pytest.mark.parametrize("method", ["put_transcript", "get_transcript"])
def test_agent_id_is_a_required_keyword(cls: type, method: str) -> None:
    param = inspect.signature(getattr(cls, method)).parameters["agent_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
