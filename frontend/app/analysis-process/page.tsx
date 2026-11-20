import { AnalysisProcessPanel } from '@/components/analysis-process/analysis-process-panel'

export const metadata = {
  title: '分析过程 · Trident',
}

export default function AnalysisProcessPage() {
  return (
    <div className="flex h-[calc(100vh-3rem)] flex-col">
      <header className="border-b border-border bg-background/60 px-4 py-3 backdrop-blur">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-bold tracking-tight text-foreground">
            分析过程
          </h1>
          <span className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
            news → gate → llm reasoning → trade → reflection → experience
          </span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          一条新闻从入库到反思、经验沉淀的完整后台推理过程,可视化 6 个阶段。
        </p>
      </header>
      <div className="min-h-0 flex-1">
        <AnalysisProcessPanel />
      </div>
    </div>
  )
}