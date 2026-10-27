import { useEffect } from 'react'
import { useTaskStore } from '@/store/taskStore'
import { get_recent_tasks, get_task_status, resetRecentVersion } from '@/services/note.ts'
import { isForegroundBusy } from '@/utils/backgroundPriority.ts'

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

/** 视频级 keeper 选择：与后端 app/services/recent_dedupe.py 同一规则——
 * 非终态(进行中) > SUCCESS > 其他终态(FAILED)，同优先级取 createdAt 新者。
 * 后端 /tasks/recent 每个视频只返回一条，本地隐藏卡要跟它对齐。 */
const rankForDedupe = (t: any) =>
  ACTIVE.includes(t?.status) ? 2 : t?.status === 'SUCCESS' ? 1 : 0

/** 每个任务「上次见到的后端 updated_at」（会话级）。
 *  外部（API / 自动化 / MCP）重新生成完成后，任务状态可能仍是 SUCCESS，
 *  仅靠状态比较无法发现内容已更新——用 updated_at 变化当内容水位线。 */
const seenUpdatedAt = new Map<string, number>()

/**
 * 清理「后端已按视频去重隐藏」的本地卡（仅本地移除，不删后端文件）：
 * 失败风暴时代同一视频留下几十条 FAILED 状态文件，/tasks/recent 以前全量
 * 返回，本地历史里堆了几十张重复卡（2026-10-06 用户反馈）。后端去重后这些
 * 卡不在 serverIds 里，按 keeper 规则清掉；后端文件保留，现行笔记被删后
 * 老笔记还能经同步重新露出。
 */
const cleanupHiddenDupes = (serverTasks: any[]) => {
  const serverIds = new Set(serverTasks.map(t => t?.task_id).filter(Boolean))
  const tasks = useTaskStore.getState().tasks
  const keeperByVideo = new Map<string, any>()
  for (const t of tasks) {
    const vid = t?.audioMeta?.video_id
    if (!vid) continue
    const cur = keeperByVideo.get(vid)
    if (
      !cur ||
      rankForDedupe(t) > rankForDedupe(cur) ||
      (rankForDedupe(t) === rankForDedupe(cur) && (t.createdAt || '') > (cur.createdAt || ''))
    )
      keeperByVideo.set(vid, t)
  }
  const stale: string[] = []
  for (const t of tasks) {
    if (!t || serverIds.has(t.id) || ACTIVE.includes(t.status)) continue
    const vid = t.audioMeta?.video_id
    if (vid && keeperByVideo.get(vid)?.id !== t.id) stale.push(t.id)
  }
  if (stale.length) useTaskStore.getState().dropLocalTasks(stale)
}

/**
 * 与后端 /tasks/recent 同步：
 * - 进页/配对成功时回填历史概要（不带正文，点开时懒加载）——
 *   否则手机 Viewer 只能看到 24h 内的增量，更早的历史永远同步不到；
 * - 每 30s 增量：后端有、本地没有的活跃任务（如自动化检查轮创建的）→ 补进生成历史；
 * - 后端已 SUCCESS 而本地仍是旧状态（如应用重启打断轮询，或自动化跑完）→ 落状态。
 *
 * 远端性能约定（2026-10-23）：
 * 1. 未通过 /sys_check 不发任何请求（enabled=false）；
 * 2. 等 persist hydration 完成再回填，避免 hydration 覆盖刚回填的列表；
 * 3. 首次只拉 120 条概要，300 条全量延迟到 idle；绝不在这里逐条拉正文
 *    （旧实现最多 120 次顺序 /task_status，和当前笔记抢连接）；
 * 4. 只有「当前正在看的笔记」才会在 updated_at 变化时拉轻量正文；
 *    其它任务只标记 contentStale，等用户点开时再拉；
 * 5. 页面不可见/用户正在打开笔记时跳过本轮。
 */
