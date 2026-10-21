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
mypy                                   # static types over src/oska, configured in pyproject.toml
oska validate                          # the catalog against its schemas
oska conform                           # the scenario suite and the level reached
```

`ruff format` is not used here; keep a change to the lines it needs.

The full suite (`pytest -q`) runs in CI on every push and pull request, together
with the catalog validation and conformance runs for every reference domain.
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

## Release

A release is a version tag. Pushing a tag that matches `v*.*.*` runs
`.github/workflows/release.yml`: it builds the distributions, checks them,
runs the newcomer path against the built wheel, publishes to PyPI, and creates
the GitHub release with the matching `CHANGELOG.md` section as its notes. The
workflow stores no secret; PyPI trusts it through OpenID Connect.

Once, before the first release:

1. On PyPI, add a trusted publisher for the project `oska`: this repository,
   workflow `release.yml`, environment `pypi`. For a project that does not exist
   yet, add it as a pending publisher.
2. In the repository settings, create the environment `pypi` and add yourself
   as a required reviewer, so every publish waits for an approval.

For each release:

1. Fold `[Unreleased]` in `CHANGELOG.md` into a dated section, for example
   `## [0.2.0] - 2026-11-03`, and leave an empty `[Unreleased]` above it.
2. Set the same version in `pyproject.toml`. The workflow fails if the tag and
   the version differ, or if the changelog has no section for the version.
3. Commit, then tag and push the tag: `git tag v0.2.0 && git push origin v0.2.0`.
4. Open the run in the Actions tab and approve the `pypi` environment when the
   publish job asks for it.
