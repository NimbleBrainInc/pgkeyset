# pgkeyset

Strict keyset pagination for async SQLAlchemy on Postgres, for APIs and agent tools.

- **Orders that lose rows are refused when you declare them.** A nullable sort column must
  say where its NULLs go, a timestamp must carry a zone, a float cannot be a key, and the
  last column (your unique id) cannot be NULL. These fail at import, not on page 40.
- **Cursors know where they came from.** Each cursor is tagged with the listing that issued
  it, so a cursor passed to the wrong list is refused instead of read as a position in an
  order it was never taken from.
- **A bad cursor has one way out.** Anything that is not a position in the listing raises
  `InvalidCursor`, whose text is always the same safe sentence, never a parse error. Your
  API answers "call again without `cursor`".
- **A walk test that proves it.** `pgkeyset.testing.walk` follows `next_cursor` to the end
  while your test inserts and deletes rows between pages, and fails on any row met twice or
  never met.

It is for Python services on async SQLAlchemy 2 that page through Postgres: HTTP APIs, and
MCP servers whose tools an agent calls page after page while it changes the same rows.

```sh
pip install pgkeyset        # or: uv add pgkeyset
```

Python 3.12+, SQLAlchemy 2, Postgres. Tested with asyncpg on Postgres 14 and 17.

## Why keyset, never offset

A caller works a list while it reads it: an agent completes a task, a reviewer rejects a
draft. Each removal shifts every later row up by one, so `OFFSET n` skips a row nobody saw.
A keyset position is a row's own sort key, and it does not move when other rows leave. It is
also an index range scan, where an offset reads and throws away every earlier row.

The price is that you can only go forward from a position, and the order must be total: two
rows must never share a position, or a page boundary inside the tie drops or repeats one.
Rows written in one transaction share `now()`, so a timestamp alone is never a total order.
Every listing ends with the row's unique id.

## Usage

Declare each paged read once, next to the query that runs it:

```python
from pgkeyset import InvalidCursor, Listing, Sort

CONTACTS = Listing("contacts", Sort.desc(contact.c.created_at), Sort.desc(contact.c.id))

async def list_contacts(conn, *, cursor: str | None, limit: int):
    stmt = CONTACTS.apply(select(contact).where(...), cursor)   # ORDER BY + strictly-after
    rows = (await conn.execute(stmt.limit(limit + 1))).mappings().all()
    page, next_cursor = CONTACTS.paginate(rows, limit)          # encodes the last row kept
    return [Contact.from_row(r) for r in page], next_cursor
```

Fetch one row more than the page. If it exists there is a next page, and `next_cursor`
encodes the last row kept; on the last page it is `None`. An absent or empty `cursor` is the
first page. Pass the same filters with every page; the cursor carries a position, not a query.

`paginate` reads the key from the raw row, before you map it to a model, so a row your code
later skips as malformed still advances the walk.

### Declaring the order

`Listing(name, *sorts)` takes the sort columns from first to last.

| Rule | Why | Refused with |
|---|---|---|
| The last column is the row's unique, non-NULL id. | Two rows sharing a position split across a page boundary. | `ValueError` if it is declared nullable |
| A nullable column says where its NULLs go: `Sort.asc(col, nulls="first")` or `"last"`. | A comparison against NULL is NULL, so a NULL band must be named on both sides of the cursor. | `ValueError` for a table column declared `nullable` without it |
| A timestamp column is `timestamptz` (`DateTime(timezone=True)`). | A naive value is read in the session's zone, so the same cursor means different instants. | `TypeError` |
| No float columns. | A float's cursor value is not the value the row holds. Use `numeric`. | `TypeError` |
| A computed expression passes `value=` to read its key from a row. | The cursor needs the value the row sorted by. | `ValueError` |

The library checks that the last column cannot be NULL. It does not check that it is
unique: declare your primary key there.

A computed column, such as a status band that puts drafts first:

```python
DRAFT_FIRST = case((message.c.status == "draft", 0), else_=1)
REVIEW = Listing(
    "review",
    Sort.asc(DRAFT_FIRST, value=lambda row: 0 if row["status"] == "draft" else 1),
    Sort.desc(message.c.drafted_at),
    Sort.desc(message.c.id),
)
```

A listing whose direction is a request argument declares one `Listing` per direction, each
with its own name, so a cursor from one direction is refused by the other.

### The SQL

When every column runs one direction and none is nullable, "strictly after" is one row
comparison, which Postgres reads as an index condition:

```sql
WHERE (created_at, id) < ($1::TIMESTAMP WITH TIME ZONE, $2::VARCHAR)
ORDER BY created_at DESC, id DESC
```

Otherwise it is the explicit expansion: after on the first column, or equal on it and after
on the next, and so on, with each NULL band named. Bound values carry their column's type,
so asyncpg gets a `datetime` for a `timestamptz`.

## The cursor

`base64url(JSON [listing, *key])`, unpadded. Timestamps are ISO-8601 with their zone,
`numeric` values are decimal strings (a JSON number would round-trip through a float), and
ints are checked against their column's width.

