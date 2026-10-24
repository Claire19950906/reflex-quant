## What

<!-- One paragraph. What does this PR do and why? -->

## Why

<!-- Link the issue. If there isn't one, explain the motivation. -->

Closes #

## How

<!-- Architecture sketch, key files, schema changes. -->

## Risk & rollback

<!-- What could break? How do we revert? -->

## Checklist

- [ ] I read [`CONTRIBUTING.md`](../../blob/main/CONTRIBUTING.md)
- [ ] I added / updated tests in `backend/tests/` (189+ must pass)
- [ ] I ran `pytest -q` locally — green
- [ ] I ran `ruff check backend/` — clean
- [ ] No new deps without justification
- [ ] If schema changed, `db.py` has the migration; `init_db.py` covers the new column
- [ ] If frontend changed, I tested on `:3000` dev server
- [ ] I read [`SECURITY.md`](../../blob/main/SECURITY.md) — no secrets in this diff

## Screenshots / logs (if visual)

<!-- Drag a screenshot or paste console output here. -->