export const useTasksSync = (interval = 30000, enabled = true) => {
  useEffect(() => {
    if (!enabled) return
    let stopped = false
    let running = false
    let timer: ReturnType<typeof setInterval> | null = null
    let focusTimer: ReturnType<typeof setTimeout> | null = null
    let fullTimer: ReturnType<typeof setTimeout> | null = null
    let idleId: number | null = null
    let partialDone = false
    let fullDone = false

    // 全量 300 条回填：只在 idle/延迟执行。孤儿清理只在这里做——
    // 用 120 条的子集判断孤儿会把 120 名之外的真实任务误删。
    const fullBackfill = async () => {
      if (stopped || fullDone) return
      // 用户此刻正在打开笔记：300 条全量回填让路，2 秒后再试。
      if (isForegroundBusy()) {
        fullTimer = setTimeout(() => void fullBackfill(), 2000)
        return
      }
      fullDone = true
      const data: any = await get_recent_tasks(300, { force: true })
      if (stopped || !Array.isArray(data?.tasks)) return
      useTaskStore.getState().backfillBackendTasks(data.tasks)
      cleanupHiddenDupes(data.tasks)
      // 后端上限就是 300：返回满 300 说明列表可能被截断，此时「不在列表里」
      // 不等于「后端没有」，不能按孤儿删（否则第 301 条以后会被误删）。
      if (data.tasks.length >= 300) return
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
      for (const id of orphans) {
        try {
          await useTaskStore.getState().removeTask(id)
        } catch {
          /* 删后端 404 也没关系：本来就是孤儿，本地已清 */
        }
      }
    }

    // 历史回填（每会话一次）：只并入概要，正文在点开笔记时懒加载。
    // 失败（如未配对 401）不锁标志——配对成功事件到来时可以重试。
    const backfill = async () => {
      if (stopped || partialDone) return
      const data: any = await get_recent_tasks(120, { force: true })
      if (stopped || !Array.isArray(data?.tasks)) return
      partialDone = true
      // 先并入回填（概要/自愈标题），再在合并后的全量上做重复卡清理
      useTaskStore.getState().backfillBackendTasks(data.tasks)
      cleanupHiddenDupes(data.tasks)
      // 300 条全量延后：先把首屏和点开笔记的带宽让出来
      if ('requestIdleCallback' in window) {
        idleId = (window as any).requestIdleCallback(() => void fullBackfill(), {
          timeout: 4000,
        })
      } else {
        fullTimer = setTimeout(() => void fullBackfill(), 2500)
      }
    }

    const run = async () => {
      if (stopped || running || isForegroundBusy()) return
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
      running = true
      try {
        // 拦截器已解包，直接是 { tasks, version, unchanged? }
        const data: any = await get_recent_tasks(120)
        if (stopped || !Array.isArray(data?.tasks) || data.unchanged) return
        // 30s 也做一次隐藏重复卡清理：手动重提同视频时，新 PENDING 卡进来后
        // 老 SUCCESS 卡要立刻让位（不等下次启动的全量回填）
        cleanupHiddenDupes(data.tasks)
        const fresh = (bt: any) => (bt.updated_at || 0) * 1000 > Date.now() - DAY_MS
        const currentId = useTaskStore.getState().currentTaskId

        for (const bt of data.tasks) {
          if (stopped || !bt?.task_id || !fresh(bt)) continue
          const local = useTaskStore.getState().tasks.find(t => t.id === bt.task_id)
          // 内容水位线：后端 updated_at 变化 = 这条任务被重跑/改写过（无论谁触发）。
          // 首次见到只记录基线，不触发拉取（避免每次启动把历史结果全拉一遍）。
          const upd = Number(bt.updated_at || 0)
          const prevUpd = seenUpdatedAt.get(bt.task_id)
          const contentChanged = prevUpd !== undefined && prevUpd !== upd
          seenUpdatedAt.set(bt.task_id, upd)

          if (!local) {
            // 只加概要：正文点开时再按需拉，绝不在后台逐条下载。
            if (ACTIVE.includes(bt.status)) {
              useTaskStore.getState().addBackendTask(bt)
            } else if (bt.status === 'SUCCESS' && bt.has_result) {
              useTaskStore.getState().addBackendTask(bt)
            }
            continue
          }

          // ① 外部把任务重新拉起（终态 → 进行中）：本地要跟着变成「生成中」，
          //    否则界面一直显示已成功，用户完全看不到外部重跑在发生。
          if (bt.status && ACTIVE.includes(bt.status) && !ACTIVE.includes(local.status)) {
            useTaskStore.getState().updateTaskContent(bt.task_id, {
              status: bt.status,
              message: bt.message || undefined,
              queuePosition: undefined,
            })
            continue
          }
          // ② 外部把任务记为失败（进行中 → FAILED，且本地还停留在旧状态）
          if (bt.status === 'FAILED' && local.status !== 'FAILED' && local.status !== 'FAILD') {
            useTaskStore.getState().updateTaskContent(bt.task_id, {
              status: 'FAILED',
              message: bt.message || undefined,
            })
            continue
          }
          // ②' 外部（手机 Viewer / MCP / 另一个窗口）把正在跑的任务停了：
          //     本地还在「生成中」时要跟着落「已停止」，否则界面一直转圈。
          //     只改进行中的本地状态：SUCCESS 等终态不因一次停止被翻掉。
          if (bt.status === 'CANCELLED' && ACTIVE.includes(local.status)) {
            useTaskStore.getState().updateTaskContent(bt.task_id, {
              status: 'CANCELLED',
              message: bt.message || '已停止生成',
              queuePosition: undefined,
            })
            continue
          }

          // ③ 有结果的任务内容被外部重写（updated_at 变化）。
          //    只有当前正在看的那篇才立刻拉轻量正文；其它任务只标记 stale，
          //    等用户点开时再拉——否则又是 N 条顺序请求抢连接。
          const needsResultPull =
            bt.has_result && contentChanged && (bt.status === 'SUCCESS' || bt.status === 'FAILED')
          const successFirstSeen =
            bt.status === 'SUCCESS' && local.status !== 'SUCCESS' && bt.has_result
          if (needsResultPull || successFirstSeen) {
            if (bt.task_id === currentId && bt.has_result) {
              try {
                const res: any = await get_task_status(bt.task_id, { view: 'note' })
                if (res?.result) {
                  // 内容水位线变化时 force 绕过 SUCCESS→SUCCESS 短路，让外部重写落地；
                  // 首次见到成功结果时只补正文，不覆盖本地已有版本。
                  // status 必须显式传入：本地可能还停在 SUMMARIZING/SAVING，只合正文
                  // 会把任务永远留在进行中（轮询重复弹成功、笔记打不开）。
                  useTaskStore.getState().applyServerResult(bt.task_id, res.result, {
                    force: needsResultPull || !!local.contentStale,
                    status: 'SUCCESS',
                  })
                  useTaskStore.getState().updateTaskContent(bt.task_id, { contentStale: false })
                }
              } catch (e) {
                console.warn('同步当前笔记结果失败:', bt.task_id, e)
              }
            } else if (successFirstSeen) {
              useTaskStore.getState().updateTaskContent(bt.task_id, {
                status: 'SUCCESS',
                message: undefined,
              })
            } else if (needsResultPull) {
              useTaskStore.getState().updateTaskContent(bt.task_id, { contentStale: true })
            }
          }
        }
      } finally {
        running = false
      }
    }

    const start = () => {
      if (stopped) return
      // 先回填概要，再跑增量：两个并发会让首屏同时发 120+120+300 三份列表。
      void backfill().finally(() => {
        if (!stopped) void run()
      })
      timer = setInterval(run, interval)
    }

    // 刚配完对立刻回填，手机不用等下一个轮询周期
    const onPaired = () => {
      resetRecentVersion()
      partialDone = false
      fullDone = false
      void backfill()
    }
    window.addEventListener('bilinote:worker-paired', onPaired)

    // 窗口重新获得焦点立即同步一次：外部改过状态时不用等下一个轮询周期。
    // focus 与 visibilitychange 会连着触发，去抖后只跑一轮。
    const onFocus = () => {
      if (document.visibilityState !== 'visible') return
      if (focusTimer) clearTimeout(focusTimer)
      focusTimer = setTimeout(() => void run(), 400)
    }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onFocus)

    // persist 用 IndexedDB 异步 hydration：先回填再 hydration 会被后者覆盖，
    // 列表要等到下一次全量回填才恢复（表现为「历史少了一半」）。
    let unsubHydrate: (() => void) | undefined
    if (useTaskStore.persist.hasHydrated()) {
      start()
    } else {
      unsubHydrate = useTaskStore.persist.onFinishHydration(start)
    }

    return () => {
      stopped = true
      if (timer) clearInterval(timer)
      if (focusTimer) clearTimeout(focusTimer)
      if (fullTimer) clearTimeout(fullTimer)
      if (idleId !== null && 'cancelIdleCallback' in window) {
        ;(window as any).cancelIdleCallback(idleId)
      }
      unsubHydrate?.()
      window.removeEventListener('bilinote:worker-paired', onPaired)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onFocus)
    }
  }, [interval, enabled])
}