It is opaque to callers and not signed, because it grants nothing. It names a position, not a
right and not a filter: the read it resumes runs under the same authorization as any other,
so a cursor replayed by someone else returns only what they could already read.

### A bad cursor

`decode` (and `apply`, which calls it) raises `InvalidCursor` for any cursor that is not a
position in that listing: malformed, truncated, from another listing, a naive timestamp, a
value out of its column's range. It is a `ValueError`, and its text is always
`INVALID_CURSOR_MESSAGE`:

> The cursor is not a position in this listing.

Map it to your bad-input error and tell the caller the way out: call again without `cursor`.
The parse failure itself never reaches the caller.

```python
try:
    contacts, next_cursor = await list_contacts(conn, cursor=cursor, limit=limit)
except InvalidCursor as exc:
    raise BadRequest(str(exc), next_step="Call again without cursor to start from the top.")
```

### Stable across versions

A caller holds a cursor across your deploys. The encoding and the SQL a cursor resumes with
are frozen in this repository's golden tests, and a change to either is a breaking change.

## What a walk guarantees

Every row that existed when the walk began, and still matches, comes back exactly once.

- A row written after the walk began sorts wherever its key puts it. In a newest-first walk
  that is ahead of the cursor, so the walk does not meet it. Tell the caller on the last page
  to start again without `cursor` to see new rows.
- A row whose sort key changes mid-walk moves in the order, and can be met twice or not at
  all. Say so on any listing whose key can change, such as a status band.

### The walk test

Test every paged read the same way: walk more than two pages to the end, insert and remove
rows between pages, and assert no gap and no duplicate. Seed the rows in one transaction so
the id tiebreak is what orders them.

```python
from pgkeyset.testing import walk

walked = await walk(
    lambda cursor: list_contacts(conn, cursor=cursor, limit=3),
    ident=lambda c: c.id,
    expected=seeded_ids,                 # every row that must be met
    between_pages=insert_one_and_remove_one,
)
```

`walk` fails on a row met twice, a row in `expected` never met, a row met that is not in
`expected`, an empty page that still hands out a cursor, or a walk that does not end.

## Compared with sqlakeyset

[sqlakeyset](https://github.com/djrobstep/sqlakeyset) is the established keyset library for
SQLAlchemy, and it does more: ORM and Core, sync and async, paging backwards, and PostgreSQL,
MariaDB/MySQL and SQLite. If you need any of those, use it.

pgkeyset is narrower and stricter:

- sqlakeyset documents that rows with a NULL in their keyset are left out of the results and
  asks you to keep sort columns `NOT NULL`. pgkeyset pages through NULLs, and refuses a
  nullable column that does not say where they sort.
- sqlakeyset serializes floats into its bookmarks. pgkeyset refuses a float key and carries
  `numeric` values as exact decimal strings.
- A pgkeyset cursor names its listing, and is refused anywhere else, with one fixed message.
- pgkeyset ships the walk test.

It pages forward only, targets Postgres only, and works on SQLAlchemy Core `select()`
statements. It builds statements and never runs them; it is tested on async engines with
asyncpg.

## API

| Name | What it is |
|---|---|
| `Listing(name, *sorts)` | One paged read: its cursor tag and its total order. |
| `Listing.apply(stmt, cursor)` | `stmt` ordered, and started strictly after `cursor` when one is given. |
| `Listing.paginate(rows, limit)` | Split a `limit + 1` fetch into `(page, next_cursor)`. |
| `Listing.after(key)`, `.key(row)`, `.encode(key)`, `.decode(cursor)` | The predicate, a row's key, and the codec, for custom flows such as a change feed's mark. |
| `Sort.asc(expr, *, nulls=None, value=None)`, `Sort.desc(...)` | One column of the order. |
| `InvalidCursor`, `INVALID_CURSOR_MESSAGE` | The refusal for a cursor that is not a position, and its fixed text. |
| `pgkeyset.testing.walk(fetch, *, ident, expected=None, between_pages=None, max_pages=1000)` | The walk test. |

## Development

```sh
docker run -d --rm -p 127.0.0.1:5432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-alpine
make verify     # ruff format check, ruff lint, ty, pytest
```

The walk suites need a Postgres superuser at `PGKEYSET_TEST_URL` (default
`postgresql+asyncpg://postgres@127.0.0.1:5432/postgres`); they create and drop a throwaway
database. Without one they skip locally and fail under CI. The codec, declaration and golden
suites need no database.

`tests/golden/` holds frozen cursors and SQL. They are not regenerated by the tests. If one
fails, a cursor someone holds would decode differently or resume somewhere else.

## Releasing

1. In a pull request, set `version` in `pyproject.toml` and date its `CHANGELOG.md` entry.
2. After it merges, tag the merge commit `v<version>` and push the tag.
3. `release.yml` builds once, installs the built wheel into a clean environment on each
   supported Python, runs the test suite the sdist ships against a real Postgres, and only then
   publishes those same files to PyPI. A version can never be uploaded twice, so a red run
   publishes nothing; fix it and release the next patch version.

Publishing uses trusted publishing (OIDC); there is no API token to rotate or leak.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Security reports: [SECURITY.md](SECURITY.md).
