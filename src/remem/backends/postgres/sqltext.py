"""The one place this backend says "this string really is SQL".

psycopg types `execute`'s query parameter as `LiteralString` for one
reason: so a string built out of user data can never arrive as SQL text.
Both modules in this package build queries with f-strings and both need to
say so, and two copies of that statement would drift - which is the whole
argument for it living here instead.
"""

from __future__ import annotations

from typing import LiteralString, cast


def as_sql(text: str) -> LiteralString:
    """SQL assembled from this package's own constants.

    Named `as_sql` rather than `sql` because both callers already use `sql`
    as a local for the query they are assembling, and a helper that shadows
    the thing it is called on is a trap.

    Every interpolation in this backend is a constant defined in it - a
    column list from `entry_columns()`, a `where` fragment built from fixed
    clauses, a migration file read off disk - and every value goes through
    a parameter. The cast is therefore true.

    It is a named function rather than an inline cast so the exemption is
    visible: putting anything that is not a module constant into a query
    means calling this, which is a deliberate act a reader can find with
    one grep, rather than an f-string nobody notices.
    """
    return cast(LiteralString, text)
