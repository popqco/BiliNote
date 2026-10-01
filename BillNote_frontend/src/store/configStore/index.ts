import { create } from 'zustand'
import { persist } from 'zustand/middleware'
interface SystemState {
  showFeatureHint: boolean // ✅ 是否显示功能提示
  setShowFeatureHint: (value: boolean) => void

  // 后续如果有其他全局状态，可以继续加
  sidebarCollapsed: boolean // ✅ 侧边栏是否收起
  setSidebarCollapsed: (value: boolean) => void

  // 剪贴板识别：默认只在窗口回到前台时检查一次（省电零打扰）；
  // 打开轮询后，每隔 N 秒经 Rust 侧读一次系统剪贴板（无需焦点），
  // 复制链接后即使不切回窗口也能捕获，切回时弹窗。
  clipboardPollEnabled: boolean
  setClipboardPollEnabled: (value: boolean) => void
  clipboardPollIntervalSec: number
  setClipboardPollIntervalSec: (value: number) => void
}
// 暂不启用
export const useSystemStore = create<SystemState>()(
  persist(
    set => ({
      showFeatureHint: true,
      setShowFeatureHint: value => set({ showFeatureHint: value }),

      sidebarCollapsed: false,
      setSidebarCollapsed: value => set({ sidebarCollapsed: value }),

      clipboardPollEnabled: false,
      setClipboardPollEnabled: value => set({ clipboardPollEnabled: value }),
      clipboardPollIntervalSec: 3,
      setClipboardPollIntervalSec: value => set({ clipboardPollIntervalSec: value }),
    }),
    {
      name: 'system-store', // 本地存储的 key
    }
  )
)
