/**
 * Trident v300 — 体验模式（Demo Mode）前端辅助
 * =============================================
 *
 * 通过环境变量切换：
 *
 *   NEXT_PUBLIC_TRIDENT_MODE    = full | demo | licensed
 *   NEXT_PUBLIC_TRIDENT_API_KEY = （可选）演示版 API Key，前端 fetch 时自动带头
 *
 * 用法：
 *
 *   import { IS_DEMO, withApiKey, DemoBadge } from '@/lib/demo-mode'
 *
 *   fetch('/api/foo', { headers: withApiKey() })
 *
 *   {IS_DEMO && <DemoBadge />}
 */

export type TridentMode = 'full' | 'demo' | 'licensed';

export const TRIDENT_MODE: TridentMode =
  (process.env.NEXT_PUBLIC_TRIDENT_MODE as TridentMode) ?? 'full';

export const IS_DEMO = TRIDENT_MODE === 'demo';
export const IS_LICENSED = TRIDENT_MODE === 'licensed';
export const IS_FULL = TRIDENT_MODE === 'full';

export const TRIDENT_API_KEY: string | undefined =
  process.env.NEXT_PUBLIC_TRIDENT_API_KEY;

/**
 * 给 fetch 自动加 API Key 头（licensed/demo 模式下生效，full 不加）
 */
export function withApiKey(headers: HeadersInit = {}): HeadersInit {
  if (IS_FULL || !TRIDENT_API_KEY) return headers;
  return {
    ...headers,
    'X-Trident-API-Key': TRIDENT_API_KEY,
  };
}

/**
 * 体验版水印徽章（demo/licensed 模式显示，full 不显示）
 */
export function getModeBadge(): { label: string; tone: 'amber' | 'blue' } | null {
  if (IS_DEMO) return { label: '体验版', tone: 'amber' };
  if (IS_LICENSED) return { label: '授权版', tone: 'blue' };
  return null;
}

/**
 * 隐藏某些内部调试 UI（demo 模式下隐藏）
 * 例如：JSON 查看器、原始 LLM prompt 面板、SQL 调试入口
 */
export function shouldHideInternal(): boolean {
  return IS_DEMO;
}

/**
 * 数字精度前端再裁剪（防御性）
 * 即使后端没脱敏，前端也再 round 一次（双保险）
 */
export function clientRound(value: number, decimals: number = 2): number {
  if (!IS_DEMO) return value;
  const f = Math.pow(10, decimals);
  return Math.round(value * f) / f;
}

/**
 * fetch wrapper：自动带 API Key + 错误归一化
 */
export async function tridentFetch(
  url: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = withApiKey(init.headers ?? {});
  const resp = await fetch(url, { ...init, headers });
  if (!resp.ok) {
    // 统一错误格式：{ error: string, message: string, ... }
    const body = await resp.json().catch(() => ({}));
    const err = new Error(body.message || `HTTP ${resp.status}`);
    (err as any).status = resp.status;
    (err as any).body = body;
    throw err;
  }
  return resp;
}