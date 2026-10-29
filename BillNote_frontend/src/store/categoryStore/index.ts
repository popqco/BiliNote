import { create } from 'zustand'
import toast from 'react-hot-toast'

import {
  NoteCategory,
  create_note_category,
  delete_note_category,
  get_note_categories,
  rename_note_category,
} from '@/services/categories'
import { useTaskStore } from '@/store/taskStore'

/** 分类列表的全局缓存：历史列表筛选、卡片徽标、阅读页徽标、自动化设置共用一份。
 *
 * 每篇笔记的归属存在 Task.category（来自 /tasks/recent），这里只管分类本身。
 * 错误文案由请求拦截器统一 toast，store 只负责返回失败信号。
 */
interface CategoryStore {
  categories: NoteCategory[]
  loading: boolean
  loaded: boolean
  load: (force?: boolean) => Promise<void>
  create: (name: string) => Promise<NoteCategory | null>
  rename: (id: string, name: string) => Promise<boolean>
  remove: (id: string) => Promise<boolean>
  /** 批量归类（走 taskStore 的乐观更新），返回是否成功 */
  assign: (taskIds: string[], categoryId: string | null) => Promise<boolean>
}

export const useCategoryStore = create<CategoryStore>((set, get) => ({
  categories: [],
  loading: false,
  loaded: false,

  load: async force => {
    if (get().loading) return
    if (get().loaded && !force) return
    set({ loading: true })
    try {
      const res = await get_note_categories()
      set({ categories: res?.categories || [], loaded: true })
    } catch {
      // 拉取失败（后端未启动/未配对）保持已有列表，下次再试
    } finally {
      set({ loading: false })
    }
  },

  create: async name => {
    const trimmed = String(name || '').trim()
    if (!trimmed) {
      toast.error('分类名称不能为空')
      return null
    }
    try {
      const cat = await create_note_category(trimmed)
      set(state => ({ categories: [...state.categories, cat] }))
      return cat
    } catch {
      return null
    }
  },

  rename: async (id, name) => {
    const trimmed = String(name || '').trim()
    if (!trimmed) {
      toast.error('分类名称不能为空')
      return false
    }
    try {
      const cat = await rename_note_category(id, trimmed)
      set(state => ({
        categories: state.categories.map(c => (c.id === id ? { ...c, ...cat } : c)),
      }))
      return true
    } catch {
      return false
    }
  },

  remove: async id => {
    try {
      await delete_note_category(id)
      set(state => ({ categories: state.categories.filter(c => c.id !== id) }))
      // 本地同步：该分类下的卡片立刻显示为未分类（后端已断开关联）
      useTaskStore.getState().dropCategory(id)
      return true
    } catch {
      return false
    }
  },

  assign: async (taskIds, categoryId) => useTaskStore.getState().assignCategory(taskIds, categoryId),
}))

/** 便捷：按分类 id 取分类（含删除后的兜底） */
export const findCategory = (
  categories: NoteCategory[],
  id?: string | null,
): NoteCategory | undefined => (id ? categories.find(c => c.id === id) : undefined)
