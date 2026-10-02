import { useEffect, useRef } from 'react'
import toast from 'react-hot-toast'
import { get_video_meta } from '@/services/note.ts'
import { useTaskStore } from '@/store/taskStore'
import { useSystemStore } from '@/store/configStore'
import { sendOsClipboardNotify } from '@/utils/osClipboardNotify.ts'

/**
 * 剪贴板视频链接识别：命中支持的视频链接 → 解析标题封面 → 右下角弹窗
 * 「是否为该视频生成笔记」。
 *
 * 两档工作模式（设置 → 通用 里切换，zustand persist 持久化）：
 * 1. 默认（轮询关）：只在窗口回到前台（focus / visibilitychange）时读一次，
 *    零后台开销、零打扰；
 * 2. 轮询开：每 N 秒经 Rust 侧读一次系统剪贴板（Tauri clipboard-manager，
 *    不需要窗口焦点，后台也能读），复制链接后即使不切回窗口也能捕获；
 *    若此时窗口不在前台（失焦即算，最小化/被完全遮挡同理），候选先攒着、等回
 *    到前台再一次性弹窗；OS 通知开着时同步弹一条系统通知（不用切回应用也能看到）。
 *
 * 为什么只能轮询、没有"中断"：
 * 操作系统根本不提供剪贴板变更通知（Windows 只有 AddClipboardFormatListener
 * 这种窗口消息，且 WebView 拿不到；macOS/Linux 同理），官方
 * tauri-plugin-clipboard-manager 也只有 readText/writeText，没有变更事件。
 * 第三方 tauri-plugin-clipboard 的 listenText 本质也是 Rust 起后台线程定时
 * GetClipboardSequenceNumber 轮询——只是把轮询藏进了 Rust 侧。所以"中断式"
 * 在这条路上不存在，自己按需轮询是最直接、最少依赖的方案。
 *
 * 开销实测结论（见 commit message）：单次 readText 是一次 IPC + 一次 WinAPI
 * 调用（空剪贴板约 1ms 量级）；3 秒间隔下 CPU/内存占用可忽略，
 * 失败时还会指数退避（3s→6s→12s→…→上限 60s），读成功一次即复位。
 *
 * 约束（用户决策 + 平台限制）：
 * - 同一个链接只提示一次（内存 Set + sessionStorage 双记，避免 StrictMode
 *   双跑 effect 导致一次粘贴弹两次）；
 * - 已经在生成历史里的视频不再提示（查 extractVideoKey + 后端 video_id 双通道）；
 * - 读不到剪贴板（插件未注册/纯 Web 端）一律静默，不打扰；
 * - 点「生成笔记」只是把链接填进表单（setClipboardCandidate），不自动提交——
 *   模型/风格/截图开关仍由用户确认，避免误触扣费。
 */

const SEEN_KEY = 'bilinote-clipboard-seen'
const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

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

export interface ClipboardCandidate {
  url: string
  platform: string
  title?: string
  cover_url?: string
}

/**
 * 读剪贴板文本，双通道：
 * 1. Tauri 优先：经 Rust 读系统剪贴板，不需要窗口焦点——后台轮询就靠它；
 * 2. 回退 Web API：navigator.clipboard.readText() 需要焦点，无焦点直接返回 null
 *    （纯 Web 端 / 插件未注册时的兜底）。
 * 任何失败都返回 null，上层静默跳过。
 */
async function readClipboardText(): Promise<string | null> {
  if (isTauri) {
    try {
      const { readText } = await import('@tauri-apps/plugin-clipboard-manager')
      const text = await readText()
      if (text) return text
    } catch { /* 插件未就绪就往下走 Web 通道 */ }
  }
  try {
    if (!navigator.clipboard?.readText) return null
    // WebView2：document.hasFocus() 为 false 时读剪贴板会被拒，提前返回
    if (typeof document !== 'undefined' && !document.hasFocus()) return null
    const text = await navigator.clipboard.readText()
    return text || null
  } catch {
    return null
  }
}

