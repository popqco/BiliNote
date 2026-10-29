import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import { cancel_task, delete_task, generateNote, get_task_status, get_task_versions } from '@/services/note.ts'
import { assign_note_category } from '@/services/categories.ts'
import { beginForegroundTask, endForegroundTask } from '@/utils/backgroundPriority.ts'
import { v4 as uuidv4 } from 'uuid'
import toast from 'react-hot-toast'
import { get, set, del } from 'idb-keyval'


export type TaskStatus =
  | 'PENDING'
  | 'PARSING'
  | 'DOWNLOADING'
  | 'TRANSCRIBING'
  | 'SUMMARIZING'
  | 'SAVING'
  | 'RUNNING'
  | 'SUCCESS'
  | 'FAILED'
  | 'FAILD'
  /** 用户主动停止（后端 /cancel_task）：终态但不是失败——界面显示「已停止」 */
  | 'CANCELLED'

export interface AudioMeta {
  cover_url: string
  duration: number
  file_path: string
  platform: string
  raw_info: any
  title: string
  video_id: string
  /** 轻量笔记视图从 raw_info 里抽出的两个字段（不再整包下发 raw_info） */
  uploader?: string
  webpage_url?: string
}

export interface Segment {
  start: number
  end: number
  text: string
}

export interface Transcript {
  full_text: string
  language: string
  raw: any
  segments: Segment[]
}
export interface Markdown {
  ver_id: string
  content: string
  style: string
  model_name: string
  created_at: string
}

export interface Task {
  id: string
  markdown: string|Markdown [] //为了兼容以前的笔记
  transcript: Transcript
  status: TaskStatus
  audioMeta: AudioMeta
  /** 进行中提示 / 失败原因（后端 status 文件的 message） */
  message?: string
  /** 当前笔记正文正在加载（历史概要卡点开后按需拉取） */
  contentLoading?: boolean
  /** 当前笔记正文加载失败（已有正文时不清空，仅提示） */
  contentError?: string
  /** 后端结果已更新，本地正文是旧的：下次打开时强制刷新（先显示旧内容） */
  contentStale?: boolean
  /** 排队位次：PENDING 时由后端返回，展示「排队中 · 第 N 位」 */
  queuePosition?: number
  /** 任务来源：manual=界面提交，auto=自动化检查轮创建 */
  origin?: string
  /** 笔记分类 id（后端 note_categories.json 的 assignments；无 = 未分类） */
  category?: string
  /**
   * 本次执行在本地被观察/发起的时刻（ms）。「停止生成」入口在刚启动后的
   * STOP_ARM_MS 内保持不可用——点「重新生成」后按钮位/面板会在毫秒级变成
   * 「停止生成」，手停在原位的第二下点击会把刚起来的新 run 停掉
   * （2026-10-18 用户实拍：两次 stop 都紧跟 retry 到达）。
   */
  startedAt?: number
  createdAt: string
  formData: {
    video_url: string
    link: undefined | boolean
    screenshot: undefined | boolean
    platform: string
    quality: string
    model_name: string
    provider_id: string
    /** 笔记风格（minimal/detailed/…）：徽标与 NoteForm 回显用；老任务可能缺失 */
    style?: string
    format?: string[]
    extras?: string
    video_understanding?: boolean
    video_interval?: number
    grid_size?: [number, number]
    task_id?: string
  }
}

