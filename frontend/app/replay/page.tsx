'use client'

import { ReplayPanel } from '@/components/replay/replay-panel'
import { ToastProvider } from '@/components/ui/toast'

export default function ReplayPage() {
  return (
    <ToastProvider>
      <main className="mx-auto flex w-full max-w-[1600px] min-h-0 flex-1 flex-col overflow-hidden p-3 md:p-4">
        <ReplayPanel />
      </main>
    </ToastProvider>
  )
}
