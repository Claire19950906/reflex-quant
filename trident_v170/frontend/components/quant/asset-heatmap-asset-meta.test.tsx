// v2026.10.15: 数据品类扩 20 个 - ASSET_META 新增条目回归测试
// v302+ P9: ASSET_META 已收敛到 lib/asset-meta.ts 单一来源。
// 验证中央 ASSET_META 包含新 20 个资产的中文标签。

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const ASSET_META_PATH = resolve(__dirname, "../../lib/asset-meta.ts");
const EVENT_BUS_PATH = resolve(__dirname, "./event-bus.tsx");

const assetMetaSrc = readFileSync(ASSET_META_PATH, "utf8");
const eventBusSrc = readFileSync(EVENT_BUS_PATH, "utf8");

const NEW_LABELS: Record<string, string> = {
  XRP: "瑞波币", DOGE: "狗狗币", BNB: "币安币",
  AVAX: "雪崩", LINK: "Chainlink", DOT: "Polkadot",
  ADA: "卡尔达诺", LTC: "莱特币",
  BRENT: "Brent 原油", NG: "天然气", HG: "铜", SILVER: "白银",
  AMD: "AMD", NFLX: "奈飞", CRM: "Salesforce", JPM: "摩根大通",
  DIS: "迪士尼", ORCL: "甲骨文", BA: "波音", GS: "高盛",
};

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

describe("v2026.10.15 ASSET_META in lib/asset-meta.ts (v302+ P9 收敛后)", () => {
  it("contains all 20 new entries", () => {
    for (const [asset, label] of Object.entries(NEW_LABELS)) {
      const re = new RegExp(`\\b${asset}\\s*:\\s*['"]${escapeRe(label)}['"]`);
      expect(assetMetaSrc, `ASSET_META missing ${asset}: '${label}'`).toMatch(re);
    }
  });

  it("preserves legacy entries", () => {
    expect(assetMetaSrc).toMatch(/BTC\s*:\s*['"]比特币['"]/);
    expect(assetMetaSrc).toMatch(/ETH\s*:\s*['"]以太坊['"]/);
    expect(assetMetaSrc).toMatch(/SOL\s*:\s*['"]Solana['"]/);
    expect(assetMetaSrc).toMatch(/XAU\s*:\s*['"]黄金['"]/);
    expect(assetMetaSrc).toMatch(/SKHYNIX\s*:\s*['"]韩国海力士['"]/);
    expect(assetMetaSrc).toMatch(/US_HYNIX\s*:\s*['"]美国海力士['"]/);
  });

  it("no local ASSET_META in asset-heatmap.tsx (单一来源)", () => {
    const heatmapPath = resolve(__dirname, "./asset-heatmap.tsx");
    const heatmapSrc = readFileSync(heatmapPath, "utf8");
    expect(heatmapSrc).not.toMatch(/const\s+ASSET_META\s*:/);
  });

  it("no local ASSET_META in asset-leaderboard.tsx (单一来源)", () => {
    const leaderboardPath = resolve(__dirname, "./asset-leaderboard.tsx");
    const leaderboardSrc = readFileSync(leaderboardPath, "utf8");
    expect(leaderboardSrc).not.toMatch(/const\s+ASSET_META\s*:/);
  });
});

describe("v2026.10.15 event-bus.tsx asset category maps expanded", () => {
  it("STOCK_ASSETS contains new 10 stocks", () => {
    for (const stock of ["AMD", "NFLX", "CRM", "JPM", "DIS", "ORCL", "BA", "GS", "INTC", "V"]) {
      const re = new RegExp(`['"]${stock}['"]`);
      expect(eventBusSrc, `event-bus.tsx missing stock ${stock}`).toMatch(re);
    }
  });

  it("CRYPTO_ASSETS contains new 8 crypto tickers", () => {
    const setBlock = eventBusSrc.match(/CRYPTO_ASSETS\s*=\s*new Set\(\[[\s\S]*?\]\)/);
    expect(setBlock, "CRYPTO_ASSETS Set not found").toBeTruthy();
    for (const crypto of ["XRP", "DOGE", "BNB", "AVAX", "LINK", "DOT", "ADA", "LTC"]) {
      expect(setBlock![0], `CRYPTO_ASSETS missing ${crypto}`).toContain(`'${crypto}'`);
    }
  });

  it("eventCategory regex covers BRENT and SILVER", () => {
    expect(eventBusSrc).toMatch(/\/WTI\|CL\|OIL\|BRENT\//);
    expect(eventBusSrc).toMatch(/\/XAU\|GOLD\|XAG\|SILVER\//);
  });
});
