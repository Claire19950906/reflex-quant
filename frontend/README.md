# Frontend

> ⚠️ **This directory contains UI templates only.** The full frontend code is
> in our private repository under design-partner agreement.

---

## What's here

This directory exists in the public repo for **architectural reference** and
to demonstrate the dashboard structure. It does **not** contain:

- ❌ Real API calls to the reflection engine
- ❌ Real WebSocket connections to the live signal feed
- ❌ Real authentication / user state
- ❌ Real chart data

It DOES contain:

- ✅ Component structure (where panels live)
- ✅ Color system / typography / spacing tokens
- ✅ Mock data shapes (so you know what to send the backend)

---

## Components overview

```
frontend/
├── src/
│   ├── app/
│   │   ├── page.tsx              # Main dashboard
│   │   ├── signals/              # Live signal feed
│   │   ├── reflection/           # 27-layer reflection panel
│   │   ├── meta-loop/            # Meta-loop dashboard
│   │   └── kg/                   # Knowledge graph view
│   ├── components/
│   │   ├── SignalCard.tsx
│   │   ├── ReflectionItem.tsx
│   │   ├── ErrorCode.tsx
│   │   ├── MetaFlowDiagram.tsx
│   │   └── KGNode.tsx
│   └── lib/
│       ├── api.ts                # Backend API client
│       └── types.ts              # Shared types
├── public/
└── tailwind.config.ts
```

---

## Demo

A **standalone interactive showcase** is available at:

- 📦 `../showcase/index.html` (in this repo)
- 🌐 Live demo at: <https://reflex-quant.github.io/showcase>

The showcase is a single HTML file (~25 KB, no external deps) that simulates
a live reflection dashboard with synthetic data. It's perfect for:

- Showing investors without giving away the real backend
- Showing potential customers what the dashboard looks like
- Embedding in technical blog posts
- Embedding in pitch decks

---

## Styling

We use Tailwind with a custom dark palette designed for long quant-trading
sessions (low eye-strain, high contrast on key data, muted secondary text).

```ts
// tailwind.config.ts excerpt
colors: {
  bg: { 1: '#0a0f1e', 2: '#131a2e', 3: '#1c2541' },
  border: '#2d3a5f',
  primary: '#3b82f6',     // signals / actions
  success: '#10b981',     // wins
  danger:  '#ef4444',     // errors
  warning: '#f59e0b',     // medium severity
  meta:    '#8b5cf6',     // meta-loop
}
```

Typography:
- UI: Inter (or system font on first load)
- Numbers: JetBrains Mono (always mono — alignment matters)

---

## API contract (what the frontend expects)

```ts
// GET /api/signals?since=ISO_TIMESTAMP
type Signal = {
  id: string;
  ticker: string;
  action: 'BUY' | 'SELL' | 'HOLD';
  entry: number;
  tp: number;
  sl: number;
  reasoning: string;          // LLM-generated reasoning text
  confidence: number;         // 0-1
  evidence_grade: 'L1' | 'L2' | 'L3';
  sources: string[];
  created_at: string;         // ISO 8601
};

// GET /api/signals/:id/reflection
type Reflection = {
  signal_id: string;
  started_at: string;
  finished_at: string;
  errors: Array<{
    code: 'E001' | 'E002' | ... | 'E027';
    layer: string;
    severity: 'LOW' | 'MEDIUM' | 'HIGH';
    remediation: string;
  }>;
  meta_loop_triggered: boolean;
  override?: {
    by: string;
    reason: string;
  };
};

// GET /api/meta-loop/cycles?since=DATE
type MetaCycle = {
  id: string;
  ran_at: string;
  trigger: 'all_checkers_passed_with_loss' | 'cluster_similarity' | 'manual';
  proposals: Array<{
    code: string;             // e.g. "E028"
    name: string;
    rationale: string;
    status: 'proposed' | 'under_review' | 'merged';
  }>;
};
```

---

## Running locally

You can't — yet. The frontend requires the backend. When v230 ships, this
section will be replaced with:

```bash
cd frontend
npm ci
npm run dev          # http://localhost:3000
```

---

## Want to contribute to the frontend?

Open an issue with the `frontend` tag. We accept contributions for:
- UI bug fixes
- Accessibility improvements (WCAG 2.1 AA target)
- Component reuse / simplification
- New visualizations (charts, graphs, etc.)
- Translations

We do **not** currently accept contributions for:
- Branding changes (logo, color system)
- Architecture overhauls

---

<sub>Last updated: 2026-10-06</sub>