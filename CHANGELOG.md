# Changelog

All notable changes to **Reflex Quant** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **i18n**: Showcase (`showcase/index.html`) and commercial license page
  (`LICENSE-COMMERCIAL.html`) now support English (default) and Simplified
  Chinese. Click the 🌐 toggle in the page header to switch. Choice persists
  in `localStorage['rq-lang']`. Browser language is auto-detected on first visit.
- **SEO**: Open Graph + Twitter Card meta tags on both HTML pages.
- **Trust signals**: `CHANGELOG.md`, `.github/CODEOWNERS`,
  `.github/FUNDING.yml`, custom `404.html`, `robots.txt`, `sitemap.xml`,
  `favicon.svg`, social-preview image.
- **Security**: CodeQL workflow (`.github/workflows/codeql.yml`) for
  automated security scanning on push / PR / weekly schedule.
- **Dependency hygiene**: Dependabot config (`.github/dependabot.yml`)
  for GitHub Actions weekly updates.
- **Editor consistency**: `.editorconfig` (UTF-8, LF, 2-space indent, final
  newline).
- **Issue forms**: `bug.yml` and `feature.yml` issue forms (GitHub Forms
  syntax) replace the old free-form markdown template.
- **README**: Link to `docs/SOCIAL_BIOS.md` from the hero section
  (17 ready-to-paste platform bios + cold-email templates).

## [0.225.0] — 2026-10-06

### Added
- **Meta-loop self-reflection (v225)**: When 7 reasoning-logic checkers
  all pass but the trade still loses, a meta-loop re-examines the
  decision. First real catch: cross-temporal drift
  (news age ≠ decision horizon). Proposed **E028: cross_temporal_consistency**.
- **27-layer reflection engine**: 7 reasoning-logic sub-layers
  (assumption, counterfactual, calibration, …) + sample-integrity,
  causal-chain, evidence-grade, and 4 more.
- **Knowledge graph persistence**: Patterns caught by the meta-loop
  accumulate and are queryable.
- **Live showcase**: `showcase/index.html` (no auth, runs offline in
  any browser). 6 screenshots + 1 demo GIF in `assets/screenshots/`.
- **Dual licensing**: AGPL-3.0 + commercial. Pricing tiers: Indie $5k/yr,
  Team $25k/yr, Firm $100k/yr, Custom/OEM (contact).
- **Pricing calculator** on `LICENSE-COMMERCIAL.html`: tier × seats × term
  × support tier with 1/2/3-year discounts and volume breaks above 50 seats.
- **Documentation** (`docs/`): `ARCHITECTURE.md`, `ROADMAP.md`,
  `PHILOSOPHY.md`, `FAQ.md`, `SECURITY_MODEL.md`, `CASE_STUDIES.md`,
  `GLOSSARY.md`, `BLOG_INDEX.md` (8 essays), `SOCIAL_BIOS.md` (17 variants).
- **Governance**: `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md`.
- **Deploy tooling**: `deploy.sh` (one-shot GitHub + Pages) and
  `netlify.toml` (one-click Netlify alternative). GitHub Actions
  workflow (`.github/workflows/pages.yml`) for automatic Pages deploy.

## [0.100.0] — 2026-06-15

### Added
- Initial public release. Core 7-layer reflection engine, E001–E027 checkers,
  single-asset decision flow (WTI, XAU, EUR, BTC, SPX, DXY).
- AGPL-3.0 license.

---

## How to read this changelog

- **Added** for new features.
- **Changed** for changes in existing functionality.
- **Deprecated** for soon-to-be-removed features.
- **Removed** for now-removed features.
- **Fixed** for any bug fixes.
- **Security** for vulnerability fixes.

[Unreleased]: https://github.com/Claire19950906/reflex-quant/compare/v0.225.0...HEAD
[0.225.0]: https://github.com/Claire19950906/reflex-quant/compare/v0.100.0...v0.225.0
[0.100.0]: https://github.com/Claire19950906/reflex-quant/releases/tag/v0.100.0
