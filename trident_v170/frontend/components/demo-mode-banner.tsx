/**
 * Trident v300 — 体验模式水印横幅
 * =================================
 * 顶部固定条 + 右下角小角标。demo / licensed 模式生效，full 模式不渲染。
 *
 * 使用：在 app/layout.tsx 中 <DemoModeBanner /> 即可。
 */

'use client';

import { useEffect, useState } from 'react';
import { getModeBadge, IS_DEMO } from '@/lib/demo-mode';

export function DemoModeBanner() {
  const [hidden, setHidden] = useState(false);
  const badge = getModeBadge();

  // localStorage 记忆"我已知道这是 demo 版"，不再打扰
  useEffect(() => {
    if (typeof window === 'undefined') return;
    if (localStorage.getItem('trident_demo_ack') === '1') setHidden(true);
  }, []);

  if (!badge) return null;

  if (hidden) {
    // 右下角小角标，提醒但不强干扰
    return (
      <div
        aria-label="Trident 演示模式"
        className="pointer-events-none fixed bottom-2 right-2 z-50 select-none rounded-md bg-amber-500/10 px-2 py-1 text-[10px] font-mono text-amber-700 dark:text-amber-400"
        style={{ opacity: 0.6 }}
      >
        Trident · {badge.label}
      </div>
    );
  }

  return (
    <div
      role="status"
      className={[
        'sticky top-0 z-40 flex items-center justify-between gap-3 border-b px-4 py-1.5 font-mono text-[11px]',
        badge.tone === 'amber'
          ? 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400'
          : 'border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-400',
      ].join(' ')}
    >
      <div className="flex items-center gap-2">
        <span aria-hidden>{badge.tone === 'amber' ? '🟡' : '🔵'}</span>
        <span>
          Trident 当前为
          <strong className="mx-1 font-semibold">{badge.label}</strong>
          {IS_DEMO
            ? '· 部分功能受限 · 数据精度降低 · 仅供体验'
            : '· 受授权使用 · 部分功能按 license 控制'}
        </span>
      </div>
      <button
        type="button"
        onClick={() => {
          localStorage.setItem('trident_demo_ack', '1');
          setHidden(true);
        }}
        className="rounded px-2 py-0.5 text-[10px] hover:bg-black/5 dark:hover:bg-white/10"
        aria-label="我知道了"
      >
        我知道了 ✕
      </button>
    </div>
  );
}