# Showcase — Interactive Demo

> 🎬 **Live demo of the Reflex Quant dashboard.**
> Open `index.html` in your browser. No install, no backend, no data leaves
> your machine.

---

## What is this?

A single-file, self-contained HTML demo (~25 KB) that simulates a live Reflex
Quant reflection dashboard with synthetic data. The demo is designed for:

- 📧 Sending to investors without giving away the real backend
- 🤝 Showing potential customers what the dashboard looks like
- 📰 Embedding in technical blog posts
- 🎤 Embedding in pitch decks

The demo **does not** include:

- ❌ Real API calls to the reflection engine
- ❌ Real WebSocket connections
- ❌ Real chart data

It **does** include:

- ✅ Simulated live signal feed (refreshes every 5 seconds)
- ✅ 3 hand-crafted case studies (with real error patterns)
- ✅ The 5-node meta-loop flow diagram
- ✅ The 6-layer knowledge graph visualization
- ✅ Full interactive UI — click cases, open detail modals

---

## How to view

### Option 1: Open the file

```bash
open showcase/index.html        # macOS
xdg-open showcase/index.html    # Linux
start showcase/index.html       # Windows
```

The demo opens in your default browser. No server needed.

### Option 2: Host on GitHub Pages

1. Push this repo to GitHub
2. Settings → Pages → Deploy from branch → main / root
3. Demo will be available at `https://<org>.github.io/reflex-quant/showcase/`

### Option 3: Embed in a blog post

```html
<iframe
  src="https://<org>.github.io/reflex-quant/showcase/"
  width="100%" height="700"
  style="border:1px solid #2d3a5f; border-radius:12px;">
</iframe>
```

---

## What's in the demo

### 1. Live signal feed (top)

- 6 signal templates, rotated every 5 seconds
- Each shows: ticker, side (BUY/SELL/HOLD), price, reasoning, source
- Mimics a real-time websocket stream

### 2. 27-Layer reflection case studies

3 hand-crafted cases:

1. **WTI Short** — system caught 3 errors (E007, E012, E019). Click to see why.
2. **XAU/USD Long** — system correctly DIDN'T flag the inverse-pair trade.
3. **WTI Long** — meta-loop triggered; all checkers passed but trade lost.

Each case is clickable — opens a detail modal with full reasoning.

### 3. Meta-loop flow diagram

A 5-node visualization of what happens when all 7 reasoning-logic checkers
pass but the trade loses:

```
Trade Closed → Outcome = LOSE → Meta-Loop Trigger →
Pattern Detected → Proposal (E028)
```

Plus an excerpt from a real `META_REFLECTION.md` entry.

### 4. Knowledge graph visualization

6 categories of insights accumulated over 7 days of paper trading, with
counts and descriptions.

---

## What this is NOT

This is **not** the production UI. The production dashboard:

- Connects to your local API server (127.0.0.1:8000)
- Shows your real signals, real reflections, real meta-loop proposals
- Has live charts and historical views
- Lets you override errors with full audit trail

The demo is the *architecture diagram*. The production is the *running system*.

---

## License

This showcase is released under the same dual license as the rest of this
repo: AGPL-3.0 + Commercial. See [../LICENSE](../LICENSE) and
[../LICENSE-COMMERCIAL.md](../LICENSE-COMMERCIAL.md).

---

<sub>Last updated: 2026-10-06</sub>