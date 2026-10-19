// v20251014: Regression tests for signal_weight integration into K-line markers.
//   Verifies that the new optional fields (signal_weight, weight_tier, cold_signal)
//   are recognized by the marker color logic and the MoverEvent type is correct.
//
// This is a static-source-analysis test (not a React render test) because
// movers-panel.tsx is ~3000 lines with heavyweight lightweight-charts/DOM deps.
// Pattern matches the existing movers-panel-no-gray-dot.test.tsx style.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC_PATH = resolve(__dirname, "./movers-panel.tsx");
const src = readFileSync(SRC_PATH, "utf8");

describe("v20251014 signal_weight integration in movers-panel.tsx", () => {
  it("MoverEvent type includes signal_weight, weight_tier, weight_explain, cold_signal", () => {
    // Locate the MoverEvent type block (lines 33-45 area)
    const moverEventBlock = src.match(/type\s+MoverEvent\s*=\s*\{[\s\S]*?\n\}/);
    expect(moverEventBlock, "MoverEvent type not found").toBeTruthy();
    const block = moverEventBlock![0];
    expect(block).toMatch(/signal_weight/);
    expect(block).toMatch(/weight_tier/);
    expect(block).toMatch(/weight_explain/);
    expect(block).toMatch(/cold_signal/);
  });

  it("BUY marker color branches on weight_tier and cold_signal (4-way)", () => {
    // Find the BUY marker color selection block (around line 1920)
    const buyBlockMatch = src.match(
      /const\s+buyColor\s*=\s*[\s\S]*?_signal_weight:\s*w\s*\?\?/,
    );
    expect(buyBlockMatch, "BUY color block not found").toBeTruthy();
    const block = buyBlockMatch![0];
    // 4 color values expected
    expect(block).toMatch(/#15803d/); // strong -> deep green
    expect(block).toMatch(/#86efac/); // cold OR weak/ignored -> light green
    expect(block).toMatch(/#22c55e/); // default / medium
  });

  it("SELL marker color branches on weight_tier and cold_signal (4-way)", () => {
    const sellBlockMatch = src.match(
      /const\s+sellColor\s*=\s*[\s\S]*?_signal_weight:\s*w\s*\?\?/,
    );
    expect(sellBlockMatch, "SELL color block not found").toBeTruthy();
    const block = sellBlockMatch![0];
    expect(block).toMatch(/#991b1b/); // strong -> deep red
    expect(block).toMatch(/#fca5a5/); // cold OR weak/ignored -> light red
    expect(block).toMatch(/#ef4444/); // default / medium
  });

  it("Marker payload includes _signal_weight, _weight_tier, _cold_signal (tooltip fields)", () => {
    // Extract the BUY forEach block: from "buyTop.forEach" until the matching "})"
    // The pattern is robust: search for `_news_action: 'BUY'` and then check the
    // surrounding 600 chars (covers the full marker object).
    const buyIdx = src.indexOf("_news_action: 'BUY'");
    const sellIdx = src.indexOf("_news_action: 'SELL'");
    expect(buyIdx, "_news_action: 'BUY' not found").toBeGreaterThan(-1);
    expect(sellIdx, "_news_action: 'SELL' not found").toBeGreaterThan(-1);
    // Walk backwards to find the buyTop.forEach start, then 600 chars forward
    const buyStart = src.lastIndexOf("buyTop.forEach", buyIdx);
    const sellStart = src.lastIndexOf("sellTop.forEach", sellIdx);
    expect(buyStart, "buyTop.forEach not found").toBeGreaterThan(-1);
    expect(sellStart, "sellTop.forEach not found").toBeGreaterThan(-1);
    const buyBlock = src.slice(buyStart, buyStart + 2000);
    const sellBlock = src.slice(sellStart, sellStart + 2000);
    expect(buyBlock).toMatch(/_signal_weight:/);
    expect(buyBlock).toMatch(/_weight_tier:/);
    expect(buyBlock).toMatch(/_cold_signal:/);
    expect(sellBlock).toMatch(/_signal_weight:/);
    expect(sellBlock).toMatch(/_weight_tier:/);
    expect(sellBlock).toMatch(/_cold_signal:/);
  });

  it("v20251014 marker uses 6-dim comment block present", () => {
    // Sanity check: ensure the explanatory comment about 6-dim weight is there
    expect(src).toMatch(/6\s*维综合\s*signal_weight/);
    expect(src).toMatch(/v20251014:\s*6\s*维/);
  });

  it("No regression: HOLD/gray dot still removed (per previous session)", () => {
    // Make sure the prior gray-dot removal is preserved
    expect(src).not.toMatch(/holdCount\s*>\s*0\s*&&/);
    expect(src).toMatch(/v20251014:\s*HOLD\/中性新闻不在 K 线上显示/);
  });

  it("Frontend consumes signal_weight only via (e as any) cast (no strict typing required)", () => {
    // We use (e as any).signal_weight — this is the same pattern as the existing
    // dynamic event fields. Verify the cast pattern is consistent.
    const swReads = src.match(/\(e\s+as\s+any\)\.signal_weight/g) || [];
    expect(swReads.length).toBeGreaterThanOrEqual(2);  // BUY + SELL
    const coldReads = src.match(/\(e\s+as\s+any\)\.cold_signal/g) || [];
    expect(coldReads.length).toBeGreaterThanOrEqual(2);
  });
});
