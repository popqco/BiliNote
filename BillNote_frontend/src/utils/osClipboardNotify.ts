/**
 * 应用外系统通知（OS 通知中心）：轮询在后台命中视频链接、而 BiliNote
 * 不在前台时，经系统通知中心弹一条纯文本提醒，用户不用切回应用就能看到。
 *
 * 为什么不用 window.Notification：
 * WebView2 对通知权限默认拒绝，tauri.localhost 下 requestPermission()
 * 直接回 denied（连系统弹窗都不弹，实测 REQ-RESULT:denied），且 denied
 * 是粘性的——Windows 设置里永远不会出现 BiliNote，这条路判死刑。
 *
 * 实现方式：调 tauri-plugin-notification 的 `notify` 命令（Rust 侧
 * notify-rust → Windows Toast）。插件注册后覆写了 window.Notification，
 * 但这里直接 invoke 命令字符串，不依赖那层 JS shim：
 * - 桌面端 request_permission 恒返回 Granted，无需系统授权；
 * - 署名走 tauri.conf.json 的 identifier，首次发送后 Windows 会把
 *   BiliNote 注册为通知发送方，设置里就能看到并开关；
 * - 桌面端点击 Toast 由 OS 按 AppUserModelID 聚焦应用窗口（若快捷方式
 *   匹配），聚焦后前台 focus 事件把攒着的应用内卡片弹出来——
 *   Rust notify 本身不支持点击回调，所以这里没有 onclick。
 *
 * 失败一律静默返回 false，不打扰主流程。
 */
const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

export async function ensureOsNotifyPermission(): Promise<boolean> {
  try {
    if (isTauri) {
      // 桌面端：插件恒返回 Granted；同时验证 notify 命令通路正常
      const { invoke } = await import('@tauri-apps/api/core')
      const state = await invoke<string>('plugin:notification|request_permission')
      return state === 'granted'
    }
    if (typeof window === 'undefined' || !('Notification' in window)) return false
    if (window.Notification.permission === 'granted') return true
    if (window.Notification.permission === 'denied') return false
    const res = await window.Notification.requestPermission()
    return res === 'granted'
  } catch {
    return false
  }
}

export async function sendOsClipboardNotify(titleOrUrl: string): Promise<boolean> {
  try {
    const body = String(titleOrUrl || '').slice(0, 120)
    if (!body) return false
    if (isTauri) {
      const { invoke } = await import('@tauri-apps/api/core')
      await invoke('plugin:notification|notify', {
        options: { title: 'BiliNote · 剪贴板发现视频链接', body },
      })
      return true
    }
    if (!(await ensureOsNotifyPermission())) return false
    const n = new window.Notification('BiliNote · 剪贴板发现视频链接', { body })
    n.onclick = () => {
      try {
        window.focus()
      } catch { /* 忽略 */ }
    }
    return true
  } catch {
    return false
  }
}
