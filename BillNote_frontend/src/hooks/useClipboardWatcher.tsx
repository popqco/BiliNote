import { useEffect, useRef } from 'react'
import toast from 'react-hot-toast'
import { get_video_meta } from '@/services/note.ts'
import { useTaskStore } from '@/store/taskStore'

/**
 * 剪贴板视频链接识别：在应用回到前台（focus / visibilitychange）时读一次剪贴板，
 * 命中支持的视频链接 → 解析标题封面 → 右下角弹窗提示「是否为该视频生成笔记」。
 *
 * 约束（用户决策 + 平台限制）：
 * - 只在 focus/visible 时读，不轮询：WebView 里 navigator.clipboard.readText()
 *   需要「用户手势/焦点」上下文，后台定时读会被浏览器拒绝，还耗电；
 * - 同一个链接只提示一次（内存 Set + sessionStorage 双记，避免 StrictMode
 *   双跑 effect 导致一次粘贴弹两次）；
 * - 已经在生成历史里的视频不再提示（查 extractVideoKey + 后端 video_id 双通道）；
 * - 读不到剪贴板（无权限/非 Tauri 焦点丢失）一律静默，不打扰；
 * - 点「生成笔记」只是把链接填进表单（setClipboardCandidate），不自动提交——
 *   模型/风格/截图开关仍由用户确认，避免误触扣费。
 */

const SEEN_KEY = 'bilinote-clipboard-seen'

function loadSeen(): Set<string> {
  try {
    const raw = sessionStorage.getItem(SEEN_KEY)
    if (raw) return new Set(JSON.parse(raw) as string[])
  } catch { /* 忽略 */ }
  return new Set()
}

function saveSeen(seen: Set<string>) {
  try {
    // 只保留最近 50 条，避免无限膨胀
    const arr = [...seen].slice(-50)
    sessionStorage.setItem(SEEN_KEY, JSON.stringify(arr))
  } catch { /* 忽略 */ }
}

/** 从一段文本里提取第一个支持的视频链接（含 b23.tv 短链与 BV 号裸文本） */
export function extractVideoUrl(text: string): { url: string; platform: string } | null {
  const src = String(text || '').trim()
  if (!src || src.length > 2000) return null
  // 完整 URL（含 b23.tv 短链）
  const urlMatch = src.match(/https?:\/\/[^\s"'<>]+/i)
  if (urlMatch) {
    const url = urlMatch[0].replace(/[.,;!?)\]]+$/, '')
    if (/bilibili\.com|b23\.tv/i.test(url)) return { url, platform: 'bilibili' }
    if (/youtube\.com|youtu\.be/i.test(url)) return { url, platform: 'youtube' }
    if (/douyin\.com/i.test(url)) return { url, platform: 'douyin' }
  }
  // BV 号裸文本（如从聊天记录复制的 "BV1xx411c7mD"）
  const bvMatch = src.match(/BV[0-9A-Za-z]{10}/)
  if (bvMatch) return { url: `https://www.bilibili.com/video/${bvMatch[0]}`, platform: 'bilibili' }
  return null
}

/** 生成历史里是否已有该视频（按 BV key + 后端 video_id 双通道查） */
function alreadyHasVideo(url: string): boolean {
  const m = String(url).match(/BV[0-9A-Za-z]{10}/)
  const key = m ? m[0] : String(url).trim().replace(/\/+$/, '')
  return useTaskStore.getState().tasks.some(t => {
    const formUrl = String((t.formData as any)?.video_url || '')
    if (formUrl) {
      const fm = formUrl.match(/BV[0-9A-Za-z]{10}/)
      const fkey = fm ? fm[0] : formUrl.trim().replace(/\/+$/, '')
      if (fkey === key) return true
    }
    const vid = (t.audioMeta as any)?.video_id
    if (vid && m && vid === m[0]) return true
    return false
  })
}

async function readClipboardText(): Promise<string | null> {
  try {
    if (!navigator.clipboard?.readText) return null
    // Tauri WebView2：document.hasFocus() 为 false 时读剪贴板会被拒，提前返回
    if (typeof document !== 'undefined' && !document.hasFocus()) return null
    const text = await navigator.clipboard.readText()
    return text || null
  } catch {
    return null
  }
}

export const useClipboardWatcher = (onCandidate: (info: {
  url: string
  platform: string
  title?: string
  cover_url?: string
}) => void) => {
  const seenRef = useRef<Set<string>>(loadSeen())
  const checkingRef = useRef(false)
  const cbRef = useRef(onCandidate)
  cbRef.current = onCandidate

  useEffect(() => {
    const check = async () => {
      if (checkingRef.current || document.hidden) return
      checkingRef.current = true
      try {
        const text = await readClipboardText()
        if (!text) return
        const found = extractVideoUrl(text)
        if (!found) return
        const seenKey = found.url
        if (seenRef.current.has(seenKey)) return
        seenRef.current.add(seenKey)
        saveSeen(seenRef.current)
        if (alreadyHasVideo(found.url)) return

        // 解析标题/封面（不下载，秒级；失败则用裸链接提示，不阻塞）
        let title: string | undefined
        let cover_url: string | undefined
        try {
          const meta: any = await get_video_meta(found.url, found.platform)
          if (meta?.title) {
            title = meta.title
            cover_url = meta.cover_url
          }
        } catch { /* 解析失败就用裸链接提示 */ }

        cbRef.current({ url: found.url, platform: found.platform, title, cover_url })
      } finally {
        checkingRef.current = false
      }
    }

    const onFocus = () => { void check() }
    const onVisible = () => { if (!document.hidden) void check() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisible)
    // 挂载后延迟首查：等后端就绪与首屏渲染完成，避免启动期弹通知
    const timer = setTimeout(check, 4000)
    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisible)
      clearTimeout(timer)
    }
  }, [])
}

// 独立 toast 弹窗（右下角，含封面+标题+「生成笔记/忽略」按钮）
export function notifyClipboardVideo(info: {
  url: string
  platform: string
  title?: string
  cover_url?: string
}, onAccept: (info: { url: string; platform: string }) => void) {
  const label = info.title || info.url
  toast.custom(
    t => (
      <div
        className={`${
          t.visible ? 'animate-enter' : 'animate-leave'
        } pointer-events-auto flex w-full max-w-sm gap-3 rounded-lg border border-border bg-background p-3 shadow-lg`}
      >
        {info.cover_url ? (
          <img src={info.cover_url} alt="封面" className="h-14 w-20 shrink-0 rounded object-cover" />
        ) : null}
        <div className="min-w-0 flex-1">
          <div className="text-xs font-medium text-muted-foreground">剪贴板发现视频链接</div>
          <div className="line-clamp-2 text-sm font-semibold">{label}</div>
          <div className="mt-2 flex gap-2">
            <button
              className="rounded bg-primary px-2.5 py-1 text-xs text-primary-foreground hover:opacity-90"
              onClick={() => {
                toast.dismiss(t.id)
                onAccept({ url: info.url, platform: info.platform })
              }}
            >
              生成笔记
            </button>
            <button
              className="rounded border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-muted"
              onClick={() => toast.dismiss(t.id)}
            >
              忽略
            </button>
          </div>
        </div>
      </div>
    ),
    { duration: 12000, position: 'bottom-right' },
  )
}
