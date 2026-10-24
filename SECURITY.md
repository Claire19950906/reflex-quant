# Security Policy

## Reporting a vulnerability

We take security seriously. If you've found a vulnerability in Reflex Quant
— a code execution flaw, a privilege escalation, a data leak, an unsafe
default — please report it **privately**, not in a public issue.

**Email**: security@reflex-quant.ai
**PGP key**: [Download](https://www.reflex-quant.ai/.well-known/pgp-key.asc) *(placeholder — finalise before public repo launch)*
**Encryption**: PGP strongly preferred. Key fingerprint will be published
on our main site.

### What to include

A good security report includes:
- A clear description of the vulnerability
- Steps to reproduce (minimal)
- Impact (what can an attacker do?)
- Affected version(s) (we typically backport v0)
- Your name / handle (for credit in our security acknowledgements — unless
  you prefer anonymity)

### Response timeline

| Stage | Time |
|---|---|
| Acknowledgement | within 48 hours |
| Triage + severity assessment | within 5 business days |
| Patch for critical issues | within 7 business days |
| Patch for high-severity | within 14 business days |
| Patch for medium / low | next minor release (typically 30 days) |
| Public disclosure | after patch lands + 30-day grace |

We follow **coordinated disclosure**. We ask that you do not publicly disclose
the vulnerability until we have published a fix, or until 90 days have elapsed
since your report, whichever comes first.

### Severity classification (CVSS 3.1)

We use [CVSS 3.1](https://www.first.org/cvss/specification-document) base scores:

| Severity | CVSS | Examples |
|---|---|---|
| Critical | 9.0 – 10.0 | Remote code execution; full AGPL-3.0 source bypass |
| High     | 7.0 – 8.9 | Privilege escalation; API auth bypass; data leak of trade history |
| Medium   | 4.0 – 6.9 | Reflected XSS in showcase; checker bypass via malformed inputs |
| Low      | 0.1 – 3.9 | Information disclosure (e.g., server version banner) |
| None     | 0.0     | Theoretical issues with no exploit path |

---

## What we will **not** consider a vulnerability

- Trading losses that result from misconfiguration or misunderstanding of the
  reflection engine — this is a **product behavior**, not a security flaw.
  Please file these as bugs, not security issues.
- Reflections from third-party LLM providers (OpenRouter, etc.) that you
  disagree with — their API behavior is their responsibility.
- Reverse-engineering the public AGPL-3.0 code is explicitly permitted by
  the license; this is not a vulnerability.

---

## Security model — what Reflex Quant protects against

Reflex Quant is **local-first**. By design, your trading data, reflection
cycles, and decision errors never leave your machine unless you explicitly
configure them to.

Default protections:

- ✅ SQLite with strict WAL + journal_mode=SYNC=NORMAL — recoverable on crash
- ✅ Auto-rotate backups before any data reset
- ✅ No telemetry, no auto-upload, no remote calls unless explicitly enabled
- ✅ All credentials read from `.env`; never logged
- ✅ WAL truncate + lazy degradation — safe to run unattended
- ✅ API server binds to `127.0.0.1` by default; never exposed externally

Out-of-scope (your responsibility):

- ❌ Securing the host machine
- ❌ Rotating API keys (OpenRouter, exchanges)
- ❌ Backing up the SQLite DB to off-site storage
- ❌ Reviewing cron job security

---

## Out-of-band security fixes

Critical security fixes may be released as out-of-band patches before the
next scheduled release. We will:

1. Tag the patch `vN.N.N-security.1`
2. Announce in Discord + GitHub Security Advisory
3. Backport to the previous v0 release for 90 days
4. Add a CVE if applicable

---

## Hall of fame

We thank the following researchers for responsible disclosure:

*(empty — first disclosure to be added here)*

---

<sub>Last updated: 2026-10-06</sub>