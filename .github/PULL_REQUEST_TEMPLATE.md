# Pull Request — copy this when opening a PR

## What does this PR do?

<!-- One-paragraph summary of the change. -->

Closes #<issue-number-if-any>

---

## Type of change

Delete options that don't apply:

- [ ] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] 💡 New reflection checker (E028+) — please also describe the real case it catches below
- [ ] ⚡ Performance improvement
- [ ] 📝 Documentation / comment
- [ ] 🧪 Test addition
- [ ] 🔧 Build / CI / tooling
- [ ] ♻️ Refactor (no functional change)

---

## Real-world motivation

If this PR adds a checker, fixes a bug, or improves a feature — **what real
trading case motivated the change?** This helps us prioritize and write a
useful changelog entry.

<!-- e.g., "Lost on EUR/USD long 2026-09-22 because LLM skipped the DXY correlation
check. E015 would have flagged it." -->

---

## Testing

- [ ] I added unit tests (`backend/tests/`)
- [ ] I ran `pytest` locally — all pass
- [ ] I ran `pre-commit run --all-files` — clean
- [ ] I have tested manually with a fresh SQLite DB

**Test command output (paste last 5 lines):**

```
$ pytest
========================= X passed in Y.YYs =========================
```

---

## Checklist

- [ ] My code follows the project's style (`pre-commit`)
- [ ] I have commented complex / non-obvious parts
- [ ] I have updated relevant docs (`docs/` if architecture changed)
- [ ] I have NOT committed `.db`, `.env`, `*.log`, or any sensitive data
- [ ] I have squashed my commits into logical units
- [ ] I have read [CONTRIBUTING.md](../CONTRIBUTING.md)

---

## Screenshots / demos (if applicable)

<!-- Drag-drop images, or paste markdown image syntax. -->

---

<sub>Thanks for contributing to a more transparent quant future.</sub>