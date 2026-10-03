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

export const indexTask = async (taskId: string): Promise<void> => {
  return await request.post('/chat/index', { task_id: taskId })
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

export const getIndexedTaskIds = async (): Promise<string[]> => {
  const res = await request.get('/chat/indexed')
  return (res?.task_ids ?? []) as string[]
}

export const backfillGlobalIndex = async (): Promise<void> => {
  await request.post('/chat/backfill', {})
}
