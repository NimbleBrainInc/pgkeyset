"""Walks against a real Postgres: every row once, in the listing's order, as the table changes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import ITEM
from pgkeyset import InvalidCursor, Listing, Sort
from pgkeyset.testing import walk

T0 = datetime(2026, 1, 1, tzinfo=UTC)

NEWEST = Listing("items", Sort.desc(ITEM.c.created_at), Sort.desc(ITEM.c.id))

_DRAFT_FIRST = sa.case((ITEM.c.status == "draft", 0), else_=1)
REVIEW = Listing(
    "items:review",
    Sort.asc(_DRAFT_FIRST, value=lambda row: 0 if row["status"] == "draft" else 1),
    Sort.desc(ITEM.c.created_at),
    Sort.desc(ITEM.c.id),
)


async def _seed(engine: AsyncEngine, rows: Sequence[dict[str, Any]]) -> None:
    """Insert ``rows`` in ONE transaction, as a burst of writes sharing ``now()`` would land."""
    async with engine.begin() as conn:
        await conn.execute(ITEM.insert(), [{"seen_at": None, "status": "open", **r} for r in rows])


async def _page(
    engine: AsyncEngine, listing: Listing, cursor: str | None, limit: int
) -> tuple[list[sa.RowMapping], str | None]:
    async with engine.connect() as conn:
        stmt = listing.apply(sa.select(ITEM), cursor).limit(limit + 1)
        rows = (await conn.execute(stmt)).mappings().all()
    return listing.paginate(rows, limit)


async def _all(engine: AsyncEngine, listing: Listing) -> list[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(listing.apply(sa.select(ITEM.c.id), None))).all()
    return [r.id for r in rows]


def _ident(row: sa.RowMapping) -> str:
    return row["id"]


async def test_a_walk_crosses_many_pages_inside_one_timestamp(engine: AsyncEngine) -> None:
    """Ten rows share one ``created_at``, so only the id tiebreak orders them."""
    ids = [f"it_{i:02d}" for i in range(10)]
    await _seed(engine, [{"id": i, "created_at": T0} for i in ids])

    pages = 0

    async def fetch(cursor: str | None) -> tuple[list[sa.RowMapping], str | None]:
        nonlocal pages
        pages += 1
        return await _page(engine, NEWEST, cursor, 3)

    walked = await walk(fetch, ident=_ident, expected=ids)
    assert pages == 4
    assert [r["id"] for r in walked] == sorted(ids, reverse=True)


async def test_rows_inserted_and_removed_between_pages_move_nothing(engine: AsyncEngine) -> None:
    """New rows sort ahead of a newest-first walk, and a removal shifts no later row."""
    ids = [f"it_{i:02d}" for i in range(9)]
    await _seed(
        engine, [{"id": i, "created_at": T0 + timedelta(seconds=n)} for n, i in enumerate(ids)]
    )
    first_page = sorted(ids, reverse=True)[:3]
    unread = "it_02"

    async def between(page_number: int) -> None:
        if page_number != 1:
            return
        await _seed(engine, [{"id": "it_new", "created_at": T0 + timedelta(days=1)}])
        async with engine.begin() as conn:
            # One row already read, one not yet reached.
            await conn.execute(ITEM.delete().where(ITEM.c.id.in_([first_page[0], unread])))

    async def fetch(cursor: str | None) -> tuple[list[sa.RowMapping], str | None]:
        return await _page(engine, NEWEST, cursor, 3)

    expected = set(ids) - {unread}
    walked = await walk(fetch, ident=_ident, expected=expected, between_pages=between)
    assert [r["id"] for r in walked] == sorted(expected, reverse=True)
    assert (await _all(engine, NEWEST))[0] == "it_new"  # a fresh walk meets it


@pytest.mark.parametrize("descending", [False, True])
@pytest.mark.parametrize("nulls", ["first", "last"])
async def test_a_nullable_column_walks_into_and_out_of_its_null_band(
    engine: AsyncEngine, descending: bool, nulls: Literal["first", "last"]
) -> None:
    stamped = [{"id": f"s_{i}", "seen_at": T0 + timedelta(hours=i % 3)} for i in range(5)]
    unstamped = [{"id": f"n_{i}", "seen_at": None} for i in range(4)]
    await _seed(engine, [{"created_at": T0, **r} for r in stamped + unstamped])
    direction = Sort.desc if descending else Sort.asc
    listing = Listing(
        f"items:seen:{descending}:{nulls}",
        direction(ITEM.c.seen_at, nulls=nulls),
        direction(ITEM.c.id),
    )

    async def fetch(cursor: str | None) -> tuple[list[sa.RowMapping], str | None]:
        return await _page(engine, listing, cursor, 2)

    walked = await walk(fetch, ident=_ident, expected=[r["id"] for r in stamped + unstamped])
    order = [r["id"] for r in walked]
    assert order == await _all(engine, listing)
    band = order[:4] if nulls == "first" else order[-4:]
    assert all(i.startswith("n_") for i in band)


async def test_a_computed_band_leads_the_order(engine: AsyncEngine) -> None:
    rows = [
        {"id": f"it_{i}", "created_at": T0 + timedelta(minutes=i), "status": s}
        for i, s in enumerate(["draft", "sent", "draft", "sent", "draft", "rejected", "sent"])
    ]
    await _seed(engine, rows)

    async def fetch(cursor: str | None) -> tuple[list[sa.RowMapping], str | None]:
        return await _page(engine, REVIEW, cursor, 2)

    walked = await walk(fetch, ident=_ident, expected=[r["id"] for r in rows])
    order = [r["id"] for r in walked]
    assert order == await _all(engine, REVIEW)
    assert order[:3] == ["it_4", "it_2", "it_0"]


async def test_a_cursor_from_another_listing_is_refused(engine: AsyncEngine) -> None:
    await _seed(engine, [{"id": f"it_{i}", "created_at": T0} for i in range(3)])
    _, cursor = await _page(engine, NEWEST, None, 1)
    assert cursor is not None
    with pytest.raises(InvalidCursor):
        await _page(engine, REVIEW, cursor, 1)
