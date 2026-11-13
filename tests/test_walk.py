"""Walks against a real Postgres: every row once, in the listing's order, as the table changes."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import ITEM, TICKET
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


#: Highest priority first (the enum's declared order, not the alphabet), largest amount next,
#: and the uuid id as the tiebreak: one listing that binds all three kinds asyncpg types.
TRIAGE = Listing(
    "tickets:triage",
    Sort.desc(TICKET.c.priority),
    Sort.desc(TICKET.c.amount),
    Sort.asc(TICKET.c.id),
)


async def test_uuid_enum_and_numeric_keys_walk_on_postgres(engine: AsyncEngine) -> None:
    """The enum, ``numeric(10, 2)`` and ``uuid`` binds reach Postgres typed, and resume exactly.

    Amounts repeat inside each priority, so the walk resumes inside ties on the first two keys
    and the uuid decides. Rows are removed between pages, behind and ahead of the cursor.
    """
    rows = [
        {
            "id": uuid.UUID(int=i + 1),
            "priority": ("low", "high", "urgent")[i % 3],
            "amount": Decimal(f"{(i % 4) * 10}.50"),
        }
        for i in range(24)
    ]
    async with engine.begin() as conn:
        await conn.execute(TICKET.insert(), rows)
    async with engine.connect() as conn:
        everything = (await conn.execute(TRIAGE.apply(sa.select(TICKET.c.id), None))).all()
    order = [r.id for r in everything]
    assert order[0] in {row["id"] for row in rows if row["priority"] == "urgent"}

    removed: list[uuid.UUID] = []

    async def fetch(cursor: str | None) -> tuple[list[sa.RowMapping], str | None]:
        async with engine.connect() as conn:
            stmt = TRIAGE.apply(sa.select(TICKET), cursor).limit(6)
            page = (await conn.execute(stmt)).mappings().all()
        return TRIAGE.paginate(page, 5)

    async def churn(page: int) -> None:
        # Remove the first remaining row (behind the cursor) and the last one (ahead of it).
        gone = [order[page - 1], order[-page]]
        removed.extend(gone)
        async with engine.begin() as conn:
            await conn.execute(TICKET.delete().where(TICKET.c.id.in_(gone)))

    walked = await walk(fetch, ident=lambda r: r["id"], between_pages=churn)
    walked_ids = [r["id"] for r in walked]
    # Every row not removed ahead of the cursor came back once, in the listing's order.
    assert walked_ids == [i for i in order if i in walked_ids]
    assert set(order) - set(walked_ids) <= set(removed)


async def test_a_cursor_outside_the_enum_or_the_numeric_column_is_refused(
    engine: AsyncEngine,
) -> None:
    """Refused at decode with the fixed text, before any SQL reaches Postgres."""
    valid = TRIAGE.encode(("high", Decimal("10.50"), uuid.UUID(int=1)))
    raw = TRIAGE.decode(valid)
    assert raw == ("high", Decimal("10.50"), uuid.UUID(int=1))

    def cursor(*key: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(["tickets:triage", *key]).encode()).decode()

    u = str(uuid.UUID(int=1))
    for bad in (
        cursor("bogus", "10.50", u),  # not a label of the enum
        cursor("high", "1.005", u),  # more decimals than numeric(10, 2) holds: would round
        cursor("high", "123456789.0", u),  # more integer digits than it holds: overflow
        cursor("high", "1E+131072", u),  # asyncpg would bind it as 0
        cursor("high", "10.50", "ABCDEF01-2345-6789-ABCD-EF0123456789"),  # not canonical
        cursor("high", "10.50", "abcdef0123456789abcdef0123456789"),  # not canonical
        cursor("high", "10.50", "not-a-uuid"),
    ):
        with pytest.raises(InvalidCursor):
            async with engine.connect() as conn:
                await conn.execute(TRIAGE.apply(sa.select(TICKET), bad).limit(1))


async def test_walk_catches_an_offset_listing(engine: AsyncEngine) -> None:
    """The control: OFFSET paging skips a row when one ahead of it leaves, and walk says so."""
    await _seed(engine, [{"id": f"it_{i:02}", "created_at": T0} for i in range(9)])

    async def fetch_by_offset(cursor: str | None) -> tuple[list[sa.RowMapping], str | None]:
        offset = int(cursor or 0)
        async with engine.connect() as conn:
            stmt = sa.select(ITEM).order_by(ITEM.c.id).offset(offset).limit(4)
            page = (await conn.execute(stmt)).mappings().all()
        more = len(page) > 3
        return list(page[:3]), str(offset + 3) if more else None

    async def remove_one_already_read(page: int) -> None:
        async with engine.begin() as conn:
            await conn.execute(ITEM.delete().where(ITEM.c.id == f"it_{page - 1:02}"))

    expected = {f"it_{i:02}" for i in range(9)} - {"it_00", "it_01"}
    with pytest.raises(AssertionError, match="never met"):
        await walk(
            fetch_by_offset,
            ident=_ident,
            expected=expected | {"it_00", "it_01"},
            between_pages=remove_one_already_read,
        )


async def test_unusual_numeric_scales_walk_on_postgres(engine: AsyncEngine) -> None:
    """``numeric(3, -2)`` and ``numeric(2, 3)`` (Postgres 15+): every row is a page boundary,
    so every value the column holds goes out in a cursor and comes back as a position."""
    async with engine.connect() as conn:
        version = (await conn.execute(sa.text("SHOW server_version_num"))).scalar_one()
    if int(version) < 150000:
        pytest.skip("Postgres before 15 allows neither a negative scale nor one above precision")
    meta = sa.MetaData()
    scaled = sa.Table(
        "scaled",
        meta,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("hundreds", sa.Numeric(3, -2), nullable=False),
        sa.Column("fine", sa.Numeric(2, 3), nullable=False),
    )
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        await conn.execute(
            scaled.insert(),
            [
                {"id": f"s{i}", "hundreds": Decimal(h), "fine": Decimal(f)}
                for i, (h, f) in enumerate(
                    [("1200", "0.012"), ("99900", "0.099"), ("0", "0"), ("-500", "-0.010")] * 2
                )
            ],
        )
    try:
        for listing in (
            Listing("by_hundreds", Sort.desc(scaled.c.hundreds), Sort.asc(scaled.c.id)),
            Listing("by_fine", Sort.asc(scaled.c.fine), Sort.desc(scaled.c.id)),
        ):

            async def fetch(
                cursor: str | None, listing: Listing = listing
            ) -> tuple[list[sa.RowMapping], str | None]:
                async with engine.connect() as conn:
                    stmt = listing.apply(sa.select(scaled), cursor).limit(2)
                    page = (await conn.execute(stmt)).mappings().all()
                return listing.paginate(page, 1)

            walked = await walk(
                fetch, ident=lambda r: r["id"], expected={f"s{i}" for i in range(8)}
            )
            assert len(walked) == 8
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(meta.drop_all)
