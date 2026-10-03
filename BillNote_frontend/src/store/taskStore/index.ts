import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import { delete_task, generateNote } from '@/services/note.ts'
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

export interface AudioMeta {
  cover_url: string
  duration: number
  file_path: string
  platform: string
  raw_info: any
  title: string
  video_id: string
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
  markdown: string|Markdown [] //为了兼容之前的笔记
  transcript: Transcript
  status: TaskStatus
  audioMeta: AudioMeta
  /** 进行中提示 / 失败原因（后端 status 文件的 message） */
  message?: string
  /** 排队位次：PENDING 时由后端返回，展示「排队中 · 第 N 位」 */
  queuePosition?: number
  /** 任务来源：manual=界面提交，auto=自动化检查轮创建 */
  origin?: string
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
  updateTaskContent: (id: string, data: Partial<Omit<Task, 'id' | 'createdAt'>>) => void
  /** 局部合并 audioMeta（早期标题/封面 / 轮询到的元信息），不覆盖已有字段 */
  mergeTaskAudioMeta: (id: string, meta: Partial<AudioMeta>) => void
  /** 把后端发现的任务（如自动化任务）补进本地列表 */
  addBackendTask: (bt: any, result?: any) => void
  removeTask: (id: string) => void
  clearTasks: () => void
  setCurrentTask: (taskId: string | null) => void
  getCurrentTask: () => Task | null
  retryTask: (id: string) => void
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

      updateTaskContent: (id, data) =>
          set(state => ({
            tasks: state.tasks.map(task => {
              if (task.id !== id) return task

              if (task.status === 'SUCCESS' && data.status === 'SUCCESS') return task

              // 如果是 markdown 字符串，封装为版本
              if (typeof data.markdown === 'string') {
                const prev = task.markdown
                // 版本徽标用合并后的 formData：轮询/同步可能在同一 patch 里带来
                // 后端结果文件的 model_name/style（旧 task.formData 还是空的）。
                const fd = (data as any).formData || task.formData || {}
                const newVersion: Markdown = {
                  ver_id: `${task.id}-${uuidv4()}`,
                  content: data.markdown,
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
                  ...data,
                  markdown: updatedMarkdown,
                }
              }

              return { ...task, ...data }
            }),
          })),


      mergeTaskAudioMeta: (id, meta) =>
        set(state => ({
          tasks: state.tasks.map(task =>
            task.id === id ? { ...task, audioMeta: { ...task.audioMeta, ...meta } } : task
          ),
        })),

      addBackendTask: (bt: any, result?: any) =>
        set(state => {
          if (state.tasks.some(t => t.id === bt.task_id)) return state
          // 徽标数据源优先级：结果文件（model_name/provider_id/style）
          // ＞ /tasks/recent 概要（status 文件 PENDING 时写入）
          // ＞ 空。任何一层缺失都继续往下一层找，不再写死 ''。
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
          const task: Task = {
            id: bt.task_id,
            status: bt.status,
            message: bt.message || undefined,
            origin: bt.origin || 'manual',
            markdown: result?.markdown || '',
            transcript: result?.transcript || { full_text: '', language: '', raw: null, segments: [] },
            audioMeta: result?.audio_meta || {
              cover_url: bt.cover_url || '',
              duration: bt.duration || 0,
              file_path: '',
              platform: bt.platform || '',
              raw_info: null,
              title: bt.title || '',
              video_id: bt.video_id || '',
            },
            createdAt: new Date().toISOString(),
            formData,
          }
          return { tasks: [task, ...state.tasks] }
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
        try {
          await generateNote({
            ...newFormData,
            task_id: id,
          })
        } catch (e: any) {
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
            await delete_task({
              task_id: task.id,
              video_id: task.audioMeta?.video_id,
              platform:
                task.audioMeta?.platform || (task as any).platform || task.formData?.platform || 'bilibili',
            })
            toast.success('已删除该笔记')
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

      setCurrentTask: taskId => set({ currentTaskId: taskId }),
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
    }
  )
)
