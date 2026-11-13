"""The cursor codec and the listing declaration: pure, no database."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from conftest import ITEM
from pgkeyset import INVALID_CURSOR_MESSAGE, InvalidCursor, Listing, Sort

NEWEST = Listing("items", Sort.desc(ITEM.c.created_at), Sort.desc(ITEM.c.id))
SEEN = Listing("items:seen", Sort.asc(ITEM.c.seen_at, nulls="first"), Sort.asc(ITEM.c.id))

WHEN = datetime(2026, 3, 4, 5, 6, 7, 891011, tzinfo=UTC)


def _raw(parts: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip("=")


def _sql(clause: sa.ColumnElement[bool]) -> str:
    """The predicate as Postgres SQL, without the casts the typed binds add."""
    return re.sub(
        r"::(VARCHAR|TIMESTAMP WITH TIME ZONE|INTEGER)",
        "",
        str(clause.compile(dialect=postgresql.dialect())),
    )


def test_a_cursor_round_trips_its_key_exactly() -> None:
    zoned = datetime(2026, 3, 4, 5, 6, 7, 1, tzinfo=timezone(timedelta(hours=-10)))
    for key in [(WHEN, "it_1"), (zoned, "it_2")]:
        assert NEWEST.decode(NEWEST.encode(key)) == key
    assert SEEN.decode(SEEN.encode((None, "it_3"))) == (None, "it_3")


def test_the_cursor_is_tagged_base64url_json() -> None:
    cursor = NEWEST.encode((WHEN, "it_1"))
    assert "=" not in cursor
    padded = cursor + "=" * (-len(cursor) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded)) == ["items", WHEN.isoformat(), "it_1"]


def test_a_cursor_from_another_listing_is_refused() -> None:
    oldest = Listing("items:asc", Sort.asc(ITEM.c.created_at), Sort.asc(ITEM.c.id))
    with pytest.raises(InvalidCursor):
        oldest.decode(NEWEST.encode((WHEN, "it_1")))  # same shape, other order
    with pytest.raises(InvalidCursor):
        SEEN.decode(NEWEST.encode((WHEN, "it_1")))


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        "!!!!",
        "W10",  # []
        _raw("items"),
        _raw({"items": 1}),
        _raw(["items"]),
        _raw(["items", WHEN.isoformat()]),
        _raw(["items", WHEN.isoformat(), "it_1", "extra"]),
        _raw(["items", "yesterday", "it_1"]),
        _raw(["items", "2026-03-04T05:06:07", "it_1"]),  # naive
        _raw(["items", 1772600767, "it_1"]),
        _raw(["items", None, "it_1"]),  # NULL where the column cannot be
        _raw(["items", WHEN.isoformat(), 7]),
        _raw(["items", WHEN.isoformat(), "it\x001"]),  # NUL, which Postgres text cannot hold
        _raw(["items", "0001-01-01T00:00:00+05:00", "it_1"]),  # before datetime.min in UTC
        _raw(["items", "9999-12-31T23:59:59-05:00", "it_1"]),  # after datetime.max in UTC
        _raw(["items", WHEN.isoformat(), "a\ud800b"]),  # a lone surrogate, not encodable UTF-8
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        base64.urlsafe_b64encode(b"[" * 100_000 + b"]" * 100_000).decode(),  # nesting
    ],
)
def test_a_malformed_cursor_is_refused_with_fixed_text(cursor: str) -> None:
    with pytest.raises(InvalidCursor) as raised:
        NEWEST.decode(cursor)
    assert str(raised.value) == INVALID_CURSOR_MESSAGE
    assert isinstance(raised.value, ValueError)


def test_an_int_position_refuses_a_bool() -> None:
    band = sa.case((ITEM.c.status == "draft", 0), else_=1)
    listing = Listing("review", Sort.asc(band, value=lambda r: 0), Sort.asc(ITEM.c.id))
    assert listing.decode(_raw(["review", 1, "it_1"])) == (1, "it_1")
    with pytest.raises(InvalidCursor):
        listing.decode(_raw(["review", True, "it_1"]))
    # The band binds as Postgres ``integer``, so a key past 32 bits would fail at execute.
    for out_of_range in (2**31, -(2**31) - 1, 2**70):
        with pytest.raises(InvalidCursor):
            listing.decode(_raw(["review", out_of_range, "it_1"]))
    assert listing.decode(_raw(["review", 2**31 - 1, "it_1"])) == (2**31 - 1, "it_1")


def test_a_numeric_position_travels_as_its_decimal_string() -> None:
    """A JSON number would round-trip through a float; the digits are the position."""
    amount = sa.cast(ITEM.c.id, sa.Numeric)
    listing = Listing("amount", Sort.asc(amount, value=lambda r: 0), Sort.asc(ITEM.c.id))
    key = (Decimal("27.10"), "it_1")
    cursor = listing.encode(key)
    padded = cursor + "=" * (-len(cursor) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded)) == ["amount", "27.10", "it_1"]
    assert listing.decode(cursor) == key
    for not_a_position in (27.1, 27, "many", "NaN", "Infinity", True):
        with pytest.raises(InvalidCursor):
            listing.decode(_raw(["amount", not_a_position, "it_1"]))


def test_a_float_column_cannot_be_a_sort_key() -> None:
    """A float's cursor value would not be the value the row holds, so the type is refused."""
    with pytest.raises(TypeError, match="cannot carry"):
        Sort.asc(sa.cast(ITEM.c.id, sa.Float), value=lambda r: 0)


