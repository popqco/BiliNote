import { create } from 'zustand'
import { persist } from 'zustand/middleware'

/**
 * 生成笔记表单「最后一次使用的选项」：表单任意设置类字段变化即落盘
 * （zustand persist → localStorage `last-note-options`），用途：
 * 1. 新建笔记 / 应用重启后的表单默认值——用户不再需要每次重设
 *    （2026-10-02 反馈：采样间隔、笔记格式、视频理解每次都被打回默认）；
 * 2. 点击历史卡片时缺失字段的回退值——旧任务的 formData 里可能没有
 *    视频理解/笔记格式这些后加的字段，不能回退到硬编码出厂默认。
 *
 * 只存「设置」不存「内容」：视频链接、平台、备注不进这里。
 */
export interface LastNoteOptions {
  model_name: string
  style: string
  quality: 'fast' | 'medium' | 'slow'
  format: string[]
  video_understanding: boolean
  video_interval: number
  grid_size: [number, number]
}

interface NoteOptionsState {
  last: LastNoteOptions | null
  setLast: (v: LastNoteOptions) => void
}

export const useNoteOptionsStore = create<NoteOptionsState>()(
  persist(
    set => ({
      last: null,
      setLast: v => set({ last: v }),
    }),
    {
      name: 'last-note-options', // localStorage key
    }
  )
)

/** 表单值 → 可持久化选项：字段缺失/类型漂移（输入框给字符串等）都在这里兜平 */
export function pickLastNoteOptions(v: {
  model_name?: string
  style?: string
  quality?: 'fast' | 'medium' | 'slow'
  format?: string[]
  video_understanding?: boolean
  video_interval?: number
  grid_size?: [number, number]
}): LastNoteOptions {
  return {
    model_name: v.model_name || '',
    style: v.style || 'minimal',
    quality: v.quality || 'medium',
    format: [...(v.format || [])],
    video_understanding: !!v.video_understanding,
    video_interval: Number(v.video_interval) || 6,
    grid_size: [Number(v.grid_size?.[0]) || 2, Number(v.grid_size?.[1]) || 2],
  }
}
