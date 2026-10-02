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
  addMessage: (taskId: string, msg: ChatMessage) => void
  clearChat: (taskId: string) => void
  getMessages: (taskId: string) => ChatMessage[]
}

export const chatKey = (taskId: string, scope: ChatScope) =>
  scope === 'all' ? 'all' : taskId

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      chatHistory: {},
      scope: 'all',

      setScope: (scope) => set({ scope }),

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
      // scope 也持久化：用户选过“当前笔记”后下次打开保持选择
      partialize: (state) => ({ chatHistory: state.chatHistory, scope: state.scope }) as ChatState,
    },
  ),
)
