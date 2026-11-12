# Security policy

## Reporting a vulnerability

Report it privately through GitHub: **Security → Report a vulnerability** on this repository.
Please do not open a public issue for it. We aim to acknowledge a report within three business
days and to agree a disclosure date with you once we understand the issue.

## Supported versions

The latest minor release gets security fixes. Before 1.0, that is the only supported line.

## What counts

A cursor is untrusted input: callers can send anything. pgkeyset's guarantee is that **a
cursor can only ever be a position in its own listing, or refused**. A vulnerability includes:

- a cursor that decodes to something other than a position in the listing it was passed to,
  or that changes the SQL beyond the bound values of the strictly-after predicate;
- a cursor that raises anything but `InvalidCursor` from `decode` or `apply`, or whose
  refusal carries text other than `INVALID_CURSOR_MESSAGE`;
- a declared listing whose walk skips or repeats a row that existed when the walk began and
  still matches, when the declaration follows the README's rules.

Out of scope, because the guarantee does not claim them:

- Authorization. A cursor is not signed and grants nothing; what a resumed read may see is
  decided by your query's filters and your database's access rules, as for any other read.
- A last sort column that is not unique. The library refuses one that can be NULL but does
  not check uniqueness; the README says to declare the primary key there.
- A row whose sort key changes during a walk, which the README documents.
