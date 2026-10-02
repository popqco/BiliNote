/**
 * 应用外系统通知（OS 通知中心）：轮询在后台命中视频链接、而 BiliNote
 * 不在前台时，经系统通知中心弹一条纯文本提醒，用户不用切回应用就能看到。
 *
 * 实现方式：直接用 WebView2 自带的 `window.Notification`（Chromium 原生，
 * 在 Windows 上即走系统 Toast；有开始菜单快捷方式 BiliNote.lnk，
 * 通知会正确署名 BiliNote）。这正是 @tauri-apps/plugin-notification 的
 * sendNotification 底层做的事（它的实现就是 `new window.Notification(...)`），
 * 这里自己构造实例是为了拿到对象挂 `onclick`——点击通知把应用窗口拉回前台，
 * 前台 focus 事件会把攒着的应用内卡片（含封面+「生成笔记」按钮）弹出来。
 *
 * 失败一律静默返回 false，不打扰主流程。
 */

export async function ensureOsNotifyPermission(): Promise<boolean> {
  try {
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
    if (!(await ensureOsNotifyPermission())) return false
    const body = String(titleOrUrl || '').slice(0, 120)
    if (!body) return false
    const n = new window.Notification('BiliNote · 剪贴板发现视频链接', { body })
    n.onclick = () => {
      try {
        window.focus()
      } catch { /* 忽略 */ }
      // 把 Tauri 窗口拉到前台（非 Tauri 纯 Web 端会走 catch 忽略）
      import('@tauri-apps/api/window')
        .then(({ getCurrentWindow }) => getCurrentWindow().setFocus())
        .catch(() => {})
    }
    return true
  } catch {
    return false
  }
}