interface TaskStore {
  tasks: Task[]
  currentTaskId: string | null
  addPendingTask: (taskId: string, platform: string, formData: any) => void
  updateTaskContent: (
    id: string,
    data: Partial<Omit<Task, 'id' | 'createdAt'>>,
    options?: { force?: boolean },
  ) => void
  /** 后端结果文件 → 本地任务：三条同步路径共用的唯一合并入口 */
  applyServerResult: (
    id: string,
    result: any,
    options?: { status?: TaskStatus; clearMessage?: boolean; force?: boolean },
  ) => void
  /** 局部合并 audioMeta（早期标题/封面 / 轮询到的元信息），不覆盖已有字段 */
  mergeTaskAudioMeta: (id: string, meta: Partial<AudioMeta>) => void
  /** 把后端发现的任务（如自动化任务）补进本地列表 */
  addBackendTask: (bt: any, result?: any) => void
  /** 历史全量回填：/tasks/recent 概要批量并入（只带概要，正文点开时懒加载） */
  backfillBackendTasks: (list: any[]) => void
  /** 仅从本地列表移除（不调后端删除）：/tasks/recent 视频级去重后，
   * 后端已隐藏的重复历史卡由本地同步清掉；后端文件保留，删掉现行笔记
   * 后老笔记还能经同步重新露出。 */
  dropLocalTasks: (ids: string[]) => void
  removeTask: (id: string) => void
  clearTasks: () => void
  setCurrentTask: (taskId: string | null) => void
  /** 点开历史卡：切当前任务并在点击处理器里立刻取正文（不干活的后台同步会暂停）。 */
  openTask: (taskId: string) => void
  /** 确保某篇笔记有正文：无正文才拉轻量视图；force 用于失败重试。 */
  ensureTaskContent: (id: string, options?: { force?: boolean }) => Promise<void>
  /** 按需加载的转录文本（打开「原文」面板时调用）。 */
  applyTranscript: (id: string, transcript: Transcript) => void
  /** 合并后端历史版本（归档旧笔记 + 同视频旧任务），按正文去重。 */
  mergeServerVersions: (id: string, versions: any[]) => void
  getCurrentTask: () => Task | null
  retryTask: (id: string) => void
  /** 停止正在生成/排队中的任务（后端 /cancel_task）：状态落 CANCELLED，
   *  改完模型/参数可直接重新生成，不必干等整条流程跑完 */
  cancelTask: (id: string) => Promise<boolean>
  /** 批量归类（乐观更新 + 失败回滚；categoryId=null 表示未分类） */
  assignCategory: (ids: string[], categoryId: string | null) => Promise<boolean>
  /** 分类被删除后：本地把该分类下的卡片显示为未分类（后端已断开关联） */
  dropCategory: (categoryId: string) => void
}

/** 「进行中」状态集合（排队/执行中的全部阶段）：停止按钮的可见性、startedAt
 *  的记时点都以此为准（NoteHistory 卡片、NoteForm 按钮、MarkdownViewer 面板共用） */
export const ACTIVE_STATUSES: TaskStatus[] = [
  'PENDING',
  'PARSING',
  'DOWNLOADING',
  'TRANSCRIBING',
  'SUMMARIZING',
  'SAVING',
  'RUNNING',
]

/** 终态集合：轮询/同步/计数/停止入口共用一份，避免各处各写一份数组 */
export const TERMINAL_STATUSES: TaskStatus[] = ['SUCCESS', 'FAILED', 'FAILD', 'CANCELLED']

export const isTerminalStatus = (status?: string | null): boolean =>
  !!status && TERMINAL_STATUSES.includes(status as TaskStatus)

/** 「停止」入口的防误触窗口：任务刚启动后这么久内停止请求会被拒/按钮不可用 */
export const STOP_ARM_MS = 2000

/** 停止入口当前是否可用（无 startedAt 视为可用：自动化任务/老数据没有
 *  「刚刚点击启动」这个上下文，不存在误触窗口） */
export const isTaskStopArmed = (task?: Pick<Task, 'startedAt'> | null): boolean =>
  !task?.startedAt || Date.now() - task.startedAt >= STOP_ARM_MS

/** markdown 字段（string | Markdown[]）是否已有可展示正文 */
const hasMarkdownContent = (md: any): boolean =>
  typeof md === 'string'
    ? !!md.trim()
    : !!(Array.isArray(md) && md.some((v: any) => !!v?.content?.trim()))

const EMPTY_TRANSCRIPT: Transcript = {
  full_text: '',
  language: '',
  raw: null,
  segments: [],
}

// 当前笔记正文请求：新的点开会让旧的 abort/失效，旧响应绝不覆盖新笔记。
let contentAbort: AbortController | null = null
let contentSeq = 0
let contentInFlight: string | null = null
/** 本会话已拉过版本列表的任务：即使本地已有正文（例如轮询先写入当前版），
 *  也要再拉一次归档版本，否则同一视频的旧版本下拉永远只有一条。 */
const versionsLoaded = new Set<string>()

/**
 * 持久化瘦身：只留历史卡需要的概要。
 *
 * zustand persist 每次 set 都会同步 JSON.stringify 整个 state——旧实现把
 * 所有 markdown/transcript/raw_info 一起写，手机上点一下历史卡要先卡住
 * 序列化几 MB。正文改成点开时按需拉取（view=note，约 8KB gzip）。
 */
const stripTaskForPersist = (t: Task): Task => {
  const audio = t.audioMeta
    ? ({
        ...t.audioMeta,
        raw_info: null,
        file_path: '',
        video_path: '',
      } as any)
    : t.audioMeta
  return {
    ...t,
    markdown: '',
    transcript: EMPTY_TRANSCRIPT,
    audioMeta: audio,
    contentLoading: false,
    contentError: undefined,
    contentStale: false,
  }
}

