# Recording a 60-second demo of Reflex Quant

This is the script we use to record a polished screen capture of the showcase. Total time: 5 minutes for setup, 60 seconds for the recording itself.

## Setup (5 minutes)

1. **Browser**: Chrome or Safari, fullscreen (Cmd+Ctrl+F in Chrome)
2. **Window size**: 1280×900 or wider. If on a Mac with smaller display, use Chrome's zoom-out (Cmd+−) to fit the showcase at the right size.
3. **Hide distractions**: Turn on Do Not Disturb. Hide the dock. Close other tabs.
4. **Open**: `~/Desktop/reflex-quant-showcase/index.html` (or the hosted version at your GitHub Pages URL)
5. **Test**: Click around. Make sure the live signal feed is rotating, the case cards open, the modal closes.

## Recording (60 seconds)

Use QuickTime Player → File → New Screen Recording → select the showcase window only (not full screen).

### The script

| Time | Action | What viewer sees |
|------|--------|------------------|
| 0:00–0:03 | Hold on the dashboard. Pause. Let the live signal feed rotate 1-2 times. | "This is Reflex Quant. A self-aware quant AI." |
| 0:03–0:06 | Click the first case card (WTI short, errors caught). Modal opens. Pause on the modal. | "When a trade loses, the system explains why. Here, three errors: E007, E012, E019." |
| 0:06–0:09 | Close modal (×). Click the second case card (XAU/USD long). Pause. | "When a trade is good, it stays quiet. Inverse-pair awareness correctly recognized this." |
| 0:09–0:13 | Close modal. Click the third case card (WTI long, meta-loop). Pause. | "The interesting case. All seven reasoning-logic checkers passed. The trade still lost. The system caught itself." |
| 0:13–0:18 | Close modal. Scroll down to show the meta-loop flow (5 nodes + arrow). Pause on the proposal box. | "The meta-loop proposed its own next checker. We added E028." |
| 0:18–0:25 | Scroll down to the knowledge graph section. Pause on each layer card. | "Every confirmed insight grows the knowledge graph. 142 here, 213 here. Compounding." |
| 0:25–0:30 | Scroll back to top. Hold on the dashboard. | (voice-over: your 30-second elevator pitch) |
| 0:30–0:60 | Your voice-over the dashboard. Suggested script below. | — |

### 30-second elevator pitch script

> "Reflex Quant is a self-aware AI for quant trading desks. We run a 7-layer reflection engine on every trade, the moment it closes. In our last 7 days of paper trading, we caught 73 errors across 59 trades — single-source trades, shallow causal chains, evidence-grade mismatches. The interesting ones are the trades where every checker passed and the trade still lost. The meta-loop catches those, and proposes its own next checker. Three new checkers per week. It's evidence the system is staying ahead of its own blindspots. We're raising a $1.5M seed round to take this to live trading."

## After recording

1. **Trim** the start and end in QuickTime (Edit → Trim).
2. **Save** as `demo.mov` (or mp4 via File → Export As).
3. **Convert to GIF** if you need an inline demo:

```bash
# Convert first 30 seconds of .mov to a smaller .gif
ffmpeg -i demo.mov -t 30 -vf "fps=10,scale=800:-1" -loop 0 demo.gif
```

Or use the pre-generated `showcase/media/demo.gif` from this repo (it's a 24-frame looping GIF showing the live signal feed rotating through the 6 templates).

## Use cases

- **Pitch deck**: 30-60s video embed in slide 9 ("Numbers slide") for live pitches.
- **Cold email**: Loom-style 2-minute walkthrough with audio.
- **GitHub README**: GIF embed at the top.
- **LinkedIn post**: 60-second clip with voice-over.

## Tips

- **Don't talk over the click sounds**. Pause 2-3 seconds after each action.
- **Mouse cursor is OK to show** — it telegraphs what you're clicking.
- **Hide notifications** — the worst thing is a Slack banner popping up mid-demo.
- **Record twice**. First take will have a stumble. Second take is usually good.
- **Under 90 seconds** for cold emails. 90s is the LinkedIn / YouTube attention cliff.

## Alternative: use the pre-generated assets

If you don't want to record your own demo, you can use:

- `showcase/media/demo.gif` — 24-frame loop, 4.8 seconds, shows signal feed rotating
- `assets/screenshots/01-dashboard-overview.png` — static full dashboard (1280×920)
- `assets/screenshots/03-meta-loop-flow.png` — the meta-loop (1240×620, the most important slide)

These are all already in the repo and ready to embed.

---

Questions? Open an issue or email z2132743607@163.com.
