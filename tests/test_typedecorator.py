"""A sort column whose type is a TypeDecorator is classified by the type it stores.

The declaration rules (which kinds a cursor carries, timezone-aware timestamps only, integer
widths, numeric precision and scale, enum labels, uuid form) apply to the stored type. Binds
stay typed with the decorator, so its processing runs on a cursor value exactly as on a value
written to the column.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import LEDGER, Passthrough, UTCDateTime
from pgkeyset import InvalidCursor, Listing, Sort
from pgkeyset.testing import walk

_md = sa.MetaData()


def _col(name: str, type_: sa.types.TypeEngine[Any], **kw: Any) -> sa.Column[Any]:
    return sa.Column(name, type_, **kw)


_T = sa.Table(
    "decorated",
    _md,
    _col("id", Passthrough(sa.Text()), primary_key=True),
    _col("at", UTCDateTime(), nullable=False),
    _col("naive", Passthrough(sa.DateTime()), nullable=False),
    _col("small", Passthrough(sa.SmallInteger()), nullable=False),
    _col("big", Passthrough(Passthrough(sa.BigInteger())), nullable=False),
    _col("amount", Passthrough(sa.Numeric(5, 2)), nullable=False),
    _col("ratio", Passthrough(sa.Float()), nullable=False),
    _col("level", Passthrough(sa.Enum("low", "high", name="level")), nullable=False),
    _col("ref", Passthrough(sa.Uuid(as_uuid=False)), nullable=False),
    _col("seen", UTCDateTime(), nullable=True),
)


def _round_trip(listing: Listing, key: tuple[Any, ...]) -> tuple[Any, ...]:
    return listing.decode(listing.encode(key))


def test_a_decorated_column_takes_the_kind_of_the_type_it_stores() -> None:
    assert Sort.asc(_T.c.at).kind == "ts"
    assert Sort.asc(_T.c.small).kind == "int"
    assert Sort.asc(_T.c.amount).kind == "num"
    assert Sort.asc(_T.c.level).kind == "str"
    assert Sort.asc(_T.c.ref).kind == "uuid"
    assert Sort.asc(_T.c.id).kind == "str"


def test_nested_decorators_are_unwrapped_to_the_stored_type() -> None:
    listing = Listing("nested", Sort.asc(_T.c.big), Sort.asc(_T.c.id))
    assert listing.order[0].kind == "int"
    # bigint width, not the default integer width, through two decorators.
    assert _round_trip(listing, (2**40, "a")) == (2**40, "a")


def test_a_decorator_over_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(TypeError, match="timezone-aware"):
        Sort.asc(_T.c.naive)


def test_a_decorator_over_a_type_the_cursor_cannot_carry_is_refused() -> None:
    with pytest.raises(TypeError, match="cannot carry"):
        Sort.asc(_T.c.ratio)


def test_a_decorated_column_still_declares_its_null_placement() -> None:
    with pytest.raises(ValueError, match="nullable"):
        Sort.asc(_T.c.seen)
    assert Sort.asc(_T.c.seen, nulls="last").nullable


def test_a_decorated_integer_position_fits_the_stored_width() -> None:
    listing = Listing("small", Sort.asc(_T.c.small), Sort.asc(_T.c.id))
    assert _round_trip(listing, (2**15 - 1, "a")) == (2**15 - 1, "a")
    with pytest.raises(InvalidCursor):
        _round_trip(listing, (2**15, "a"))


def test_a_decorated_numeric_fits_the_stored_precision_and_scale() -> None:
    listing = Listing("amount", Sort.asc(_T.c.amount), Sort.asc(_T.c.id))
    assert _round_trip(listing, (Decimal("999.99"), "a")) == (Decimal("999.99"), "a")
    for outside in ("1000.00", "1.005"):
        with pytest.raises(InvalidCursor):
            _round_trip(listing, (Decimal(outside), "a"))


def test_a_decorated_enum_position_is_one_of_its_labels() -> None:
    listing = Listing("level", Sort.asc(_T.c.level), Sort.asc(_T.c.id))
    assert _round_trip(listing, ("high", "a")) == ("high", "a")
    with pytest.raises(InvalidCursor):
        _round_trip(listing, ("urgent", "a"))


def test_a_decorated_uuid_keeps_the_stored_types_python_form() -> None:
    listing = Listing("ref", Sort.asc(_T.c.ref), Sort.asc(_T.c.id))
    ref = "00000000-0000-0000-0000-000000000001"
    # as_uuid=False on the stored type: the decoded key is the string.
    assert _round_trip(listing, (ref, "a")) == (ref, "a")


def test_a_decorated_timestamp_decodes_aware_in_utc() -> None:
    listing = Listing("at", Sort.desc(_T.c.at), Sort.desc(_T.c.id))
    at = datetime(2026, 3, 1, 12, tzinfo=UTC)
    assert _round_trip(listing, (at, "a")) == (at, "a")


def test_a_bind_stays_typed_with_the_decorator() -> None:
    """The decorator's own bind processing runs on a cursor value, as on a written one."""
    sort = Sort.desc(_T.c.at)
    bound = sort.bind(datetime(2026, 3, 1, tzinfo=UTC))
    assert isinstance(bound.type, UTCDateTime)


async def test_decorated_keys_walk_on_postgres(engine: AsyncEngine) -> None:
    """Every sort column decorated: the walk resumes exactly, inside ties, as rows churn."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "id": uuid.UUID(int=i + 1),
            # Pairs share a timestamp, so the walk resumes inside ties.
            "posted_at": t0 + timedelta(minutes=i // 2),
            "rank": i % 3,
            "amount": Decimal(f"{(i % 4) * 10}.25"),
        }
        for i in range(20)
    ]
    async with engine.begin() as conn:
        await conn.execute(LEDGER.insert(), rows)

    for listing in (
        Listing("ledger:newest", Sort.desc(LEDGER.c.posted_at), Sort.desc(LEDGER.c.id)),
        Listing(
            "ledger:rank",
            Sort.asc(LEDGER.c.rank),
            Sort.desc(LEDGER.c.amount),
            Sort.asc(LEDGER.c.id),
        ),
    ):
        async with engine.connect() as conn:
            everything = (await conn.execute(listing.apply(sa.select(LEDGER.c.id), None))).all()
        order = [r.id for r in everything]

        async def fetch(
            cursor: str | None, listing: Listing = listing
        ) -> tuple[list[sa.RowMapping], str | None]:
            async with engine.connect() as conn:
                stmt = listing.apply(sa.select(LEDGER), cursor).limit(4)
                page = (await conn.execute(stmt)).mappings().all()
            return listing.paginate(page, 3)

        walked = await walk(fetch, ident=lambda r: r["id"], expected=set(order))
        assert [r["id"] for r in walked] == order
        # The decorator hands back an aware timestamp, which the cursor carries.
        assert all(r["posted_at"].tzinfo is not None for r in walked)