@pytest.mark.parametrize(
    ("type_", "bits"),
    [(sa.SmallInteger, 16), (sa.SMALLINT, 16), (sa.BigInteger, 64), (sa.BIGINT, 64)],
)
def test_an_int_position_fits_its_column_width(type_: type[sa.Integer], bits: int) -> None:
    key = sa.cast(ITEM.c.id, type_)
    listing = Listing("n", Sort.asc(key, value=lambda r: 0), Sort.asc(ITEM.c.id))
    top = (1 << (bits - 1)) - 1
    assert listing.decode(_raw(["n", top, "it_1"])) == (top, "it_1")
    with pytest.raises(InvalidCursor):
        listing.decode(_raw(["n", top + 1, "it_1"]))


def test_encoding_a_naive_timestamp_is_a_server_bug() -> None:
    with pytest.raises(ValueError, match="naive"):
        NEWEST.encode((WHEN.replace(tzinfo=None), "it_1"))


def test_an_absent_or_empty_cursor_is_the_first_page() -> None:
    for cursor in (None, ""):
        stmt = NEWEST.apply(sa.select(ITEM), cursor)
        assert stmt.whereclause is None


def test_paginate_encodes_the_last_row_kept() -> None:
    rows = [{"id": f"it_{i}", "created_at": WHEN} for i in range(4)]
    page, cursor = NEWEST.paginate(rows, 3)
    assert page == rows[:3]
    assert cursor is not None
    assert NEWEST.decode(cursor) == (WHEN, "it_2")
    assert NEWEST.paginate(rows, 4) == (rows, None)
    assert NEWEST.paginate([], 4) == ([], None)


def test_a_uniform_order_is_one_row_comparison() -> None:
    assert _sql(NEWEST.after((WHEN, "it_1"))) == (
        "(item.created_at, item.id) < (%(param_1)s, %(param_2)s)"
    )


def test_a_mixed_or_nullable_order_is_the_explicit_form() -> None:
    mixed = Listing("mixed", Sort.asc(ITEM.c.status), Sort.desc(ITEM.c.id))
    assert _sql(mixed.after(("draft", "it_1"))) == (
        "item.status > %(param_1)s OR item.status = %(param_2)s AND item.id < %(param_3)s"
    )
    assert _sql(SEEN.after((None, "it_1"))) == (
        "item.seen_at IS NOT NULL OR item.seen_at IS NULL AND item.id > %(param_1)s"
    )


