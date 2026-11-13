"""Strict keyset pagination for async SQLAlchemy on Postgres.

Code declares each paged read once as a :class:`Listing`, its name and total order, and
the listing does the rest::

    CONTACTS = Listing("contacts", Sort.desc(contact.c.created_at), Sort.desc(contact.c.id))

    stmt = CONTACTS.apply(select(contact), cursor)        # ORDER BY + strictly-after
    rows = (await conn.execute(stmt.limit(limit + 1))).mappings().all()
    page, next_cursor = CONTACTS.paginate(rows, limit)

A cursor that is not a position in the listing raises :class:`InvalidCursor`, which the
server maps to its own bad-input error. :mod:`pgkeyset.testing` holds the walk test that
proves a listing never skips or repeats a row.
"""

from .core import INVALID_CURSOR_MESSAGE, InvalidCursor, Listing, Sort

__all__ = ["INVALID_CURSOR_MESSAGE", "InvalidCursor", "Listing", "Sort"]
__version__ = "0.2.0"
