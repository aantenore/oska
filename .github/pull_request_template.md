## What changes and why

## Checklist

- [ ] Tests cover the change, and a new behavior has a test that fails without it
- [ ] The impacted tests pass, and `ruff check src tests` is clean
- [ ] Docs and spec are updated in this change (`docs/`, `spec/`, `README.md`), or nothing needed updating, and why:
- [ ] `oska conform` still reaches the same level, and `oska validate` passes
- [ ] No domain-specific name, client or secret in the core, in a test or in a document
