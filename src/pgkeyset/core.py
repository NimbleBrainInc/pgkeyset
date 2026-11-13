"""A listing's total order, the predicate that resumes it, and the cursor that names a position.

A :class:`Listing` is one paged read's order, declared once: a name, then its :class:`Sort`
columns from first to last, the last one unique. Everything a paged read needs follows from
that declaration, so it is written nowhere else:

  * ``ORDER BY`` in the declared directions, with the NULL placement spelled out for a
    nullable column instead of left to the database's default.
  * "Strictly after this position", as a row comparison ``(a, id) < (x, y)`` when every
    column runs the same direction and none can be NULL, which Postgres reads as an index
    condition. Otherwise it is the explicit expansion: after on the first column, or equal on
    it and after on the next, and so on, with each NULL band named, since a comparison against
    NULL is NULL. Bound values are typed like their columns, so asyncpg gets a ``datetime``
    for a ``timestamptz``.
  * The cursor, ``base64url(JSON [listing, *key])``, with each timestamp as ISO-8601 carrying
    its zone, each ``numeric`` as its decimal string and each ``uuid`` in its canonical form.
    The listing tag means a cursor from another read is refused rather than read as a position
    in an order it never came from.

Any cursor that does not decode as a position in this listing raises :class:`InvalidCursor`,
whose text is fixed: a parse failure's message never reaches the wire.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import ColumnElement, Select, and_, false, literal, or_, tuple_
from sqlalchemy.sql.elements import ColumnClause, Label
from sqlalchemy.types import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    Integer,
    Numeric,
    SmallInteger,
    String,
    TypeDecorator,
    TypeEngine,
    Uuid,
)

__all__ = ["INVALID_CURSOR_MESSAGE", "InvalidCursor", "Listing", "Sort"]

INVALID_CURSOR_MESSAGE = "The cursor is not a position in this listing."

# The value kinds a key can hold, and so the kinds a cursor can carry back.
_Kind = Literal["ts", "int", "str", "num", "uuid"]

# The longest cursor decoded. A real key is far shorter; a longer string is refused before it is
# base64- and JSON-decoded, so a caller cannot make the server parse an arbitrarily large blob.
_MAX_CURSOR_CHARS = 1 << 20

# The ``numeric`` values asyncpg and Postgres carry exactly: at most 131072 digits before the
# decimal point and 16383 after it. Past the first, asyncpg binds the value as 0; past the
# second, the driver refuses it.
_NUMERIC_MAX_ADJUSTED = 131071
_NUMERIC_MIN_EXPONENT = -16383


class InvalidCursor(ValueError):
    """A cursor that does not decode as a position in the listing it was passed to.

    The message is always :data:`INVALID_CURSOR_MESSAGE`, never the parse failure's, so a
    server can put it on the wire as bad input. The way out is always the same: call again
    without ``cursor``.
    """

    def __init__(self) -> None:
        super().__init__(INVALID_CURSOR_MESSAGE)


@dataclass(frozen=True)
class Sort:
    """One column of a listing's order: an expression, its direction, and where NULLs go.

    ``nulls`` declares the column nullable and places its NULL band ``"first"`` or ``"last"``.
    A table column declared ``nullable`` must say which; anything else is taken as never NULL.
    Only a plain table column is checked, so a computed or labelled expression that can be NULL
    must pass ``nulls`` itself.

    ``value`` reads this column's value from a fetched row, for the cursor. It defaults to
    ``row[<column key>]``, so a computed expression (a ``case`` band) needs one, unless it is
    a labelled expression the query selects.
    """

    expr: ColumnElement[Any]
    descending: bool = False
    nulls: Literal["first", "last"] | None = None
    value: Callable[[Any], Any] | None = None

    @classmethod
    def asc(
        cls,
        expr: ColumnElement[Any],
        *,
        nulls: Literal["first", "last"] | None = None,
        value: Callable[[Any], Any] | None = None,
    ) -> Sort:
        """``expr`` ascending. ``nulls`` places a nullable column's NULL band ``"first"`` or
        ``"last"``; ``value`` reads a computed expression's key from a fetched row."""
        return cls(expr, descending=False, nulls=nulls, value=value)

    @classmethod
    def desc(
        cls,
        expr: ColumnElement[Any],
        *,
        nulls: Literal["first", "last"] | None = None,
        value: Callable[[Any], Any] | None = None,
    ) -> Sort:
        """``expr`` descending. ``nulls`` and ``value`` are as for :meth:`asc`."""
        return cls(expr, descending=True, nulls=nulls, value=value)

    def __post_init__(self) -> None:
        _kind(_base(self.expr.type))  # refuse a type the cursor cannot carry, at declaration
        if self.nulls is None and getattr(self.expr, "nullable", False):
            raise ValueError(
                f"{self.expr} is nullable: declare where its NULLs sort with nulls='first' "
                "or nulls='last'"
            )
        if self.value is None and _row_key(self.expr) is None:
            raise ValueError(f"{self.expr} is computed: pass value= to read it from a row")

    @property
    def kind(self) -> _Kind:
        return _kind(_base(self.expr.type))

    @property
    def nullable(self) -> bool:
        return self.nulls is not None

    def order(self) -> ColumnElement[Any]:
        clause = self.expr.desc() if self.descending else self.expr.asc()
        if self.nulls == "first":
            return clause.nulls_first()
        if self.nulls == "last":
            return clause.nulls_last()
        return clause

    def read(self, row: Any) -> Any:
        if self.value is not None:
            return self.value(row)
        return row[_row_key(self.expr)]

    def bind(self, value: Any) -> ColumnElement[Any]:
        # Typed with the column's own type, a TypeDecorator included, so its bind processing
        # runs on the cursor value exactly as on a value written to the column.
        return literal(value, self.expr.type)

    def equals(self, value: Any) -> ColumnElement[bool]:
        if value is None:
            return self.expr.is_(None)
        return self.expr == self.bind(value)

    def beyond(self, value: Any) -> ColumnElement[bool] | None:
        """Rows that sort strictly after ``value`` on this column alone, or None for none."""
        if value is None:
            # Inside the NULL band: everything after it is the non-NULL rows, or nothing.
            return self.expr.is_not(None) if self.nulls == "first" else None
        bound = self.bind(value)
        past = self.expr < bound if self.descending else self.expr > bound
        if self.nulls == "last":
            return or_(past, self.expr.is_(None))
        return past


