'use client'

/**
 * /trading-health — 交易健康度看板入口。
 *
 * 数据来自独立只读页 frontend/public/trading-health.html（10s 轮询
 * /api/trading/health）。这里只做一个薄壳，保证导航里点进来就能看到，
 * 不重复实现一套取数逻辑。
 */
export default function TradingHealthPage() {
  return (
    <main style={{ height: 'calc(100vh - 56px)', width: '100%' }}>
      <iframe
        src="/trading-health.html"
        title="交易健康度"
        style={{ width: '100%', height: '100%', border: 0 }}
      />
    </main>
  )
}