/**
 * 结果文件 audio_meta 落地前的合并：空字段不覆盖本地已有值。
 *
 * 老笔记的结果文件里经常没有封面/标题（早期版本才补全元数据），而懒加载正文 /
 * 轮询 / 增量同步过去都是整包替换 audioMeta——一条老笔记点开一次，卡片上刚从
 * /tasks/recent 拿到的好封面就被空值清掉，表现为「封面刷不出来」（2026-10-17
 * 用户反馈）。这里只让非空字段覆盖本地，其余保留。
 */
export const mergeAudioMeta = (
  prev: Partial<AudioMeta> | undefined | null,
  incoming: any,
): AudioMeta => {
  const base: any = { ...(prev || {}) }
  for (const [k, v] of Object.entries(incoming || {})) {
    if (v === undefined || v === null) continue
    if (typeof v === 'string' && !v.trim()) continue
    base[k] = v
  }
  return base as AudioMeta
}

/** 刚在本机改过分类的任务（task_id → 时间戳）。
 *
 * 归类请求返回后，之前已经发出的 /tasks/recent 轮询可能带着旧分类落地，
 * 把乐观更新冲掉（卡片闪回未分类、5 秒后再变回来）。窗口期内同步不覆盖分类。
 */
const categoryWriteGuard = new Map<string, number>()
const CATEGORY_GUARD_MS = 10_000

/** 结果文件里的生成参数快照键：只补本地缺失值，不覆盖用户提交时的选择 */
const RESULT_FORM_KEYS = [
  'model_name',
  'provider_id',
  'style',
  'quality',
  'video_url',
  'platform',
] as const

/** 后端 /tasks/recent 概要（+可选结果）→ 本地 Task。
 *  徽标数据源优先级：结果文件（model_name/provider_id/style）＞ 概要 ＞ 空。
 *  addBackendTask（增量）与 backfillBackendTasks（全量回填）共用一份映射。 */
const backendTaskToTask = (bt: any, result?: any, createdAt?: string): Task => {
  const formData = {
    video_url: result?.video_url || bt.video_url || '',
    platform: result?.platform || bt.platform || '',
    quality: result?.quality || bt.quality || 'medium',
    model_name: result?.model_name || bt.model_name || '',
    provider_id: result?.provider_id || bt.provider_id || '',
    style: result?.style || bt.style || '',
    format: result?.format || bt.format || [],
    extras: result?.extras || bt.extras || '',
    video_understanding: result?.video_understanding ?? bt.video_understanding ?? false,
    video_interval: result?.video_interval ?? bt.video_interval ?? 6,
    grid_size: result?.grid_size || bt.grid_size || [2, 2],
    link: result?.link ?? bt.link ?? undefined,
    screenshot: result?.screenshot ?? bt.screenshot ?? undefined,
  }
  return {
    id: bt.task_id,
    status: bt.status,
    message: bt.message || undefined,
    origin: bt.origin || 'manual',
    category: bt.category || undefined,
    markdown: result?.markdown || '',
    transcript: result?.transcript || { full_text: '', language: '', raw: null, segments: [] },
    // 概要与结果文件合并：结果文件缺封面/标题时保留 /tasks/recent 的值
    audioMeta: mergeAudioMeta(
      {
        cover_url: bt.cover_url || '',
        duration: bt.duration || 0,
        file_path: '',
        platform: bt.platform || '',
        raw_info: null,
        title: bt.title || '',
        video_id: bt.video_id || '',
      },
      result?.audio_meta,
    ),
    createdAt: createdAt ?? new Date().toISOString(),
    formData,
  }
}

