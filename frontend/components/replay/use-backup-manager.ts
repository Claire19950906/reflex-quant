// v170 P0+：replay-panel 备份管理 hook
//
// 职责：拉取 /backups 列表 + 通过 /backups/open 在服务器本机打开目录。
// 状态与 openBackupDir 都封进 hook，调用方只读 backupInfo 与 openBackupDir。

import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiSend } from '@/lib/api'
import { useToast } from '@/components/ui/toast'

export type BackupInfo = {
  backup_dir: string
  files: Array<{ name: string; size: number; modified: number }>
}

/**
 * 备份管理 hook：
 *   - 挂载时自动拉一次 /backups（失败静默）
 *   - 提供 openBackupDir()，失败用 window.alert 报错
 * v302+ P0: window.alert 已替换为 toast.error — 不阻塞主线程、不打断回放流程
 */
export function useBackupManager() {
  const { toast } = useToast()
  const [backupInfo, setBackupInfo] = useState<BackupInfo | null>(null)

  const loadBackups = useCallback(async () => {
    try {
      const data = await apiGet<BackupInfo>('/backups')
      setBackupInfo(data)
    } catch {
      // ignore — 备份目录不可用不影响主面板
    }
  }, [])

  useEffect(() => {
    void loadBackups()
  }, [loadBackups])

  const openBackupDir = useCallback(async () => {
    try {
      const data = await apiSend<{ ok: boolean; error?: string; backup_dir: string }>(
        '/backups/open',
        'POST',
      )
      if (!data.ok) {
        // v302+ P0: 用 toast.error 替代 window.alert
        toast.error(`打开备份文件夹失败：${data.error ?? '未知错误'}`)
      }
    } catch (cause) {
      toast.error(
        `打开备份文件夹失败：${cause instanceof Error ? cause.message : '未知错误'}`,
      )
    }
  }, [toast])

  return { backupInfo, openBackupDir }
}