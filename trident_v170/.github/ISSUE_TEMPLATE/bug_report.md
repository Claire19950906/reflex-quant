---
name: 🐞 Bug report
about: Something broke — signal stuck, reflection engine wrong, UI broken
title: "[bug] "
labels: ["bug", "needs-triage"]
assignees: []
---

## What happened

<!-- A short, factual description. Skip the speculation; we'll figure out why together. -->

## Steps to reproduce

```bash
# Exact commands, config, or UI clicks
```

## Expected behavior

<!-- What you expected to happen. -->

## Actual behavior

<!-- What actually happened. Paste logs / screenshots / API responses. -->

## Environment

- **Version / commit:** v225 · commit `xxxxxxx` (run `git rev-parse HEAD`)
- **OS:** macOS 14 / Ubuntu 22.04 / Windows 11
- **Python:** 3.11.x
- **Deployment:** local dev / launchd / systemd / Docker
- **Branch:** main / feat-xxx

## Logs

```
# paste relevant tail here
```

## Severity

- [ ] 🔴 blocker (live trading / data loss)
- [ ] 🟠 serious (wrong reflection verdict, silent error)
- [ ] 🟡 minor (cosmetic, dev-only)

## Checklist

- [ ] I searched [existing issues](../../issues) and didn't find a duplicate
- [ ] I can reproduce this on a fresh `git clone`
- [ ] I attached relevant logs (no secrets — see SECURITY.md)
