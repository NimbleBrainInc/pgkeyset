"""The public names and call shapes, pinned so a change to them is a deliberate one.

Callers import ``Listing``, ``Sort``, ``InvalidCursor`` and ``INVALID_CURSOR_MESSAGE``
from the package and ``walk`` from ``pgkeyset.testing``, and call the methods below. A
server maps ``InvalidCursor`` to its bad-input error by catching ``ValueError`` or the class
itself, and puts the message on the wire, so the hierarchy and the text are API too.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from importlib.metadata import version
from typing import Any

import pgkeyset
from pgkeyset import INVALID_CURSOR_MESSAGE, InvalidCursor, Listing, Sort
from pgkeyset.testing import walk


def _shape(fn: Callable[..., Any]) -> list[tuple[str, str, object]]:
    return [(p.name, p.kind.name, p.default) for p in inspect.signature(fn).parameters.values()]


def test_the_package_exports() -> None:
    assert sorted(pgkeyset.__all__) == [
        "INVALID_CURSOR_MESSAGE",
        "InvalidCursor",
        "Listing",
        "Sort",
    ]


def test_the_version_is_the_installed_distribution_version() -> None:
    assert pgkeyset.__version__ == version("pgkeyset")


def test_a_bad_cursor_is_a_value_error_with_fixed_text() -> None:
    assert INVALID_CURSOR_MESSAGE == "The cursor is not a position in this listing."
    assert issubclass(InvalidCursor, ValueError)
    assert str(InvalidCursor()) == INVALID_CURSOR_MESSAGE


def test_sort_call_shapes() -> None:
    for make in (Sort.asc, Sort.desc):
        assert _shape(make) == [
            ("expr", "POSITIONAL_OR_KEYWORD", inspect.Parameter.empty),
            ("nulls", "KEYWORD_ONLY", None),
            ("value", "KEYWORD_ONLY", None),
        ]


def test_listing_call_shapes() -> None:
    empty = inspect.Parameter.empty
    assert _shape(Listing) == [
        ("name", "POSITIONAL_OR_KEYWORD", empty),
        ("order", "VAR_POSITIONAL", empty),
    ]
    assert _shape(Listing.apply) == [
        ("self", "POSITIONAL_OR_KEYWORD", empty),
        ("stmt", "POSITIONAL_OR_KEYWORD", empty),
        ("cursor", "POSITIONAL_OR_KEYWORD", empty),
    ]
    assert _shape(Listing.paginate) == [
        ("self", "POSITIONAL_OR_KEYWORD", empty),
        ("rows", "POSITIONAL_OR_KEYWORD", empty),
        ("limit", "POSITIONAL_OR_KEYWORD", empty),
    ]
    for method in (Listing.after, Listing.encode):
        assert _shape(method) == [
            ("self", "POSITIONAL_OR_KEYWORD", empty),
            ("key", "POSITIONAL_OR_KEYWORD", empty),
        ]
    assert _shape(Listing.key) == [
        ("self", "POSITIONAL_OR_KEYWORD", empty),
        ("row", "POSITIONAL_OR_KEYWORD", empty),
    ]
    assert _shape(Listing.decode) == [
        ("self", "POSITIONAL_OR_KEYWORD", empty),
        ("cursor", "POSITIONAL_OR_KEYWORD", empty),
    ]


def test_walk_call_shape() -> None:
    empty = inspect.Parameter.empty
    assert _shape(walk) == [
        ("fetch", "POSITIONAL_OR_KEYWORD", empty),
        ("ident", "KEYWORD_ONLY", empty),
        ("expected", "KEYWORD_ONLY", None),
        ("between_pages", "KEYWORD_ONLY", None),
        ("max_pages", "KEYWORD_ONLY", 1000),
    ]
