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

  // 应用外系统通知：轮询在后台命中链接时，经 OS 通知中心弹一条
  //（应用不在前台也能看到）；点击通知回到应用，应用内卡片再弹出。
  // 默认开；关掉后后台命中只攒着、回前台才提示。纯文本通知，不带封面。
  clipboardOsNotifyEnabled: boolean
  setClipboardOsNotifyEnabled: (value: boolean) => void

  // 阅读区平滑滚动（滚轮惯性，Lenis）：只影响桌面鼠标/触控板，手机触摸
  // 本来就是原生惯性。关闭则回到浏览器原生滚动（一格一顿）。
  smoothScrollEnabled: boolean
  setSmoothScrollEnabled: (value: boolean) => void
  smoothScrollTier: SmoothScrollTier
  setSmoothScrollTier: (value: SmoothScrollTier) => void
}

// 跟手：滑行短、停得快；适中：连贯不飘；动量：接近手机松手后的滑行
export type SmoothScrollTier = 'direct' | 'medium' | 'momentum'
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

      clipboardOsNotifyEnabled: true,
      setClipboardOsNotifyEnabled: value => set({ clipboardOsNotifyEnabled: value }),

      smoothScrollEnabled: true,
      setSmoothScrollEnabled: value => set({ smoothScrollEnabled: value }),
      smoothScrollTier: 'medium',
      setSmoothScrollTier: value => set({ smoothScrollTier: value }),
    }),
    {
      name: 'system-store', // 本地存储的 key
    }
  )
)
