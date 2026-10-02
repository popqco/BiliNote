import { create } from 'zustand'
import { persist } from 'zustand/middleware'

/**
 * 自定义风格预设：把 NoteForm「备注」(extras) 存成可复用、可一键填入的
 * 个人风格/调性（对标在线版"自定义你自己的调性"）。
 *
 * 存储范式照抄 `noteOptionsStore`（zustand persist → localStorage），
 * 但 key 独立（`custom-style-presets`），与"最后一次使用的选项"互不干扰：
 * 备注框内容本身仍不进 last-note-options（内容不记设置才记）。
 *
 * 纯前端 store，后端不用改：填入后走原有 extras 透传链路参与生成。
 */
export interface CustomStylePreset {
  id: string
  name: string
  content: string
  createdAt: number
  updatedAt: number
}

interface CustomStylePresetState {
  presets: CustomStylePreset[]
  /** 同名视为覆盖（保留原 id，只更新内容与 updatedAt），否则新增 */
  upsertPreset: (name: string, content: string) => CustomStylePreset
  removePreset: (id: string) => void
}

const makeId = () =>
  `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`

export const useCustomStylePresetStore = create<CustomStylePresetState>()(
  persist(
    (set, get) => ({
      presets: [],
      upsertPreset: (name, content) => {
        const trimmedName = name.trim()
        const trimmedContent = content.trim()
        const now = Date.now()
        const existing = get().presets.find(p => p.name === trimmedName)
        if (existing) {
          const updated: CustomStylePreset = {
            ...existing,
            content: trimmedContent,
            updatedAt: now,
          }
          set(state => ({
            presets: state.presets.map(p => (p.id === existing.id ? updated : p)),
          }))
          return updated
        }
        const created: CustomStylePreset = {
          id: makeId(),
          name: trimmedName,
          content: trimmedContent,
          createdAt: now,
          updatedAt: now,
        }
        set(state => ({ presets: [...state.presets, created] }))
        return created
      },
      removePreset: id =>
        set(state => ({ presets: state.presets.filter(p => p.id !== id) })),
    }),
    {
      name: 'custom-style-presets', // localStorage key
    }
  )
)