class Listing:
    """One paged read: a name for its cursors, and its total order.

    The last :class:`Sort` must be unique and never NULL (the row's id), so no two rows share
    a position and a page boundary never falls inside a tie.
    """

    def __init__(self, name: str, *order: Sort) -> None:
        if not order:
            raise ValueError("a listing needs at least one sort column")
        if order[-1].nullable:
            raise ValueError(
                "a listing's last sort column is its unique tiebreak; it cannot be NULL"
            )
        self.name = name
        self.order = order

    def __repr__(self) -> str:
        return f"Listing({self.name!r})"

    # --- the query ---------------------------------------------------------- #

    def apply[S: Select[Any]](self, stmt: S, cursor: str | None) -> S:
        """Order ``stmt`` by this listing and, given a cursor, start strictly after it.

        An empty or absent cursor is the first page. The caller adds ``.limit(limit + 1)``.
        Raises :class:`InvalidCursor` for a cursor that is not a position in this listing, and
        ``ValueError`` for a ``stmt`` that is already ordered: the listing owns the order, and an
        ORDER BY ahead of it would page in an order the cursor does not follow.
        """
        if stmt._order_by_clauses:
            raise ValueError(f"{self!r} owns the order: pass a statement without ORDER BY")
        stmt = stmt.order_by(*(sort.order() for sort in self.order))
        if cursor:
            stmt = stmt.where(self.after(self.decode(cursor)))
        return stmt

    def after(self, key: Sequence[Any]) -> ColumnElement[bool]:
        """The rows strictly after ``key`` in this order."""
        if self._uniform():
            exprs = [sort.expr for sort in self.order]
            binds = [sort.bind(v) for sort, v in zip(self.order, key, strict=True)]
            if self.order[0].descending:
                return tuple_(*exprs) < tuple_(*binds)
            return tuple_(*exprs) > tuple_(*binds)
        terms: list[ColumnElement[bool]] = []
        for i, (sort, value) in enumerate(zip(self.order, key, strict=True)):
            beyond = sort.beyond(value)
            if beyond is None:
                continue
            ties = [s.equals(v) for s, v in zip(self.order[:i], key[:i], strict=True)]
            terms.append(and_(*ties, beyond) if ties else beyond)
        return or_(*terms) if terms else false()

    def _uniform(self) -> bool:
        return not any(s.nullable for s in self.order) and (
            len({s.descending for s in self.order}) == 1
        )

    # --- the page ----------------------------------------------------------- #

    def paginate[T](self, rows: Sequence[T], limit: int) -> tuple[list[T], str | None]:
        """Split a ``limit + 1`` fetch into ``(page, next_cursor)``.

        The extra row exists only when another page does. ``next_cursor`` then encodes the last
        row KEPT, read from the raw row, and is None on the last page. A negative ``limit`` is a
        server bug, and raises ``ValueError``.
        """
        if limit < 0:
            raise ValueError(f"{self!r}: limit must not be negative, got {limit}")
        page = list(rows[:limit])
        if len(rows) > limit and page:
            return page, self.encode(self.key(page[-1]))
        return page, None

    def key(self, row: Any) -> tuple[Any, ...]:
        """A fetched row's position in this order."""
        return tuple(sort.read(row) for sort in self.order)

    # --- the cursor --------------------------------------------------------- #

    def encode(self, key: Sequence[Any]) -> str:
        """``base64url(JSON [listing, *key])``.

        A key the cursor could not carry back is a server bug, and raises ``ValueError``: a
        naive timestamp, a non-finite ``numeric``, or a float for a ``numeric`` column.
        """
        if len(key) != len(self.order):
            raise ValueError(f"{self!r} keys have {len(self.order)} parts, got {len(key)}")
        parts: list[Any] = [self.name]
        for sort, value in zip(self.order, key, strict=True):
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    raise ValueError(f"{self!r}: a naive timestamp cannot name a position")
                value = value.isoformat()
            elif isinstance(value, Decimal):
                if not value.is_finite():
                    raise ValueError(f"{self!r}: a non-finite numeric cannot name a position")
                # A JSON number is a float to most readers; the digits are the position.
                value = str(value)
            elif isinstance(value, uuid.UUID):
                value = str(value)
            elif isinstance(value, float) and sort.kind == "num":
                raise ValueError(f"{self!r}: read a numeric key as Decimal, not float")
            parts.append(value)
        raw = json.dumps(parts, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def decode(self, cursor: str) -> tuple[Any, ...]:
        """The position ``cursor`` names in this listing, or :class:`InvalidCursor`."""
        if not isinstance(cursor, str) or len(cursor) > _MAX_CURSOR_CHARS:
            raise InvalidCursor
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            parts = json.loads(base64.urlsafe_b64decode(padded.encode()))
        # A cursor of deeply nested arrays exhausts the parser's recursion limit.
        except (binascii.Error, UnicodeError, ValueError, RecursionError):
            raise InvalidCursor from None
        if not isinstance(parts, list) or len(parts) != len(self.order) + 1:
            raise InvalidCursor
        if parts[0] != self.name:
            raise InvalidCursor
        try:
            return tuple(_parse(v, s) for v, s in zip(parts[1:], self.order, strict=True))
        except (TypeError, ValueError):
            raise InvalidCursor from None


def _base(type_: TypeEngine[Any]) -> TypeEngine[Any]:
    """The type a column stores: a TypeDecorator, nested or not, is read as its underlying type.

    Every rule about what a key may hold (its kind, timezone, width, precision, enum labels)
    applies to what the database stores. The cursor carries the value a fetched row holds, and
    binds it back through the decorator, so the decorator's Python value must be of the same
    kind as its stored type, as a decorator that normalizes timestamps to UTC is.
    """
    while isinstance(type_, TypeDecorator):
        type_ = type_.impl_instance
    return type_


def _kind(type_: TypeEngine[Any]) -> _Kind:
    if isinstance(type_, DateTime):
        if not type_.timezone:
            raise TypeError("a timestamp sort column must be timezone-aware (timestamptz)")
        return "ts"
    if isinstance(type_, Integer):
        return "int"
    if isinstance(type_, Enum):
        # A Python enum class maps a member to a stored label the cursor would have to carry
        # back through the class; a string enum's labels are the values themselves.
        if type_.enum_class is not None:
            raise TypeError(
                "a cursor cannot carry a Python-enum sort key: declare the column with string "
                "labels, Enum('a', 'b', name=...)"
            )
        return "str"
    if isinstance(type_, String):
        return "str"
    if isinstance(type_, Uuid):
        return "uuid"
    if isinstance(type_, Numeric) and not isinstance(type_, Float):
        if not type_.asdecimal:
            raise TypeError(
                "a numeric sort column must be read as Decimal: asdecimal=False returns floats, "
                "whose cursor value is not the value the row holds"
            )
        return "num"
    raise TypeError(f"a cursor cannot carry a {type_!r} sort key")


def _row_key(expr: ColumnElement[Any]) -> str | None:
    if isinstance(expr, ColumnClause | Label):
        return expr.key
    return None


def _parse(value: Any, sort: Sort) -> Any:
    if value is None:
        if not sort.nullable:
            raise ValueError("NULL in a non-null position")
        return None
    if sort.kind == "ts":
        if not isinstance(value, str):
            raise TypeError("ts")
        parsed = datetime.fromisoformat(value)
        # A naive timestamp compared against timestamptz would be read in the session's zone.
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        # The driver binds the instant in UTC, and an offset at either end of the calendar
        # puts that instant outside the range a datetime holds.
        try:
            return parsed.astimezone(UTC)
        except OverflowError:
            raise ValueError("timestamp out of range") from None
    if sort.kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("int")
        # The driver binds an int at its column type's width and refuses one that does not fit.
        type_ = _base(sort.expr.type)
        bits = next((b for t, b in _INT_BITS.items() if isinstance(type_, t)), 32)
        if not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
            raise ValueError("int out of range")
        return value
    if sort.kind == "num":
        if not isinstance(value, str):
            raise TypeError("num")
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise ValueError("not a decimal") from None
        # NaN and the infinities parse, and neither is a position a numeric row holds.
        if not number.is_finite():
            raise ValueError("non-finite decimal")
        _check_numeric(number, _base(sort.expr.type))
        return number
    if sort.kind == "uuid":
        if not isinstance(value, str):
            raise TypeError("uuid")
        parsed_uuid = uuid.UUID(value)
        # Only the canonical spelling this library issues names a position.
        if str(parsed_uuid) != value:
            raise ValueError("non-canonical uuid")
        return parsed_uuid if getattr(_base(sort.expr.type), "as_uuid", True) else value
    if not isinstance(value, str):
        raise TypeError("str")
    # Postgres text holds only UTF-8 with no NUL. Anything else the driver refuses at execute;
    # an unencodable string (a lone surrogate) raises UnicodeEncodeError, a ValueError.
    if "\x00" in value:
        raise ValueError("NUL in a string key")
    value.encode("utf-8")
    type_ = _base(sort.expr.type)
    if isinstance(type_, Enum) and value not in type_.enums:
        raise ValueError("not a label of the enum")
    return value


def _check_numeric(number: Decimal, type_: TypeEngine[Any]) -> None:
    """Refuse a ``numeric`` the driver would bind as another value, or the column cannot hold.

    Pure digit arithmetic on the decimal's tuple, never a context operation, so a value with an
    enormous exponent is measured without being rounded first.
    """
    _, digits, exponent = number.as_tuple()
    if not isinstance(exponent, int):  # NaN or an infinity: refused by the caller already
        raise ValueError("non-finite decimal")
    zero = not any(digits)
    if exponent < _NUMERIC_MIN_EXPONENT:
        raise ValueError("numeric scale out of range")
    if not zero and number.adjusted() > _NUMERIC_MAX_ADJUSTED:
        raise ValueError("numeric out of range")
    precision = getattr(type_, "precision", None)
    if precision is None or zero:
        return
    # A declared NUMERIC(p, s) rounds a value to s decimal places (to 10^-s, so a negative s
    # rounds left of the point) and holds it only if its absolute value is below 10^(p - s).
    # Postgres allows a negative s and an s above p, so neither measure is clamped. A cursor
    # whose last significant digit is finer than 10^-s is not a value any row holds, and the
    # bind's cast would round it; one at or above 10^(p - s) would overflow.
    scale = getattr(type_, "scale", None) or 0
    trailing = 0
    while trailing < len(digits) - 1 and digits[len(digits) - 1 - trailing] == 0:
        trailing += 1
    if exponent + trailing < -scale or number.adjusted() >= precision - scale:
        raise ValueError("numeric outside the column's precision")


# Bind widths for the integer types narrower or wider than Postgres ``integer``, matched by
# ``isinstance`` so the uppercase ``SMALLINT`` and ``BIGINT`` subclasses resolve too.
_INT_BITS: dict[type, int] = {SmallInteger: 16, BigInteger: 64}
