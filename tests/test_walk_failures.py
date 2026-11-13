"""The walk test fails when a listing is wrong: each failure it promises, from a scripted fetch.

These need no database. Each fake ``fetch`` plays back pages the way a broken listing would
return them, and the walk must refuse with an ``AssertionError`` that names what went wrong.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from pgkeyset.testing import walk


def _pages(*pages: tuple[Sequence[str], str | None]):
    """A ``fetch`` that serves ``pages`` in order, keyed by the cursor each one handed out."""
    by_cursor: dict[str | None, tuple[Sequence[str], str | None]] = {}
    cursor: str | None = None
    for rows, next_cursor in pages:
        by_cursor[cursor] = (rows, next_cursor)
        cursor = next_cursor

    async def fetch(cursor: str | None) -> tuple[Sequence[str], str | None]:
        return by_cursor[cursor]

    return fetch


async def test_a_clean_walk_returns_every_row_in_order() -> None:
    fetch = _pages((["a", "b"], "c1"), (["c", "d"], "c2"), (["e"], None))
    assert await walk(fetch, ident=str, expected="abcde") == list("abcde")


async def test_a_row_met_twice_fails() -> None:
    fetch = _pages((["a", "b"], "c1"), (["b", "c"], None))
    with pytest.raises(AssertionError, match=r"page 2 repeats 'b'"):
        await walk(fetch, ident=str)


async def test_an_expected_row_never_met_fails() -> None:
    fetch = _pages((["a", "b"], "c1"), (["d"], None))
    with pytest.raises(AssertionError, match=r"never met \[\"'c'\"\]"):
        await walk(fetch, ident=str, expected="abcd")


async def test_a_row_met_that_was_not_expected_fails() -> None:
    fetch = _pages((["a", "b"], "c1"), (["x"], None))
    with pytest.raises(AssertionError, match="should not have"):
        await walk(fetch, ident=str, expected="ab")


async def test_an_empty_page_that_hands_out_a_cursor_fails() -> None:
    fetch = _pages((["a"], "c1"), ([], "c2"), (["b"], None))
    with pytest.raises(AssertionError, match="page 2 is empty but hands out a next_cursor"):
        await walk(fetch, ident=str)


async def test_a_walk_that_never_ends_fails() -> None:
    count = 0

    async def endless(cursor: str | None) -> tuple[Sequence[str], str | None]:
        nonlocal count
        count += 1
        return [f"r{count}"], f"c{count}"

    with pytest.raises(AssertionError, match="did not end within 5 pages"):
        await walk(endless, ident=str, max_pages=5)
    assert count == 5


async def test_between_pages_runs_after_each_page_that_has_a_next() -> None:
    calls: list[int] = []

    async def record(page: int) -> None:
        calls.append(page)

    fetch = _pages((["a"], "c1"), (["b"], "c2"), (["c"], None))
    await walk(fetch, ident=str, between_pages=record)
    assert calls == [1, 2]
