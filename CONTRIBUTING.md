# Contributing

## Setup

Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv venv --seed
source .venv/bin/activate
uv pip install -e '.[dev,api]'
```

Add the `postgres` extra (`'.[dev,api,postgres]'`) to work on the relational
backend. Its tests run against a real server when `OSKA_TEST_POSTGRES_URL`
names one, and are skipped otherwise.

## Checks

Run what your change touches, not everything, while you work:

```sh
pytest tests/unit/test_kit.py          # the test files that cover what you changed
ruff check src tests                   # lint, line length 100
oska validate                          # the catalog against its schemas
oska conform                           # the scenario suite and the level reached
```

`ruff format` is not used here; keep a change to the lines it needs.

The full suite (`pytest -q`) runs in CI on every push and pull request, together
with the catalog validation and conformance runs for both reference domains.
Run it locally before a release or after a change to shared code.

A behavior change comes with a test that fails before the change and passes
after it.

## Documentation

Every change to behavior updates the documentation and the spec in the same
change: the page under `docs/`, the matching layer under `spec/` and its
numbered requirements, `README.md` where it is mentioned, and the conformance
checklist if a requirement is added or reworded. The core stays free of any
one domain: a domain belongs in a pack or under `examples/`.

## Commits

One logical change per commit. The subject is a sentence in the present tense
that says what is true after the change, with no prefix and no trailing period,
for example `The relational backend, proven against a live database`. The body
is prose: what the change does, why, and what it replaces. Name the spec
requirements it adds or changes. When a change closes a hole, say that a test
failed before the fix.

## Pull requests

Describe the change and how you checked it. The pull request template lists
what a reviewer looks for.
