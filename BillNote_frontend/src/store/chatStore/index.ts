import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { ChatSource } from '@/services/chat'

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  sources?: ChatSource[]
}

export type ChatScope = 'current' | 'all'

interface ChatState {
  /** 按“笔记 + 范围”分桶：全部笔记问答共用 'all' 桶，单篇问答按 taskId 分桶 */
  chatHistory: Record<string, ChatMessage[]>
  /** 当前问答范围（默认跨全部笔记，对标“第二大脑”） */
  scope: ChatScope
  setScope: (scope: ChatScope) => void
  /** 问答选用的模型（model_name），全局生效、持久化；优先级高于任务卡片自带配置 */
  chatModelName: string
  setChatModelName: (m: string) => void
  addMessage: (taskId: string, msg: ChatMessage) => void
  clearChat: (taskId: string) => void
  getMessages: (taskId: string) => ChatMessage[]
}

export const chatKey = (taskId: string, scope: ChatScope) =>
  scope === 'all' ? 'all' : taskId

/** 问答来源跳转目标：切到哪篇笔记、定位到哪个章节/转录时间。 */
export interface SourceJumpTarget {
  task_id: string
  /** markdown 来源的章节标题（与索引 section_title 一致） */
  section_title?: string
  /** transcript 来源的起始秒数 */
  start_time?: number
  nonce: number
}

interface ChatJumpState {
  jumpTarget: SourceJumpTarget | null
  requestJump: (t: Omit<SourceJumpTarget, 'nonce'>) => void
  consumeJump: () => void
}

export const useChatJumpStore = create<ChatJumpState>()((set) => ({
  jumpTarget: null,
  requestJump: (t) => set({ jumpTarget: { ...t, nonce: Date.now() } }),
  consumeJump: () => set({ jumpTarget: null }),
}))

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      chatHistory: {},
      scope: 'all',
      chatModelName: '',

      setScope: (scope) => set({ scope }),
      setChatModelName: (m) => set({ chatModelName: m }),

      addMessage: (taskId, msg) =>
        set(state => ({
          chatHistory: {
            ...state.chatHistory,
            [taskId]: [...(state.chatHistory[taskId] || []), msg],
          },
        })),

      clearChat: (taskId) =>
        set(state => {
          const { [taskId]: _, ...rest } = state.chatHistory
          return { chatHistory: rest }
        }),

      getMessages: (taskId) => get().chatHistory[taskId] || [],
    }),
    {
      name: 'bilinote-chat-storage',
      // scope / chatModelName 也持久化：用户选过“当前笔记”或问答模型后下次打开保持选择
      partialize: (state) => ({
        chatHistory: state.chatHistory,
        scope: state.scope,
        chatModelName: state.chatModelName,
      }) as ChatState,
    },
  ),
)
