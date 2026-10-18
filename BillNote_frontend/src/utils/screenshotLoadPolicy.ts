/**
 * 笔记原片截图的加载策略开关（2026-10-18 白屏根治配套）。
 *
 * 背景：单篇笔记可达数百张 1600px 截屏缩略（解码后约 5.8MB/张）。浏览器对
 * 「仍在 DOM 里」的 <img> 会一直保留解码位图，通读长篇笔记累计到数 GB，
 * WebView2 渲染进程被 OOM 杀掉整页白屏（logs/frontend_crash.log 里
 * reason=OUT_OF_MEMORY 连发；2026-10-17 只做了懒加载+缩略，装载不卸载，
 * 长笔记读完仍会堆爆）。NoteImage 按「离屏即卸 src」把常驻解码量压到
 * 视口附近十几张。
 *
 * 唯一的例外：长图导出要把整篇（含全部截图）画进一张图——capture 前打开
 * export 模式，强制所有截图挂上真 src 且禁止卸载，截完关掉。
 */
let exportMode = false
const listeners = new Set<(v: boolean) => void>()

export const setScreenshotExportMode = (v: boolean) => {
  if (exportMode === v) return
  exportMode = v
  listeners.forEach(l => {
    try {
      l(v)
    } catch {
      /* 监听器异常不影响其他订阅者 */
    }
  })
}

export const getScreenshotExportMode = () => exportMode

export const subscribeScreenshotExportMode = (l: (v: boolean) => void) => {
  listeners.add(l)
  return () => {
    listeners.delete(l)
  }
}
