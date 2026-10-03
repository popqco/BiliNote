import { useEffect, useRef } from 'react'
import { useTaskStore } from '@/store/taskStore'
import { get_task_status } from '@/services/note.ts'
import toast from 'react-hot-toast'

const TERMINAL = ['SUCCESS', 'FAILED', 'FAILD']

/**
 * 任务状态轮询。
 *
 * 韧性设计（2026-10-01 事故复盘）：
 * - 网络级失败（后端重启/超时）不再把任务直接标 FAILED，保留原状态继续轮询；
 * - 只有后端明确返回业务失败（code !== -1 且带 msg）才落 FAILED，并把真实原因
 *   写在卡片上、弹一次带视频名字的提示；
 * - 状态未变化时也合并早期元信息（标题/封面）与排队位次，卡片信息第一时间可见。
 */
export const useTaskPolling = (interval = 3000) => {
  const tasks = useTaskStore(state => state.tasks)

  const tasksRef = useRef(tasks)
  useEffect(() => {
    tasksRef.current = tasks
  }, [tasks])

  // 连续网络错误计数：抖动期间静默重试，只在持续异常时提醒一次
  const netErrRef = useRef<Record<string, number>>({})
  // P1-③ 回填去重：已是 FAILED 但本地无内容的任务每任务每会话只补拉一次，
  // 避免每 3 秒对着注定无结果的失败任务发请求。重试会把本地状态改回 PENDING，
  // 走正常轮询路径，不受这里影响。
  const backfilledRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    const timer = setInterval(async () => {
      // P1-③：已是 FAILED 但本地没内容的任务，每任务每会话补拉一次后端
      // status（后端 FAILED-but-has-result 附带 result）。后端任务在前端
      // 无对应本地任务时，前端任务通常是 PENDING 待轮询，此处只做"补结果"，
      // 不改任何任务的终态/非终态——状态机语义不变。
      const failedWithoutContent = tasksRef.current.filter(task => {
        if (task.status !== 'FAILED' && task.status !== 'FAILD') return false
        if (backfilledRef.current.has(task.id)) return false
        // markdown: string | Markdown[]，取"是否有可展示内容"时用 any 避开联合索引的类型报错
        const md: any = task.markdown
        return !(typeof md === 'string' ? md : md?.[0]?.content)
      })
      for (const task of failedWithoutContent) {
        backfilledRef.current.add(task.id)
        try {
          await get_task_status(task.id)
        } catch (e: any) {
          const failedResult = e?.data?.result
          if (failedResult) {
            useTaskStore.getState().updateTaskContent(task.id, {
              markdown: failedResult.markdown,
              transcript: failedResult.transcript,
              audioMeta: failedResult.audio_meta,
            })
          }
        }
      }

      const pendingTasks = tasksRef.current.filter(task => !TERMINAL.includes(task.status))
      if (pendingTasks.length === 0) return

      for (const task of pendingTasks) {
        try {
          // 注意：axios 拦截器已解包，返回的是后端 data 字段（any 标注以匹配运行时形态）
          const res: any = await get_task_status(task.id)
          netErrRef.current[task.id] = 0
          const { status } = res

          if (status === 'SUCCESS') {
            const { markdown, transcript, audio_meta } = res.result
            toast.success('笔记生成成功')
            useTaskStore.getState().updateTaskContent(task.id, {
              status,
              markdown,
              transcript,
              audioMeta: audio_meta,
              queuePosition: undefined,
              message: undefined,
            })
            continue
          }

          if (status && status !== task.status) {
            useTaskStore.getState().updateTaskContent(task.id, {
              status,
              queuePosition: undefined,
              message: res.message || undefined,
            })
          }
          // 状态未变也合并早期元信息与排队位次（标题/封面在排队期间即可显示）
          if (res.audio_meta) {
            useTaskStore.getState().mergeTaskAudioMeta(task.id, res.audio_meta)
          }
          if (typeof res.queue_position === 'number' && res.queue_position !== task.queuePosition) {
            useTaskStore.getState().updateTaskContent(task.id, { queuePosition: res.queue_position })
          }
        } catch (e: any) {
          const isBusinessFailure = e && typeof e.code === 'number' && e.code !== -1
          if (isBusinessFailure) {
            // 后端明确失败（如 SUMMARY 阶段降级链耗尽）：落状态 + 展示真实原因。
            // P1-③：FAILED-but-has-result（后端 500 的 data 里带了 result）时
            // 把老结果一并写入 store，前端失败视图可展示已有笔记内容。
            const reason = String(e.msg || '任务失败')
            const failedResult = e?.data?.result
            useTaskStore.getState().updateTaskContent(task.id, {
              status: 'FAILED',
              message: reason,
              ...(failedResult
                ? {
                    markdown: failedResult.markdown,
                    transcript: failedResult.transcript,
                    audioMeta: failedResult.audio_meta,
                  }
                : {}),
            })
            toast.error(`「${task.audioMeta?.title || '未命名笔记'}」生成失败：${reason}`)
          } else {
            // 网络级错误：后端重启/超时，保留状态继续轮询
            const n = (netErrRef.current[task.id] || 0) + 1
            netErrRef.current[task.id] = n
            console.warn(`任务 ${task.id} 状态查询暂不可用（连续 ${n} 次），保持原状态继续重试`)
            if (n === 20) {
              toast.error('与后端的连接暂时异常，任务状态同步将在恢复后自动继续')
            }
          }
        }
      }
    }, interval)

    return () => clearInterval(timer)
  }, [interval])
}