def test_order_by_spells_out_null_placement_only_where_nulls_exist() -> None:
    compiled = str(SEEN.apply(sa.select(ITEM.c.id), None).compile(dialect=postgresql.dialect()))
    assert compiled.endswith("ORDER BY item.seen_at ASC NULLS FIRST, item.id ASC")
    compiled = str(NEWEST.apply(sa.select(ITEM.c.id), None).compile(dialect=postgresql.dialect()))
    assert compiled.endswith("ORDER BY item.created_at DESC, item.id DESC")


def test_an_already_ordered_statement_is_refused() -> None:
    with pytest.raises(ValueError, match="owns the order") as raised:
        NEWEST.apply(sa.select(ITEM).order_by(ITEM.c.id.asc()), None)
    assert not isinstance(raised.value, InvalidCursor)
    with pytest.raises(ValueError, match="owns the order"):
        NEWEST.apply(
            sa.select(ITEM).order_by(ITEM.c.created_at.desc()), NEWEST.encode((WHEN, "it_1"))
        )


def test_a_declaration_that_cannot_page_safely_is_refused() -> None:
    with pytest.raises(ValueError, match="nullable"):
        Sort.asc(ITEM.c.seen_at)
    with pytest.raises(ValueError, match="tiebreak"):
        Listing("x", Sort.asc(ITEM.c.id), Sort.asc(ITEM.c.seen_at, nulls="last"))
    with pytest.raises(ValueError, match="value="):
        Sort.asc(sa.case((ITEM.c.status == "draft", 0), else_=1))
    with pytest.raises(TypeError, match="timezone"):
        Sort.asc(sa.column("at", sa.DateTime()))
    with pytest.raises(TypeError):
        Sort.asc(sa.column("blob", sa.LargeBinary()))
    with pytest.raises(ValueError):
        Listing("x")


# --- numeric: the range the driver carries, and a declared precision and scale -------------- #

AMOUNT = Listing(
    "amount", Sort.asc(sa.cast(ITEM.c.id, sa.Numeric), value=lambda r: 0), Sort.asc(ITEM.c.id)
)
PRICE = Listing(
    "price", Sort.asc(sa.cast(ITEM.c.id, sa.Numeric(10, 2)), value=lambda r: 0), Sort.asc(ITEM.c.id)
)


@pytest.mark.parametrize(
    "value",
    [
        "1E+131071",  # the largest magnitude asyncpg binds exactly
        "-1E+131071",
        "1E-16383",  # the smallest scale it carries
        "0E+200000",  # a zero carries no digits, whatever its exponent
        "1." + "0" * 16383,
        "123.4560000",
    ],
)
def test_a_numeric_inside_the_driver_range_decodes(value: str) -> None:
    assert AMOUNT.decode(_raw(["amount", value, "it_1"])) == (Decimal(value), "it_1")


@pytest.mark.parametrize(
    "value",
    [
        "1E+131072",  # asyncpg would bind it as 0: the walk would resume somewhere else
        "1E+200000",
        "9" * 131073,
        "-1E+131072",
        "1E-16384",  # the driver refuses it with its own error text
        "0E-20000",
        "12E-16384",
        "1." + "0" * 16384,
    ],
)
def test_a_numeric_outside_the_driver_range_is_refused(value: str) -> None:
    with pytest.raises(InvalidCursor) as raised:
        AMOUNT.decode(_raw(["amount", value, "it_1"]))
    assert str(raised.value) == INVALID_CURSOR_MESSAGE


def test_a_numeric_fits_its_declared_precision_and_scale() -> None:
    for fits in ("12345678.99", "-12345678.99", "0.5", "1.50", "1.500", "0", "0E+50", "1E+7"):
        assert PRICE.decode(_raw(["price", fits, "it_1"])) == (Decimal(fits), "it_1")
    # More decimals than the column holds would round in the bind's cast; more integer digits
    # would overflow it. No row holds either, so neither is a position.
    for beyond in ("1.005", "0.001", "123456789.0", "1E+8", "1E+20"):
        with pytest.raises(InvalidCursor):
            PRICE.decode(_raw(["price", beyond, "it_1"]))


