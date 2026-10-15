"use client";

/**
 * Trident v300 — 前端反爬虫 Guard
 * =================================
 *
 * 集成：
 *   1. Cloudflare Turnstile（人机验证）
 *   2. 行为指纹采集（鼠标轨迹 / 滚动 / 点击）
 *   3. 上报到后端，行为分 < 0.35 触发二次验证
 *
 * 用法（包裹投资人 portal 整页）：
 *   <AntiBotGuard>
 *     <InvestorPortal />
 *   </AntiBotGuard>
 */

import { useEffect, useRef, useState } from "react";

declare global {
  interface Window {
    turnstile?: {
      render: (el: HTMLElement, opts: any) => string;
      reset: (id: string) => void;
    };
  }
}

interface Props {
  children: React.ReactNode;
  turnstileSiteKey?: string;
  behaviorEndpoint?: string;
  enabled?: boolean;
}

interface BehaviorEvent {
  type: "mousemove" | "scroll" | "key" | "click";
  t: number;
  x?: number;
  y?: number;
}

const BEHAVIOR_FLUSH_MS = 5000;
const BEHAVIOR_BATCH = 60;

export function AntiBotGuard({
  children,
  turnstileSiteKey = process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY || "1x00000000000000000000AA",
  behaviorEndpoint = "/api/security/behavior",
  enabled = true,
}: Props) {
  const [passed, setPassed] = useState(!enabled);
  const [behaviorScore, setBehaviorScore] = useState<number | null>(null);
  const turnstileRef = useRef<HTMLDivElement>(null);
  const turnstileIdRef = useRef<string | null>(null);
  const eventsRef = useRef<BehaviorEvent[]>([]);
  const lastFlushRef = useRef<number>(Date.now());

  // ── 1) Turnstile 渲染
  useEffect(() => {
    if (!enabled || passed) return;
    if (!turnstileRef.current) return;

    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js";
    script.async = true;
    script.defer = true;
    document.body.appendChild(script);

    script.onload = () => {
      if (window.turnstile && turnstileRef.current) {
        turnstileIdRef.current = window.turnstile.render(turnstileRef.current, {
          sitekey: turnstileSiteKey,
          callback: (token: string) => {
            // 把 token 放 sessionStorage 给业务请求用
            sessionStorage.setItem("trident_turnstile", token);
            setPassed(true);
          },
          "error-callback": () => {
            console.warn("turnstile failed");
          },
        });
      }
    };

    return () => {
      if (turnstileIdRef.current && window.turnstile) {
        try { window.turnstile.reset(turnstileIdRef.current); } catch {}
      }
    };
  }, [enabled, passed, turnstileSiteKey]);

  // ── 2) 行为采集
  useEffect(() => {
    if (!enabled) return;

    const onMove = (e: MouseEvent) =>
      eventsRef.current.push({ type: "mousemove", t: Date.now(), x: e.clientX, y: e.clientY });
    // v302+ P0: 实际滚动容器是 #app-scroll-root 而不是 window,
    // 因为 layout.tsx 把 <main> 作为唯一的滚动节点 — window.scrollY 永远 0。
    // 这里挂到 #app-scroll-root 节点,使用 capture 阶段确保 document 也能拿到。
    const onScroll = () => {
      const root = document.getElementById("app-scroll-root");
      const y = root ? root.scrollTop : window.scrollY;
      eventsRef.current.push({ type: "scroll", t: Date.now(), y });
    };
    const onKey = () => eventsRef.current.push({ type: "key", t: Date.now() });
    const onClick = (e: MouseEvent) =>
      eventsRef.current.push({ type: "click", t: Date.now(), x: e.clientX, y: e.clientY });

    document.addEventListener("mousemove", onMove, { passive: true });
    // v302+ P0: scroll 事件不冒泡 — 必须直接挂在 #app-scroll-root 节点上。
    // 这里在挂载时查找一次,挂载/卸载时同步挂摘。如果未来重新布局,只要保持
    // 这个 id 即可不需要改这里。
    const scrollRoot = document.getElementById("app-scroll-root");
    if (scrollRoot) scrollRoot.addEventListener("scroll", onScroll, { passive: true });
    // 兜底: 仍然挂一个 window scroll 监听, 防止极少数场景(embedded iframe / 全屏 dialog)
    // 滚动节点不是 #app-scroll-root 时丢失数据。
    window.addEventListener("scroll", onScroll, { passive: true });
    document.addEventListener("keydown", onKey);
    document.addEventListener("click", onClick);

    const timer = setInterval(() => {
      const now = Date.now();
      if (now - lastFlushRef.current < BEHAVIOR_FLUSH_MS) return;
      if (eventsRef.current.length === 0) return;

      const batch = eventsRef.current.splice(0, BEHAVIOR_BATCH);
      lastFlushRef.current = now;

      const token = sessionStorage.getItem("trident_turnstile") || "";
      fetch(behaviorEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ events: batch, turnstile_token: token }),
      })
        .then((r) => r.json())
        .then((d) => {
          if (typeof d.score === "number") setBehaviorScore(d.score);
          if (d.score < 0.2) {
            console.warn("low behavior score, re-challenge");
            setPassed(false);
            sessionStorage.removeItem("trident_turnstile");
          }
        })
        .catch(() => {});
    }, 1000);

    return () => {
      document.removeEventListener("mousemove", onMove);
      if (scrollRoot) scrollRoot.removeEventListener("scroll", onScroll);
      window.removeEventListener("scroll", onScroll);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("click", onClick);
      clearInterval(timer);
    };
  }, [enabled, behaviorEndpoint]);

  if (!enabled) return <>{children}</>;
  if (passed) return <>{children}</>;

  return (
    <div style={{
      minHeight: "100vh", display: "flex", flexDirection: "column",
      alignItems: "center", justifyContent: "center",
      background: "linear-gradient(180deg, #0b1220 0%, #1a2332 100%)",
      color: "#e5e7eb", fontFamily: "system-ui, sans-serif",
    }}>
      <h2 style={{ fontSize: 22, marginBottom: 8 }}>🔒 Trident 投资人验证</h2>
      <p style={{ color: "#9ca3af", fontSize: 14, marginBottom: 24 }}>
        请完成下方验证后查看 demo（约 3 秒）
      </p>
      <div ref={turnstileRef} />
      {behaviorScore !== null && (
        <p style={{ marginTop: 24, fontSize: 12, color: "#6b7280" }}>
          行为评分：{(behaviorScore * 100).toFixed(0)}/100
        </p>
      )}
      <p style={{ marginTop: 32, fontSize: 11, color: "#4b5563", maxWidth: 360, textAlign: "center" }}>
        持续异常访问会被记录并加入黑名单。<br/>
        By accessing, you agree to the NDA on the next page.
      </p>
    </div>
  );
}

export default AntiBotGuard;
