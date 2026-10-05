/**
 * 统一的「保存文件」出口：浏览器走 a[download]，Tauri 走原生另存为对话框。
 *
 * 为什么需要：Tauri v2 的 WebView 默认拦截网页发起的下载（未注册下载处理器时
 * blob: 锚点点击静默失败）——思维导图 PNG/SVG/XMind/HTML 与长图/海报导出在
 * 桌面 App 里"点了没反应"的根因（2026-10-05 定位，浏览器里一切正常）。
 * 用户选择：桌面端每次弹系统另存为对话框（可自选路径）。
 *
 * 注意 @tauri-apps/api 系只能在 Tauri 环境动态 import（见 tauri-static-import
 * 教训：顶层静态 import 会让纯浏览器构建直接白屏）。
 */
export const isTauri = (): boolean =>
  typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

/** 保存成功返回保存路径；用户取消对话框返回 null；浏览器环境返回 undefined（已走下载） */
export async function saveBlob(blob: Blob, filename: string): Promise<string | null | undefined> {
  if (!isTauri()) {
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
    return undefined
  }
  const { save } = await import('@tauri-apps/plugin-dialog')
  const ext = filename.includes('.') ? filename.split('.').pop()! : ''
  const filters = ext ? [{ name: ext.toUpperCase(), extensions: [ext] }] : undefined
  const path = await save({ defaultPath: filename, filters })
  if (!path) return null // 用户取消
  const { writeFile } = await import('@tauri-apps/plugin-fs')
  await writeFile(path, new Uint8Array(await blob.arrayBuffer()))
  return path
}
