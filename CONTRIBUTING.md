# Contributing to Reflex Quant 🪞

Thanks for your interest in making quant trading more honest.

This document explains how to contribute. **Please read it before opening an
issue or pull request.**

---

## 🪞 Project values

Reflex Quant exists because most quant systems are black boxes. We believe:

1. **Transparency over opacity.** Reflection logs should be auditable.
2. **Self-improvement over static deployment.** The system should learn from
   its own mistakes.
3. **Community over gatekeeping.** A self-aware quant system improves faster
   when many eyes read it.

When in doubt about a contribution, ask: *does this make the system more
honest about itself?*

---

## 🐛 Found a bug?

1. **Search existing issues** — your bug may already be tracked.
2. **Use the bug report template** when you open a new one.
3. Include:
   - OS, Python version, Node version
   - Steps to reproduce (minimal)
   - Expected vs actual output
   - Whether it happens with a fresh DB or after warm-up

**Security issues** must NOT be opened as public issues. Email
**security@reflex-quant.ai** instead. See [SECURITY.md](./SECURITY.md).

---

## 💡 Want to add a reflection checker (E028+)?

We love this. Reflection checkers are the heart of the system.

Process:

1. Open an issue with tag `checker-proposal`
2. Describe:
   - Which reflection layer it belongs to (event_logic / causal / sample_integrity / reasoning_logic / cross_asset / meta / confidence)
   - What error pattern it catches
   - What evidence you have (real cases from your trading)
3. We discuss the design in the issue
4. After approval, we collaborate on the implementation
5. The checker becomes **E028** (or whatever the next number is) in the
   reflection engine
6. You're credited in [docs/CONTRIBUTORS.md](./docs/CONTRIBUTORS.md)

> 💡 Many of our v225 checkers came from real trade failures. The most
> valuable contributions are based on **actual losses you've seen**, not
> theoretical improvements.

---

## 🧪 Want to add tests?

Always welcome. Add to `backend/tests/` and ensure `pytest` passes locally.

Tests are required for:
- New reflection checkers
- Any DB schema change
- Any LLM prompt change

---

## 📝 Want to improve docs?

PRs to `docs/` are welcome and reviewed within 48 hours.

Specifically useful:
- More case studies in `examples/`
- New entries in [docs/FAQ.md](./docs/FAQ.md)
- Translations (zh-CN, ja, ko)
- Improvement of `README.md` clarity

---

## 🚫 What we won't accept

To keep the project honest and maintainable, we will not merge:

- ❌ **Trading strategy secrets** that violate our AGPL-3.0 license
  (commercial users must use the Commercial License instead)
- ❌ **Closed-source sub-modules** — the reflection engine is fully open
- ❌ **Telemetry/tracking code** — Reflex Quant is local-first
- ❌ **Hard-coded proprietary model weights** that can't be regenerated

If your contribution requires a closed component, see the Commercial License
path ([LICENSE-COMMERCIAL.md](./LICENSE-COMMERCIAL.md)).

---

## 🔄 Pull request process

1. Fork the repo
2. Create a branch: `git checkout -b feat/e028-cross-temporal`
3. Make your changes
4. Add tests
5. Run `pytest` locally — must pass
6. Run `pre-commit run --all-files` — must pass
7. Push your branch
8. Open a pull request using the [PR template](./.github/PULL_REQUEST_TEMPLATE.md)
9. Wait for review (typically 3-5 business days)
10. Address feedback, push updates
11. After 2 approvals + CI green → merge

We commit to responding within 5 business days. If we don't, ping us.

---

## 🌍 Code of conduct

This project follows the [Contributor Covenant](./CODE_OF_CONDUCT.md). Be
excellent to each other. We take violations seriously.

---

## 📬 Questions?

- **For bug / feature / question** → open an issue
- **For security** → security@reflex-quant.ai (encrypted preferred)
- **For commercial licensing** → z2132743607@163.com
- **For everything else** → Discord (link in [README.md](./README.md))

---

<sub>Last updated: 2026-10-06</sub>