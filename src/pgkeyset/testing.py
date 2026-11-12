"""The walk test for a paged read: every row once, with rows inserted and removed mid-walk.

:func:`walk` follows ``next_cursor`` from the first page to the last and fails on a row met
twice, or, given the rows the walk must meet, on one it never met. ``between_pages`` runs
after each page that has a next one, which is where a test inserts and removes rows to show
the position does not move when other rows leave.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable, Iterable, Sequence

__all__ = ["walk"]


async def walk[T](
    fetch: Callable[[str | None], Awaitable[tuple[Sequence[T], str | None]]],
    *,
    ident: Callable[[T], Hashable],
    expected: Iterable[Hashable] | None = None,
    between_pages: Callable[[int], Awaitable[None]] | None = None,
    max_pages: int = 1000,
) -> list[T]:
    """Walk a listing to its end and return every row met, in order.

    ``fetch(cursor)`` reads one page as ``(rows, next_cursor)``, passing the same filters
    every call. ``ident`` names a row. ``expected``, when given, is every row the walk must
    meet: those that existed when it began and still match. ``between_pages(n)`` runs after
    page ``n`` (from 1) when another page follows.
    """
    walked: list[T] = []
    seen: set[Hashable] = set()
    cursor: str | None = None
    for page_number in range(1, max_pages + 1):
        rows, next_cursor = await fetch(cursor)
        for row in rows:
            row_id = ident(row)
            assert row_id not in seen, f"page {page_number} repeats {row_id!r}"
            seen.add(row_id)
            walked.append(row)
        if next_cursor is None:
            break
        assert rows, f"page {page_number} is empty but hands out a next_cursor"
        if between_pages is not None:
            await between_pages(page_number)
        cursor = next_cursor
    else:
        raise AssertionError(f"the walk did not end within {max_pages} pages")
    if expected is not None:
        want = set(expected)
        missing, extra = want - seen, seen - want
        assert not missing, f"the walk never met {sorted(map(repr, missing))}"
        assert not extra, f"the walk met rows it should not have: {sorted(map(repr, extra))}"
    return walked
