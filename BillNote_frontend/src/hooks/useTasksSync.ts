import { useEffect } from 'react'
import { useTaskStore } from '@/store/taskStore'
import { get_recent_tasks, get_task_status } from '@/services/note.ts'

const ACTIVE = ['PENDING', 'PARSING', 'DOWNLOADING', 'TRANSCRIBING', 'SUMMARIZING', 'SAVING']
const DAY_MS = 24 * 3600 * 1000

/**
 * 与后端 /tasks/recent 增量同步：
 * - 后端有、本地没有的活跃任务（如自动化检查轮创建的）→ 补进生成历史；
 * - 后端已 SUCCESS 而本地仍是旧状态（如应用重启打断轮询，或自动化跑完）→ 拉结果修正。
 * 只处理最近 24 小时内有更新的任务，避免把陈年历史灌进界面。
 */
export const useTasksSync = (interval = 30000) => {
  useEffect(() => {
    let stopped = false

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

    run()
    const timer = setInterval(run, interval)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [interval])
}
