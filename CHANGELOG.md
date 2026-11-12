# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change the API.
The cursor encoding and the SQL a cursor resumes with are pinned by golden tests; a change to
either is a breaking change in any version.

## [0.1.0] - 2026-11-12

First release.

- `Listing`: one paged read's name and total order. `apply` adds the ORDER BY and the
  strictly-after predicate (a row comparison when the order is uniform, the explicit
  expansion with named NULL bands otherwise), and `paginate` splits a `limit + 1` fetch into
  the page and its next cursor.
- `Sort`: one column of the order, with its direction, its NULL placement, and how to read a
  computed column's value from a row. Nullable columns without a placement, naive timestamps,
  float keys, and a nullable last column are refused at declaration.
- The cursor: `base64url(JSON [listing, *key])`, with timestamps as ISO-8601 with a zone and
  `numeric` values as decimal strings.
- `InvalidCursor`, a `ValueError` whose text is always `INVALID_CURSOR_MESSAGE`, for any
  cursor that is not a position in the listing.
- `pgkeyset.testing.walk`: walks a listing to its end, failing on a row met twice or never met.

[0.1.0]: https://github.com/NimbleBrainInc/pgkeyset/releases/tag/v0.1.0
