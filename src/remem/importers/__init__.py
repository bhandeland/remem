"""Importers turn a third-party knowledge store into neutral SourceRecords.

Each importer knows one source's file format and vocabulary. Nothing about
what to do with the records - kind, origin, identity, idempotency - lives
here; that is a service's job, and it is why importers.base carries no
import of anything under remem.services.
"""

from __future__ import annotations