export const useClipboardWatcher = (onCandidate: (info: ClipboardCandidate) => void) => {
  // 轮询开关/间隔/OS 通知开关走全局 store（设置页可改），订阅后即时生效
  const pollEnabled = useSystemStore(s => s.clipboardPollEnabled)
  const pollIntervalSec = useSystemStore(s => s.clipboardPollIntervalSec)
  const osNotifyEnabled = useSystemStore(s => s.clipboardOsNotifyEnabled)
  const seenRef = useRef<Set<string>>(loadSeen())
  const checkingRef = useRef(false)
  // 窗口不可见时命中的候选先攒在这里，回到前台再弹（攒多个就只弹最后一个）
  const pendingRef = useRef<ClipboardCandidate | null>(null)
  // 连续读取失败次数（指数退避用，成功一次即清零）
  const failStreakRef = useRef(0)
  const cbRef = useRef(onCandidate)
  cbRef.current = onCandidate

  useEffect(() => {
    let disposed = false

    /** 读一次剪贴板 → 命中新链接则解析标题封面。deferUnfocused=true 时窗口不
     *  在前台就只攒候选不弹窗（由回到前台的 flush 统一弹）。返回 true 表示读成功。 */
    const check = async (deferUnfocused = false): Promise<boolean> => {
      if (checkingRef.current || disposed) return true
      checkingRef.current = true
      try {
        const text = await readClipboardText()
        if (!text) return true
        const found = extractVideoUrl(text)
        if (!found) return true
        const seenKey = found.url
        if (seenRef.current.has(seenKey)) return true
        seenRef.current.add(seenKey)
        saveSeen(seenRef.current)
        if (alreadyHasVideo(found.url)) return true

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
        if (disposed) return true

        const candidate = { url: found.url, platform: found.platform, title, cover_url }
        // 焦点门控，不是可见性门控：BiliNote 没拿到前台焦点就该走 OS 通知 +
        // 攒候选。旧实现用 document.hidden（最小化/被完全遮挡才 true），窗口
        // 只是开在后台时，应用内弹窗弹在看不见的窗口里、OS 通知又被跳过，
        // 用户永远收不到提醒（2026-10-02 实测复现：注册表 LastNotificationAdded
        // Time 不更新、切回窗口弹窗已在）。document.hidden 保留作兜底：WebView2
        // 失焦瞬间 hasFocus() 可能仍短暂为 true。
        if (deferUnfocused && (document.hidden || !document.hasFocus())) {
          // 非前台命中的先攒着：回到前台时 flush 再弹应用内卡片，避免打扰；
          // 若 OS 通知开着，同时经系统通知中心弹一条纯文本——不用切回应用
          // 也能看到，点击通知回应用后 flush 弹出完整卡片。
          pendingRef.current = candidate
          if (osNotifyEnabled) {
            void sendOsClipboardNotify(candidate.title || candidate.url)
          }
        } else {
          cbRef.current(candidate)
        }
        return true
      } catch {
        return false
      } finally {
        checkingRef.current = false
      }
    }

    /** 把后台攒着的候选弹出来（回到前台时调用） */
    const flushPending = () => {
      if (pendingRef.current && !document.hidden) {
        cbRef.current(pendingRef.current)
        pendingRef.current = null
      }
    }

    const onFocus = () => { flushPending(); void check() }
    const onVisible = () => { if (!document.hidden) { flushPending(); void check() } }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisible)
    // 挂载后延迟首查：等后端就绪与首屏渲染完成，避免启动期弹通知
    const timer = setTimeout(() => void check(), 4000)

    // 轮询档：setTimeout 链（不用 setInterval），失败时指数退避
    // 3s→6s→12s→…→上限 60s，成功一次即复位——剪贴板读失败通常是瞬时的，
    // 退避只是避免某个坏状态下空转打扰。
    let pollTimer: number | undefined
    if (pollEnabled) {
      const baseMs = Math.min(Math.max(pollIntervalSec, 1), 60) * 1000
      const tick = async () => {
        if (disposed) return
        const ok = await check(true)
        failStreakRef.current = ok ? 0 : failStreakRef.current + 1
        const backoff = Math.min(2 ** failStreakRef.current, 20)
        pollTimer = window.setTimeout(tick, Math.min(baseMs * backoff, 60000))
      }
      pollTimer = window.setTimeout(tick, baseMs)
    }

    return () => {
      disposed = true
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisible)
      clearTimeout(timer)
      if (pollTimer !== undefined) clearTimeout(pollTimer)
    }
  }, [pollEnabled, pollIntervalSec, osNotifyEnabled])
}

// 独立 toast 弹窗（右下角，含封面+标题+「生成笔记/忽略」按钮）
// 封面走后端 image_proxy 代理：B 站 CDN 按 Referer 防盗链，WebView2 访问
// tauri.localhost 会带上自己的 Referer 直连拿 403（实测 3/3）；代理侧固定
// Referer: www.bilibili.com 直连 200。即使代理也失败，onError 隐藏封面
// 只留标题，不再留裂图占位。
export function notifyClipboardVideo(info: {
  url: string
  platform: string
  title?: string
  cover_url?: string
}, onAccept: (info: { url: string; platform: string }) => void) {
  const label = info.title || info.url
  // 封面必须走后端 image_proxy：B 站 CDN 按 Referer 防盗链，
  // 直链在 WebView2 里拿 403（实测 3/3），代理侧固定 Referer 才 200。
  const apiBase = String((import.meta as any).env?.VITE_API_BASE_URL || '/api').replace(/\/$/, '')
  const coverSrc = info.cover_url
    ? `${apiBase}/image_proxy?url=${encodeURIComponent(info.cover_url)}`
    : ''
  toast.custom(
    t => (
      <div
        className={`${
          t.visible ? 'animate-enter' : 'animate-leave'
        } pointer-events-auto flex w-full max-w-sm gap-3 rounded-lg border border-border bg-background p-3 shadow-lg`}
      >
        {coverSrc ? (
          <img
            src={coverSrc}
            alt="封面"
            referrerPolicy="no-referrer"
            onError={e => { e.currentTarget.style.display = 'none' }}
            className="h-14 w-20 shrink-0 rounded object-cover"
          />
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
