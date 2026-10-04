import { useEffect } from 'react'
import { useTaskStore } from '@/store/taskStore'
import { get_recent_tasks, get_task_status } from '@/services/note.ts'

const ACTIVE = ['PENDING', 'PARSING', 'DOWNLOADING', 'TRANSCRIBING', 'SUMMARIZING', 'SAVING']

/** 本地任务是否有正文（任一形态）：孤儿清理只动“无正文”的失败卡，有内容的失败卡保留 */
const hasTaskContent = (t: any): boolean => {
  const md = t?.markdown
  if (typeof md === 'string' ? md.trim() : md?.[0]?.content) return true
  const tr = t?.transcript
  if (typeof tr === 'string' ? tr.trim() : tr?.full_text?.trim() || tr?.segments?.length) return true
  return false
}

const DAY_MS = 24 * 3600 * 1000

/**
 * 与后端 /tasks/recent 同步：
 * - 进页/配对成功时全量回填一次历史概要（不带正文，点开时懒加载）——
 *   否则手机 Viewer 只能看到 24h 内的增量，更早的历史永远同步不到；
 * - 每 30s 增量：后端有、本地没有的活跃任务（如自动化检查轮创建的）→ 补进生成历史；
 * - 后端已 SUCCESS 而本地仍是旧状态（如应用重启打断轮询，或自动化跑完）→ 拉结果修正。
 * 增量只处理最近 24 小时内有更新的任务，避免把陈年状态变化反复拉结果。
 */
export const useTasksSync = (interval = 30000) => {
  useEffect(() => {
    let stopped = false

    // 历史全量回填（每会话一次）：只并入概要，正文在点开笔记时懒加载。
    // 失败（如未配对 401）不锁标志——配对成功事件到来时可以重试。
    // 顺带清本地孤儿：后端没这个任务了（磁盘文件已删/从无此任务），但本地
    // persist 里还留着卡——多见于 10-01 前后调试期写进来的脏数据（如三张
    // 「重复任务」失败卡，磁盘无文件、后端无记录，留着永远是未命名）。
    // 只清同时满足的：后端全量里没有 + 磁盘无结果 + 本地无正文 + FAILED/FAILD。
    let backfilled = false
    const backfill = async () => {
      if (backfilled) return
      const data: any = await get_recent_tasks(300)
      if (stopped || !Array.isArray(data?.tasks)) return
      backfilled = true
      const serverIds = new Set(data.tasks.map((t: any) => t?.task_id).filter(Boolean))
      const orphans = useTaskStore
        .getState()
        .tasks.filter(
          t =>
            t &&
            !serverIds.has(t.id) &&
            (t.status === 'FAILED' || t.status === 'FAILD') &&
            !hasTaskContent(t),
        )
        .map(t => t.id)
      useTaskStore.getState().backfillBackendTasks(data.tasks)
      for (const id of orphans) {
        try {
          await useTaskStore.getState().removeTask(id)
        } catch {
          /* 删后端 404 也没关系：本来就是孤儿，本地已清 */
        }
      }
    }
    // 刚配完对立刻回填，手机不用等下一个轮询周期
    window.addEventListener('bilinote:worker-paired', backfill)

    const run = async () => {
      // 拦截器已解包，直接是 { tasks: [...] }
      const data: any = await get_recent_tasks(120)
      const list = data?.tasks
      if (stopped || !Array.isArray(list)) return
      const fresh = (bt: any) => (bt.updated_at || 0) * 1000 > Date.now() - DAY_MS

      for (const bt of list) {
        if (stopped || !bt?.task_id || !fresh(bt)) continue
        const local = useTaskStore.getState().tasks.find(t => t.id === bt.task_id)

        if (!local) {
          if (ACTIVE.includes(bt.status)) {
            useTaskStore.getState().addBackendTask(bt)
          } else if (bt.status === 'SUCCESS' && bt.has_result) {
            try {
              const res: any = await get_task_status(bt.task_id)
              if (res?.result) useTaskStore.getState().addBackendTask(bt, res.result)
            } catch (e) {
              console.warn('同步后端成功任务失败:', bt.task_id, e)
            }
          }
        } else if (bt.status === 'SUCCESS' && local.status !== 'SUCCESS' && bt.has_result) {
          try {
            const res: any = await get_task_status(bt.task_id)
            if (res?.result) {
              const { markdown, transcript, audio_meta } = res.result
              // 结果文件里的生成参数快照（model_name/provider_id/style）一并合进
              // formData：徽标与 NoteForm 回显的数据源。只补本地缺失的键，
              // 不覆盖用户本地已有的提交值。
              const patch: any = {
                status: 'SUCCESS',
                markdown,
                transcript,
                audioMeta: audio_meta,
                message: undefined,
              }
              const fd = local.formData || {}
              const merged: any = { ...fd }
              let touched = false
              for (const k of ['model_name', 'provider_id', 'style', 'quality', 'video_url', 'platform'] as const) {
                if (!merged[k] && res.result[k]) {
                  merged[k] = res.result[k]
                  touched = true
                }
              }
              if (touched) patch.formData = merged
              useTaskStore.getState().updateTaskContent(bt.task_id, patch)
            }
          } catch (e) {
            console.warn('同步任务结果失败:', bt.task_id, e)
          }
        }
      }
    }

    backfill()
    run()
    const timer = setInterval(run, interval)
    return () => {
      stopped = true
      clearInterval(timer)
      window.removeEventListener('bilinote:worker-paired', backfill)
    }
  }, [interval])
}