def test_a_numeric_without_a_declared_scale_holds_integers() -> None:
    whole = Listing(
        "whole", Sort.asc(sa.cast(ITEM.c.id, sa.Numeric(5)), value=lambda r: 0), Sort.asc(ITEM.c.id)
    )
    assert whole.decode(_raw(["whole", "99999", "it_1"])) == (Decimal("99999"), "it_1")
    for beyond in ("100000", "1.5"):
        with pytest.raises(InvalidCursor):
            whole.decode(_raw(["whole", beyond, "it_1"]))


def test_a_numeric_read_as_float_cannot_be_a_sort_key() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        Sort.asc(sa.cast(ITEM.c.id, sa.Numeric(asdecimal=False)), value=lambda r: 0)


def test_encoding_a_numeric_the_cursor_cannot_carry_is_a_server_bug() -> None:
    for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError, match="non-finite"):
            AMOUNT.encode((value, "it_1"))
    with pytest.raises(ValueError, match="Decimal"):
        AMOUNT.encode((27.1, "it_1"))


# --- enum and uuid keys ------------------------------------------------------------------ #

STATUS = sa.Enum("draft", "sent", "failed", name="item_status")


def test_an_enum_position_is_one_of_its_labels() -> None:
    listing = Listing(
        "by_status",
        Sort.asc(sa.cast(ITEM.c.status, STATUS), value=lambda r: 0),
        Sort.asc(ITEM.c.id),
    )
    assert listing.decode(listing.encode(("sent", "it_1"))) == ("sent", "it_1")
    for not_a_label in ("bogus", "SENT", ""):
        with pytest.raises(InvalidCursor):
            listing.decode(_raw(["by_status", not_a_label, "it_1"]))


def test_a_python_enum_cannot_be_a_sort_key() -> None:
    import enum

    class Status(enum.Enum):
        draft = "d"
        sent = "s"

    with pytest.raises(TypeError, match="Python-enum"):
        Sort.asc(sa.cast(ITEM.c.status, sa.Enum(Status, name="st")), value=lambda r: 0)


def test_a_uuid_position_travels_in_its_canonical_form() -> None:
    import uuid

    at = sa.column("created_at", sa.DateTime(timezone=True))
    as_uuid = Listing("u", Sort.desc(at), Sort.desc(sa.column("id", sa.Uuid)))
    as_text = Listing("t", Sort.desc(at), Sort.desc(sa.column("id", sa.Uuid(as_uuid=False))))
    native = Listing("n", Sort.asc(sa.column("id", postgresql.UUID())))
    key = uuid.UUID("abcdef01-2345-6789-abcd-ef0123456789")
    cursor = as_uuid.encode((WHEN, key))
    padded = cursor + "=" * (-len(cursor) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded))[2] == str(key)
    assert as_uuid.decode(cursor) == (WHEN, key)
    assert as_text.decode(as_text.encode((WHEN, str(key)))) == (WHEN, str(key))
    assert native.decode(native.encode((key,))) == (key,)
    for not_canonical in (str(key).upper(), key.hex, f"{{{key}}}", f"urn:uuid:{key}", "x", 7):
        with pytest.raises(InvalidCursor):
            as_uuid.decode(_raw(["u", WHEN.isoformat(), not_canonical]))


# --- the cursor itself, and the page size ------------------------------------------------ #


def test_a_cursor_that_is_not_text_or_is_oversized_is_refused() -> None:
    for not_text in (b"WyJpdGVtcyJd", 7, ["items"]):
        with pytest.raises(InvalidCursor):
            NEWEST.decode(not_text)  # ty: ignore[invalid-argument-type]
    with pytest.raises(InvalidCursor):
        NEWEST.apply(sa.select(ITEM), b"WyJpdGVtcyJd")  # ty: ignore[invalid-argument-type]
    with pytest.raises(InvalidCursor):
        NEWEST.decode("A" * ((1 << 20) + 1))


def test_a_negative_page_size_is_a_server_bug() -> None:
    with pytest.raises(ValueError, match="negative") as raised:
        NEWEST.paginate([], -1)
    assert not isinstance(raised.value, InvalidCursor)