export const useTaskStore = create<TaskStore>()(
  persist(
    (set, get) => ({
      tasks: [],
      currentTaskId: null,

      addPendingTask: (taskId: string, platform: string, formData: any) =>

        set(state => ({
          tasks: [
            {
              formData: formData,
              id: taskId,
              status: 'PENDING',
              // 本地刚提交：停止按钮从这个时刻起才有防误触窗口
              startedAt: Date.now(),
              markdown: '',
              platform: platform,
              transcript: {
                full_text: '',
                language: '',
                raw: null,
                segments: [],
              },
              createdAt: new Date().toISOString(),
              audioMeta: {
                cover_url: '',
                duration: 0,
                file_path: '',
                platform: '',
                raw_info: null,
                title: '',
                video_id: '',
              },
            },
            ...state.tasks,
          ],
          currentTaskId: taskId, // 默认设置为当前任务
        })),

      updateTaskContent: (id, data, options) =>
          set(state => ({
            tasks: state.tasks.map(task => {
              if (task.id !== id) return task

              // SUCCESS→SUCCESS 且本地已有正文时短路：防止轮询/同步冲掉用户
              // 已有的版本列表。回填的历史概要（SUCCESS 但无正文）要放行——
              // 懒加载正文靠这条路径写进来。外部内容水位线变化时传 force 绕过。
              if (
                task.status === 'SUCCESS' &&
                data.status === 'SUCCESS' &&
                hasMarkdownContent(task.markdown) &&
                !options?.force
              )
                return task

              // 被外部（自动化/MCP/另一客户端）启动的任务也要有 startedAt：
              // 状态从「非进行中」变为「进行中」时记时，停止入口的防误触
              // 窗口对外部启动同样成立（仅当本地没有更近的记时点时才覆盖）
              let patch: any = data
              if (
                data.status &&
                ACTIVE_STATUSES.includes(data.status as TaskStatus) &&
                !ACTIVE_STATUSES.includes(task.status)
              ) {
                patch = { ...data, startedAt: Date.now() }
              }

              // 如果是 markdown 字符串，封装为版本
              if (typeof patch.markdown === 'string') {
                const prev = task.markdown
                // 版本徽标用合并后的 formData：轮询/同步可能在同一 patch 里带来
                // 后端结果文件的 model_name/style（旧 task.formData 还是空的）。
                const fd = (patch as any).formData || task.formData || {}
                const newVersion: Markdown = {
                  ver_id: `${task.id}-${uuidv4()}`,
                  content: patch.markdown,
                  style: fd.style || '',
                  model_name: fd.model_name || '',
                  created_at: new Date().toISOString(),
                }

                let updatedMarkdown: Markdown[]
                if (Array.isArray(prev)) {
                  updatedMarkdown = [newVersion, ...prev]
                } else {
                  updatedMarkdown = [
                    newVersion,
                    ...(typeof prev === 'string' && prev
                        ? [{
                          ver_id: `${task.id}-${uuidv4()}`,
                          content: prev,
                          style: fd.style || '',
                          model_name: fd.model_name || '',
                          created_at: new Date().toISOString(),
                        }]
                        : []),
                  ]
                }

                return {
                  ...task,
                  ...patch,
                  markdown: updatedMarkdown,
                }
              }

              return { ...task, ...patch }
            }),
          })),

      applyServerResult: (id, result, options) => {
        const task = get().tasks.find(t => t.id === id)
        if (!task) return
        if (!result) {
          // 后端已是终态但结果文件缺失/读失败时，状态仍要落地：否则任务永远
          // 停在「保存中」，轮询重复成功提示（2026-10-21 实拍）。
          if (options?.status !== undefined) {
            get().updateTaskContent(id, {
              status: options.status,
              queuePosition: undefined,
              ...(options.clearMessage === false ? {} : { message: undefined }),
            })
          }
          return
        }
        const patch: any = {
          markdown: result.markdown,
          // 轻量视图不带 transcript；只有结果里真有才覆盖，避免把已经
          // 按需加载好的字幕清空（打开笔记/后台同步都走这条合并入口）。
          ...(result.transcript ? { transcript: result.transcript } : {}),
          // 空字段不覆盖本地（老结果文件缺封面/标题，整包替换会清掉卡片元数据）
          audioMeta: mergeAudioMeta(task.audioMeta, result.audio_meta),
          queuePosition: undefined,
        }
        if (options?.status !== undefined) patch.status = options.status
        if (options?.clearMessage !== false) patch.message = undefined
        // 结果文件里的生成参数快照只补本地缺失的键，不覆盖提交时的选择。
        const fd = task.formData || {}
        const merged: any = { ...fd }
        let touched = false
        for (const k of RESULT_FORM_KEYS) {
          if (!merged[k] && result[k]) {
            merged[k] = result[k]
            touched = true
          }
        }
        if (touched) patch.formData = merged
        get().updateTaskContent(id, patch, { force: options?.force })
      },


      mergeTaskAudioMeta: (id, meta) =>
        set(state => ({
          tasks: state.tasks.map(task => {
            if (task.id !== id) return task
            // 每 3s 轮询都会带同一份 audio_meta；值没变就别 set——
            // 旧实现每次 set 都会触发整库持久化，白白卡主线程。
            const changed = Object.entries(meta || {}).some(([k, v]) => {
              if (v === undefined || v === null) return false
              const cur = (task.audioMeta as any)?.[k]
              if (typeof v === 'object') return JSON.stringify(cur) !== JSON.stringify(v)
              return cur !== v
            })
            return changed ? { ...task, audioMeta: { ...task.audioMeta, ...meta } } : task
          }),
        })),

      addBackendTask: (bt: any, result?: any) =>
        set(state => {
          if (state.tasks.some(t => t.id === bt.task_id)) return state
          return { tasks: [backendTaskToTask(bt, result), ...state.tasks] }
        }),

      // 历史全量回填：手机 Viewer 首次打开也能看到 Worker 全部笔记——
      // 之前只有 24h 内更新的增量会进列表，更早的历史永远同步不到
      // （2026-10-04 用户反馈：手机只能加载 12 篇）。只并入概要不拉正文，
      // 几十篇正文一次灌进 IndexedDB 太重；正文在点开笔记时懒加载。
      // 注意用概要刷新已存在的任务：后端会自愈补标题（老任务状态文件只有
      // status），本地 persist 里存的仍是旧的「无标题」快照，不刷新就永远
      // 显示「未命名笔记」（2026-10-04 端口/标题复查发现）。
      backfillBackendTasks: (list: any[]) =>
        set(state => {
          if (!Array.isArray(list) || list.length === 0) return state
          const byId = new Map(list.filter(bt => bt?.task_id).map(bt => [bt.task_id, bt]))
          let touched = false
          const tasks = state.tasks.map(t => {
            const bt = byId.get(t.id)
            if (!bt) return t
            byId.delete(t.id)
            // 标题/封面/时长：后端自愈后的值优先，本地已有值次之
            const meta = {
              ...t.audioMeta,
              title: (bt as any).title || t.audioMeta?.title || '',
              cover_url: (bt as any).cover_url || t.audioMeta?.cover_url || '',
              duration: (bt as any).duration ?? t.audioMeta?.duration ?? 0,
              video_id: (bt as any).video_id || t.audioMeta?.video_id || '',
              platform: (bt as any).platform || t.audioMeta?.platform || '',
            }
            // 分类跟随后端，但刚在本机改过的（窗口期）不让旧轮询覆盖乐观更新
            const guarded =
              Date.now() - (categoryWriteGuard.get(t.id) || 0) < CATEGORY_GUARD_MS
            const btCategory = (bt as any).category || undefined
            const nextCategory = guarded ? t.category : btCategory
            // status/message 跟随后端终态走（失败原因等），正文/版本不碰
            if (
              meta.title === t.audioMeta?.title &&
              meta.cover_url === t.audioMeta?.cover_url &&
              (bt as any).status === t.status &&
              ((bt as any).message || undefined) === t.message &&
              nextCategory === t.category
            )
              return t
            touched = true
            return {
              ...t,
              status: (bt as any).status ?? t.status,
              message: (bt as any).message || undefined,
              category: nextCategory,
              audioMeta: meta,
            }
          })
          const incoming: Task[] = []
          for (const bt of byId.values()) {
            // createdAt 取后端更新时间，老笔记才能在历史列表里排对位置
            incoming.push(
              backendTaskToTask(bt, undefined, new Date((bt.updated_at || 0) * 1000).toISOString()),
            )
          }
          if (incoming.length === 0 && !touched) return state
          return {
            tasks: [...tasks, ...incoming].sort((a, b) =>
              (b.createdAt || '').localeCompare(a.createdAt || ''),
            ),
          }
        }),

      getCurrentTask: () => {
        const currentTaskId = get().currentTaskId
        return get().tasks.find(task => task.id === currentTaskId) || null
      },
      retryTask: async (id: string, payload?: any) => {

        if (!id){
          toast.error('任务不存在')
          return
        }
        const task = get().tasks.find(task => task.id === id)
        console.log('retry',task)
        if (!task) return

        const newFormData = payload || task.formData
        // 无 payload 的重试（失败页"重试"按钮）：formData 可能缺模型参数
        // （参数快照前的老任务），直接发会复现"后台炸响应已发出、任务永远 PENDING"。
        // 这里先拦住：缺哪个说哪个，不发请求、不改状态。
        if (!newFormData?.model_name || !newFormData?.provider_id) {
          toast.error('该任务缺少模型参数（老任务无参数快照），请先在表单重选模型后再点重新生成')
          return
        }
        // 乐观反馈（2026-10-18）：提交请求在后端繁忙时要几秒才回来，期间
        // 界面毫无变化，用户会以为没点上而再点一次——那第二下正落在随后
        // 变出的「停止生成」上。先本地切 PENDING，失败再回滚。
        const prevStatus = task.status
        const prevMessage = task.message
        set(state => ({
          tasks: state.tasks.map(t =>
            t.id === id
              ? { ...t, status: 'PENDING', message: undefined, queuePosition: undefined, startedAt: Date.now() }
              : t,
          ),
        }))
        try {
          await generateNote({
            ...newFormData,
            task_id: id,
          })
        } catch (e: any) {
          // 回滚乐观状态（成功路径不需要再改：PENDING 已是目标态）
          set(state => ({
            tasks: state.tasks.map(t =>
              t.id === id ? { ...t, status: prevStatus, message: prevMessage } : t,
            ),
          }))
          // 后端去重拦截：该视频已有未完成任务，选中已存在的那张卡
          if (e?.data?.duplicated && e?.data?.existing_task_id) {
            set({ currentTaskId: e.data.existing_task_id })
            return
          }
          // 就绪门禁：转写模型未下载好。不要把任务标成 PENDING（会一直转），
          // 给提示让用户先去下载。
          if (e?.data?.reason === 'transcriber_model_not_ready') {
            toast.error(
              e?.data?.downloading
                ? '转写模型正在下载中，请稍候再重试'
                : '转写模型尚未下载，请先去「设置 → 音频转写配置」页下载',
            )
            return
          }
          console.error('重试任务失败：', e)
          return
        }

        set(state => ({
          tasks: state.tasks.map(t =>
              t.id === id
                  ? {
                    ...t,
                    formData: newFormData, // ✅ 显式更新 formData
                    status: 'PENDING',
                    // 清掉上一轮的终态文案（失败原因/「已停止生成」）：不清的话
                    // 重试后的加载面板会先显示旧消息，等下一次轮询才刷新
                    message: undefined,
                    queuePosition: undefined,
                    startedAt: Date.now(),
                  }
                  : t
          ),
        }))
      },


      removeTask: async id => {
        const task = get().tasks.find(t => t.id === id)

        // 更新 Zustand 状态
        set(state => ({
          tasks: state.tasks.filter(task => task.id !== id),
          currentTaskId: state.currentTaskId === id ? null : state.currentTaskId,
        }))

        // 调后端真删。之前这里有两个坑：platform 取的是 task.platform
        // （老任务的这个字段是 undefined，axios 会把它整个丢掉 → 后端 422 →
        // 「服务器错误，请稍后再试」+「删除任务失败」两条红条），而且后端当时
        // 只是空实现，卡片删掉 30 秒后又被 /tasks/recent 同步回来。
        if (task) {
          try {
            const res: any = await delete_task({
              task_id: task.id,
              video_id: task.audioMeta?.video_id,
              platform:
                task.audioMeta?.platform || (task as any).platform || task.formData?.platform || 'bilibili',
            })
            // 后端会把被删视频写进「自动化忽略名单」（不再自动重新生成），
            // 当场告知用户，免得以为删完还会被自动化补回来
            const ignored = Array.isArray(res?.ignored) ? res.ignored.length : 0
            toast.success(
              ignored > 0 ? '已删除该笔记（该视频不会再被自动化生成）' : '已删除该笔记',
            )
          } catch (e: any) {
            // 删除失败（例如任务还在生成中）：把卡片放回去，别让用户以为删干净了
            set(state =>
              state.tasks.some(t => t.id === task.id)
                ? state
                : { ...state, tasks: [task, ...state.tasks] },
            )
            const msg = e?.msg || e?.detail || '删除失败'
            toast.error(typeof msg === 'string' ? msg : '删除失败')
            console.error('❌ 删除任务失败:', e)
          }
        }
      },

      clearTasks: () => set({ tasks: [], currentTaskId: null }),

      /**
       * 停止正在生成/排队中的任务。
       *
       * 后端立即把状态落 CANCELLED 并中止当前步骤（下载进度回调、转写重试
       * 间隙、总结流式增量处都会命中检查点）；本地同步成「已停止」，
       * 失败卡上的「重试」按钮即可用来换模型重跑。
       */
      cancelTask: async (id: string) => {
        const task = get().tasks.find(t => t.id === id)
        if (!task) {
          toast.error('任务不存在')
          return false
        }
        // 防误触（2026-10-18）：刚点过「重新生成/提交」的 STOP_ARM_MS 内，
        // 停止入口一律不生效——用户实拍里那两次「莫名其妙被停止」都是
        // 启动后 1 秒内的第二下点击落在刚变出的停止按钮上。
        if (!isTaskStopArmed(task)) {
          toast('任务刚刚开始，稍候即可停止', { icon: '⏳' })
          return false
        }
        try {
          const res: any = await cancel_task(id)
          if (res && res.cancelled === false) {
            // 已经结束的任务：如实告知，状态以轮询到的终态为准
            toast(res.message || '任务已结束，无需停止')
            return false
          }
          set(state => ({
            tasks: state.tasks.map(t =>
              t.id === id ? { ...t, status: 'CANCELLED', message: '已停止生成', queuePosition: undefined } : t,
            ),
          }))
          toast.success('已停止生成')
          return true
        } catch (e: any) {
          const msg = e?.msg || e?.detail || '停止失败'
          toast.error(typeof msg === 'string' ? msg : '停止失败')
          console.error('❌ 停止任务失败:', e)
          return false
        }
      },

      assignCategory: async (ids, categoryId) => {
        const unique = Array.from(new Set((ids || []).filter(Boolean)))
        if (unique.length === 0) return false
        const prev = new Map(get().tasks.map(t => [t.id, t.category]))
        const now = Date.now()
        unique.forEach(id => categoryWriteGuard.set(id, now))
        set(state => ({
          tasks: state.tasks.map(t =>
            unique.includes(t.id) ? { ...t, category: categoryId || undefined } : t,
          ),
        }))
        try {
          await assign_note_category(unique, categoryId)
          return true
        } catch (e: any) {
          set(state => ({
            tasks: state.tasks.map(t =>
              unique.includes(t.id) ? { ...t, category: prev.get(t.id) } : t,
            ),
          }))
          unique.forEach(id => categoryWriteGuard.delete(id))
          const msg = e?.msg || e?.detail || '归类失败'
          toast.error(typeof msg === 'string' ? msg : '归类失败')
          console.error('❌ 归类失败:', e)
          return false
        }
      },

      dropCategory: categoryId =>
        set(state => ({
          tasks: state.tasks.map(t =>
            t.category === categoryId ? { ...t, category: undefined } : t,
          ),
        })),

      dropLocalTasks: ids => {
        const drop = new Set(ids)
        if (drop.size === 0) return
        set(state => ({
          tasks: state.tasks.filter(t => !drop.has(t.id)),
          currentTaskId:
            state.currentTaskId && drop.has(state.currentTaskId) ? null : state.currentTaskId,
        }))
      },

      setCurrentTask: taskId => set({ currentTaskId: taskId }),

      openTask: taskId => {
        set({ currentTaskId: taskId })
        // 在点击处理器里就发请求，不等 Home 的 effect / 下一次 React 提交；
        // 已有正文会直接命中缓存，不会有网络往返。
        void get().ensureTaskContent(taskId)
      },

      ensureTaskContent: async (id, options) => {
        const task = get().tasks.find(t => t.id === id)
        if (!task) return
        const isActive = ACTIVE_STATUSES.includes(task.status)
        // 生成中也要取正文/历史版本：后端复用 task_id 时旧结果文件还在，
        // 新版本跑完前用户应当能继续看上一版（2026-10-25 用户反馈）。
        // 失败/停止但留有旧结果的任务同样要能打开历史版本。
        const canHaveHistory =
          isActive ||
          task.status === 'SUCCESS' ||
          task.status === 'FAILED' ||
          task.status === 'FAILD' ||
          task.status === 'CANCELLED'
        if (!canHaveHistory) return
        const hasContent = hasMarkdownContent(task.markdown)
        // 已有正文也可能只是「当前版」：本会话第一次打开时仍要拉一次版本列表，
        // 否则同一视频的旧版本（归档文件/旧 task）永远不会出现在下拉里。
        const needsVersions = !versionsLoaded.has(id)
        if (hasContent && !options?.force && !task.contentStale && !needsVersions) {
          if (task.contentLoading || task.contentError) {
            get().updateTaskContent(id, { contentLoading: false, contentError: undefined })
          }
          return
        }
        if (contentInFlight === id) return

        // 切笔记：旧请求立刻作废，旧响应不允许写进新笔记。
        contentAbort?.abort()
        const ctrl = new AbortController()
        contentAbort = ctrl
        const seq = ++contentSeq
        contentInFlight = id
        beginForegroundTask()
        get().updateTaskContent(id, { contentLoading: true, contentError: undefined })
        try {
          const [statusResult, versionsResult] = await Promise.allSettled([
            isActive
              ? Promise.resolve(null)
              : get_task_status(id, { view: 'note', signal: ctrl.signal }),
            get_task_versions(id),
          ])
          if (seq !== contentSeq || ctrl.signal.aborted) return
          const res: any = statusResult.status === 'fulfilled' ? statusResult.value : null
          const versions: any[] =
            versionsResult.status === 'fulfilled' && Array.isArray((versionsResult.value as any)?.versions)
              ? (versionsResult.value as any).versions
              : []
          if (versionsResult.status === 'fulfilled') versionsLoaded.add(id)
          if (res?.result) {
            // 已有正文时（显式重试/外部更新）必须 force 才能绕过 SUCCESS→SUCCESS 短路。
            get().applyServerResult(id, res.result, {
              status: 'SUCCESS',
              force: hasContent || !!options?.force || !!task.contentStale,
            })
          }
          // 归档版本 / 同视频旧任务：放在当前结果之后合并，后端元信息优先。
          if (versions.length) get().mergeServerVersions(id, versions)
          if (!res?.result && !versions.length && !hasContent) {
            // 首次生成且还没有任何历史版本：保持进度页；失败/停止的任务
            // 保持各自终态页，不要用「结果不可用」覆盖失败原因。
            if (isActive || task.status !== 'SUCCESS') return
            const msg =
              statusResult.status === 'rejected' && typeof (statusResult.reason as any)?.msg === 'string'
                ? (statusResult.reason as any).msg
                : '笔记结果暂不可用，请稍后重试'
            get().updateTaskContent(id, { contentLoading: false, contentError: msg })
            return
          }
          get().updateTaskContent(id, { contentStale: false })
        } catch (e: any) {
          if (seq !== contentSeq || ctrl.signal.aborted) return
          // 取消请求不是失败；已有正文时保留，不把整篇变成错误页。
          if (hasContent) return
          // 生成中/失败/停止的终态视图由各自分支负责，不在这里改成加载失败。
          if (isActive || task.status !== 'SUCCESS') return
          const msg = typeof e?.msg === 'string' ? e.msg : '笔记加载失败，请检查网络后重试'
          get().updateTaskContent(id, { contentLoading: false, contentError: msg })
        } finally {
          endForegroundTask()
          if (seq === contentSeq) {
            contentInFlight = null
            // 成功时正文已经写入；这里只负责收掉加载态（不会覆盖正文）。
            get().updateTaskContent(id, { contentLoading: false })
          }
        }
      },

      mergeServerVersions: (id, versions) => {
        const task = get().tasks.find(t => t.id === id)
        if (!task || !Array.isArray(versions) || versions.length === 0) return
        // 以正文为去重键：同一篇内容可能同时存在于「当前结果 / 归档文件 /
        // 同视频旧 task」，后端条目覆盖本地条目（它有稳定的 ver_id 和归档时间）。
        const byContent = new Map<string, Markdown>()
        const add = (v: any) => {
          const content = String(v?.content || '').trim()
          if (!content) return
          byContent.set(content, {
            ver_id: String(v?.ver_id || `${id}-server-${byContent.size}`),
            content,
            style: String(v?.style || ''),
            model_name: String(v?.model_name || ''),
            created_at: String(v?.created_at || ''),
          })
        }
        const prev = task.markdown
        if (Array.isArray(prev)) prev.forEach(add)
        else if (typeof prev === 'string' && prev.trim()) {
          add({ content: prev, created_at: task.createdAt })
        }
        versions.forEach(add)
        const merged = [...byContent.values()].sort(
          (a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0),
        )
        if (merged.length) get().updateTaskContent(id, { markdown: merged })
      },

      applyTranscript: (id, transcript) =>
        get().updateTaskContent(id, { transcript }),
    }),
    {
      name: 'task-storage',
      storage: createJSONStorage(() => ({
        getItem: async (name: string): Promise<string | null> => {
          const value = await get(name)
          return value ?? null
        },
        setItem: async (name: string, value: string): Promise<void> => {
          await set(name, value)
        },
        removeItem: async (name: string): Promise<void> => {
          await del(name)
        },
      })),
      // 只持久化卡片概要：正文/转录/raw_info 点开时按需拉取，避免每次
      // set 都同步 stringify 整个笔记库（手机上点一下历史卡就卡顿的根因）。
      partialize: state =>
        ({
          tasks: state.tasks.map(stripTaskForPersist),
          currentTaskId: state.currentTaskId,
        }) as any,
      version: 2,
      migrate: (persisted: any) => {
        if (!persisted || !Array.isArray(persisted.tasks)) return persisted as any
        return {
          ...persisted,
          tasks: persisted.tasks.map(stripTaskForPersist),
        } as any
      },
    }
  )
)
