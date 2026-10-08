# Documentation best practices

Rules for READMEs, docs pages, comments, API docs, and design docs. Condensed
from [Google's Documentation Best Practices](https://google.github.io/styleguide/docguide/best_practices.html)
(CC-BY-3.0), with the original wording kept where it is already direct.

"Say what you mean, simply and directly." — Brian Kernighan

## Minimum viable documentation

A small set of fresh and accurate docs is better than a large assembly of
"documentation" in various states of disrepair.

Write short and useful documents. Cut out everything unnecessary, including
out-of-date, incorrect, or redundant information. Keep improving every doc to
suit changing needs. **Docs work best when they are alive but frequently
trimmed, like a bonsai tree.**

When editing: if a section no longer matches the code, either fix it or flag it
for deletion. Do not leave it as decoration.

## Update docs with code

**Change the documentation in the same commit as the code change.** This keeps
docs fresh, and it tells the reviewer what you're doing.

Docstrings, header files, `README.md`, and any other affected docs get updated
alongside the change.

## Delete dead documentation

Dead docs misinform, they slow people down, and they set a precedent for leaving
messes behind. If the docs are in bad shape:

- Take it slow; doc health is a gradual accumulation.
- First delete what you're certain is wrong; ignore what's unclear.
- Scan every doc and make one simple decision: keep or delete?
- Default to delete. Stragglers can always be recovered from version control.
- Iterate.

## Prefer the good over the perfect

Documentation is an art. There is no perfect document, only proven methods and
prudent guidelines. Ship the useful doc.

## Documentation is the story of your code

It's easy to write something a computer understands; it's much harder to write
something both a human and a computer understand. **Write for humans first,
computers second.**

Put each piece of information at the level where it belongs:

1. **Meaningful names.** Good naming carries information that would otherwise
   need a comment. This applies to local variables, classes, files, and
   directories.
2. **Inline comments.** Their job is what the code cannot say itself, mainly
   *why* the code is there.
3. **Method API documentation.** The contract of how the code must behave, for
   future programmers who will use and modify it: arguments, return values,
   gotchas, restrictions, exceptions and errors. Any documented behavior should
   usually have a test verifying it. Not *why* — that goes in inline comments.
   Think in practical terms: "This is a hammer. You use it to pound nails."
4. **Class / module API documentation.** A brief overview of what the class or
   file does, plus a few short usage examples. Examples matter most when there
   are several distinct ways to use it. Always list the simplest use case first.
5. **README.md.** Orients a new user to the directory and points to more
   detailed guides. It should answer: What is this directory intended to hold?
   Which files should the developer look at first, and are some of them an API?
   Who maintains this and where can I learn more?
6. **docs directory.** How to get started with the API, library, or tool; how to
   run its tests; how to debug its output; how to release the binary.
7. **Design docs and PRDs.** They discuss a proposed implementation to collect
   feedback. Once the code is implemented, they are archives of those decisions,
   not half-correct docs.
8. **Other external docs.** If docs live outside the repo (Drive, a wiki, a
   site), link to them prominently from the project's `README.md`.

When editing, ask whether a passage is at the right level. A "why" buried in an
API doc, or a design rationale duplicated in five docstrings, belongs somewhere
else.

## Duplication is evil

Do not write your own guide to a common technology or process. Link to it
instead. If the guide doesn't exist or is badly out of date, submit your updates
to it or create a package-level `README.md`. **Take ownership and don't be
shy**: other teams will usually welcome the contribution.
