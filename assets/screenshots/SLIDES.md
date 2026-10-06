# Reflex Quant — Screenshot Library

> Six high-resolution PNG screenshots for use in pitch decks, blog posts, and one-on-one investor demos. All data is synthetic and safe to publish.

---

## The screenshots

| # | File | Best for |
|---|------|----------|
| 1 | `01-dashboard-overview.png` (1280×920) | **The full picture.** Hero slide. Demo opening. "This is what it looks like in operation." |
| 2 | `02-signal-feed.png` (1100×780) | **Live signals.** "What does Reflex Quant actually output?" |
| 3 | `03-meta-loop-flow.png` (1240×620) | **The killer slide.** This is the meta-loop. Most investors will spend 2+ minutes here. |
| 4 | `04-knowledge-graph.png` (1100×600) | **The moat.** "Your knowledge graph grows with every trade." |
| 5 | `05-case-detail-modal.png` (980×720) | **The receipts.** Shows the system catching real errors (E007, E012, E019). |
| 6 | `06-stats-hero.png` (1280×540) | **The trailer slide.** Big numbers. Use as opener or closer. |

All screenshots are 72 DPI PNG, dark theme matching the live showcase. They will look correct on dark or light backgrounds — the dark UI keeps self-contained contrast.

---

## Suggested slide order (10-slide investor deck)

| Slide | Content | Screenshot |
|---|---|---|
| 1 | Title — "Reflex Quant: the self-reflective AI for quant trading desks" | — |
| 2 | Problem — "Quant PMs spend 80% watching trades, 20% reflecting" | — |
| 3 | Solution — "We don't just trade. We reflect on why." | `01-dashboard-overview.png` |
| 4 | Live signals — "Here's what it actually outputs" | `02-signal-feed.png` |
| 5 | The reflection engine — "27 checkers across 7 layers" | (architecture diagram) |
| 6 | **The meta-loop** — "When the checkers miss, the system catches its own blindspot" | `03-meta-loop-flow.png` |
| 7 | The moat — "Your knowledge graph grows with every trade" | `04-knowledge-graph.png` |
| 8 | Case study — "E007, E012, E019 caught on WTI short" | `05-case-detail-modal.png` |
| 9 | Numbers — "59 trades, 73 errors caught, 12 meta-loop cycles" | `06-stats-hero.png` |
| 10 | The ask — "$1.5M seed, 18-month runway" | — |

This is the order we use internally. Slides 6 and 8 are where conversations happen.

---

## Pitching tips

**The meta-loop slide (Slide 6) is the most important.**

Most investors will say "okay, but you can build a list of checkers in a weekend." The meta-loop answer is: yes, but the system proposes its own next checker. It's evidence that the system is staying ahead of its own blindspots.

When you walk through Slide 6, narrate the WTI long example:

> "Tuesday's inventory build headline was already priced by Thursday morning. None of the seven reasoning-logic checkers tested for time-windowed context validity. They couldn't have, because they were built before we knew this failure mode existed. The system flagged itself. We added E028. It caught 4 more drift signals in the next 7 days."

Then go to Slide 8 (case detail) and show E007, E012, E019 on the WTI short. **Different failure mode. Same mechanism.**

**The moat slide (Slide 7) is the second-most important.**

Investors who understand enterprise software will immediately ask: "isn't this just a knowledge graph?" Answer: yes, but ours grows from confirmed meta-loop proposals, not from any analyst's notes. It compounds.

**The numbers slide (Slide 9) is the closer.**

Use `06-stats-hero.png` and let the numbers sit on screen for 5-10 seconds while you say: "the system is proposing 3 new checkers per week. That's faster than we could write them."

---

## Demo recording

For the 60-second video to attach to your cold email or LinkedIn DM:

1. Open `~/Desktop/reflex-quant-showcase/index.html` in a browser
2. Maximize the window (1280×900 or wider)
3. QuickTime Player → File → New Screen Recording
4. Click through:
   - 0:00 — show the dashboard overview (3 sec)
   - 0:03 — click on the first case card to open the modal (3 sec)
   - 0:06 — close modal, click on the second case card (3 sec)
   - 0:09 — close modal, click on the meta-loop card (3 sec)
   - 0:12 — scroll down to show the live signal feed (5 sec)
   - 0:17 — show the knowledge graph section (3 sec)
   - 0:20 — say your 30-second elevator pitch over the static dashboard
5. Stop recording. Save as `demo.mov`.

If you don't want to record your own voice, just record 20 seconds of clicks and use the static screenshots above for the rest.

---

## Reusing the screenshots

The screenshots are **public-domain-equivalent for use in your pitches** — they contain no proprietary data, only synthetic trade examples and the system's public error code taxonomy.

If you fork this repo into a different brand (under commercial license), regenerate them with your branding. The Python source that generated them is in this conversation's `/tmp/rq_design_*.py` files — not committed to the repo (because it's tooling, not the product).

To regenerate with new data:
```bash
python3 /path/to/rq_design_a.py   # tokens + fonts + helpers
python3 /path/to/rq_design_main1.py
python3 /path/to/rq_design_main2.py
python3 /path/to/rq_design_main3a.py
python3 /path/to/rq_design_main3b.py
```
Edit the `signals = [...]` and `cases = ...` lists in `rq_design_main1.py` to change the data.
