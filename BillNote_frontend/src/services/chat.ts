import request from '@/utils/request'

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatSource {
  text: string
  source_type: 'markdown' | 'transcript' | 'meta' | string
  task_id?: string
  note_title?: string
  section_title?: string
  start_time?: number
  end_time?: number
  /** 该来源是否被回答正文里的 [n] 角标实际引用（后端 _finalize_answer 打标） */
  cited?: boolean
}

export type ChatScope = 'current' | 'all'

export interface AskResponse {
  answer: string
  sources: ChatSource[]
}

export type IndexStatus = 'idle' | 'indexing' | 'indexed' | 'failed'

export interface ChatStatusResponse {
  indexed: boolean
  status: IndexStatus
}

/** 索引兜底内容：后端缺 note_results 源文件时（笔记只存在前端 IndexedDB），
 * 前端把自己持有的内容推给后端落盘再索引（2026-10-04 手机实机发现）。 */
export interface IndexNotePayload {
  markdown: string
  transcript?: {
    full_text?: string
    language?: string
    raw?: unknown
    segments?: unknown[]
  }
  audio_meta?: Record<string, unknown>
}

export const indexTask = async (taskId: string, note?: IndexNotePayload): Promise<void> => {
  return await request.post('/chat/index', { task_id: taskId, note })
}

export const askQuestion = async (data: {
  task_id: string
  question: string
  history: ChatMessage[]
  provider_id: string
  model_name: string
  scope?: ChatScope
}): Promise<AskResponse> => {
  return await request.post('/chat/ask', data, { timeout: 120000 })
}

export const getChatStatus = async (taskId: string): Promise<ChatStatusResponse> => {
  return await request.get(`/chat/status?task_id=${taskId}`)
}

export const getIndexedTaskIds = async (limit = 500): Promise<string[]> => {
  const res = await request.get(`/chat/indexed?limit=${limit}`)
  return (res?.task_ids ?? []) as string[]
}

export interface IndexCoverage {
  indexed: number
  total_notes: number
  missing: string[]
}

/** 覆盖率统计：已索引数 / 笔记总数 / 缺失 task_id（供提示条展示）。 */
export const getIndexCoverage = async (): Promise<IndexCoverage> => {
  return await request.get('/chat/coverage')
}

export const backfillGlobalIndex = async (): Promise<void> => {
  await request.post('/chat/backfill', {})
}
