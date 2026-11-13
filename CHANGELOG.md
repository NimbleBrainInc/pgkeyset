# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change the API.
The cursor encoding and the SQL a cursor resumes with are pinned by golden tests; a change to
either is a breaking change in any version.

## [0.2.0] - 2026-11-13

Cursors issued by 0.1.x decode and resume exactly as before; the golden cursor and SQL files are
unchanged.

### Added

- A sort column whose type is a SQLAlchemy `TypeDecorator`, nested or not, is classified by the
  type it stores: its kind, the timezone-aware timestamp rule, integer width, numeric precision
  and scale, enum labels and uuid form all follow the stored type. Binds stay typed with the
  decorator, so its processing runs on a cursor value as on a written one. A decorator over a
  naive `DateTime` is still refused. The decorator's Python value must be of the same kind as
  its stored type.

## [0.1.1] - 2026-11-13

Cursors issued by 0.1.0 decode and resume exactly as before; the golden cursor and SQL files
only gained cases.

### Added

- `uuid` sort keys (`Uuid`, `postgresql.UUID`): carried in canonical form, decoded to
  `uuid.UUID` (or `str` for `Uuid(as_uuid=False)`). A uuid primary key can be the tiebreak.
- String `Enum` sort keys: a cursor value must be one of the enum's labels.
- `pgkeyset.__version__`.

### Fixed

- A `numeric` cursor value beyond what asyncpg binds exactly (more than 131072 digits before
  the point, or more than 16383 after it) raised a driver error or bound as 0 and resumed at
  the wrong position. It is now refused with `InvalidCursor`, as is a value beyond a declared
  `numeric(p, s)`, which the bind's cast would have rounded or overflowed.
- A cursor that is not a string, or is longer than 1 MiB, raises `InvalidCursor`.
- `walk` checks with explicit raises, so its failures hold under `python -O`.

### Changed

- Refused at declaration: an enum column backed by a Python enum class, and a `numeric`
  column with `asdecimal=False`. Neither could page correctly.
- `encode` raises `ValueError` for a non-finite `Decimal` or a float `numeric` key, and
  `paginate` for a negative `limit`: server bugs, like a naive timestamp.
- Requires SQLAlchemy 2.0.37 or later; 2.0.36 binds a `smallint` key as `integer`.
- Python 3.14 is supported and tested.

## [0.1.0] - 2026-11-12

First release.

- `Listing`: one paged read's name and total order. `apply` adds the ORDER BY and the
  strictly-after predicate (a row comparison when the order is uniform, the explicit
  expansion with named NULL bands otherwise), and refuses a statement that already has an
  ORDER BY. `paginate` splits a `limit + 1` fetch into the page and its next cursor.
- `Sort`: one column of the order, with its direction, its NULL placement, and how to read a
  computed column's value from a row. Nullable columns without a placement, naive timestamps,
  float keys, and a nullable last column are refused at declaration.
- The cursor: `base64url(JSON [listing, *key])`, with timestamps as ISO-8601 with a zone and
  `numeric` values as decimal strings.
- `InvalidCursor`, a `ValueError` whose text is always `INVALID_CURSOR_MESSAGE`, for any
  cursor that is not a position in the listing, including one nested deeply enough to exhaust
  the JSON parser's recursion limit.
- `pgkeyset.testing.walk`: walks a listing to its end, failing on a row met twice or never met.

[0.2.0]: https://github.com/NimbleBrainInc/pgkeyset/releases/tag/v0.2.0
[0.1.1]: https://github.com/NimbleBrainInc/pgkeyset/releases/tag/v0.1.1
[0.1.0]: https://github.com/NimbleBrainInc/pgkeyset/releases/tag/v0.1.0
