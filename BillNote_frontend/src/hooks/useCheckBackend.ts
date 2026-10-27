import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/utils/apiFetch.ts'
import { isBackendAlive } from '@/utils/backendAliveProbe.ts'

// 后端就绪检测的时间常量
// - 单次探测超时 10s：远端手机经 Tailscale 首包可能慢，5s 会在资源下载
//   抢占连接时误杀（表现为「刷很多次才连上」）。
// - 失败后退避 2→3→5→8→10s，永不终态：Tailscale 路由晚几秒就绪也能
//   自动连上，不需要用户刷新页面。
// - 60s 后仍没连上只切「连接失败/仍在重试」UI，轮询继续；真正进程退出
//   由 Tauri 事件处理。
const PROBE_TIMEOUT_MS = 10_000
const DEGRADED_AFTER_MS = 60_000
const BACKOFF_MS = [2_000, 3_000, 5_000, 8_000, 10_000]

const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

async function probeSysCheck(): Promise<boolean> {
  const ctrl = new AbortController()
  const t = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS)
  try {
    const res = await apiFetch('/sys_check', { signal: ctrl.signal })
    if (!res.ok) return false
    const json = await res.json().catch(() => null)
    return json?.code === 0
  }
  catch {
    return false
  }
  finally {
    clearTimeout(t)
  }
}

interface Status {
  loading: boolean
  initialized: boolean
  failed: boolean
  lastError: string | null
}

interface BackendCheck extends Status {
  retry: () => void
}

const initialStatus: Status = {
  loading: true,
  initialized: false,
  failed: false,
  lastError: null,
}

/**
 * 后端就绪检测。
 *
 * 三路信号汇聚：
 *  1. HTTP 轮询 /sys_check —— 所有平台通用；失败只退避，不终态
 *  2. Tauri 'backend-ready' 事件 —— 桌面端 sidecar 探测器先于 HTTP 一步触达
 *  3. Tauri 'backend-terminated' / 'backend-startup-timeout' 事件 —— sidecar
 *     真死了才终态，避免「本轮超时 = 永远失败，只能刷新」
 *
 * 任何一路报「ready」即成功；只有 Tauri 确认进程退出才停止轮询。
 */
export const useCheckBackend = (): BackendCheck => {
  const [status, setStatus] = useState<Status>(initialStatus)
  // tick 用来强制重启 useEffect（retry 时 +1），不引入 ref 互斥逻辑的复杂性
  const [tick, setTick] = useState(0)
  // 标记当前 effect 是否已 settle（避免后到的事件覆盖已确定的成功/失败态）
  const settledRef = useRef(false)

  const retry = useCallback(() => {
    settledRef.current = false
    setStatus(initialStatus)
    setTick((t) => t + 1)
  }, [])

  useEffect(() => {
    let pollTimerId: ReturnType<typeof setTimeout> | null = null
    let cancelled = false
    const tauriUnsubs: Array<() => void> = []
    const startedAt = Date.now()
    let attempt = 0

    const markReady = () => {
      if (cancelled || settledRef.current) return
      settledRef.current = true
      setStatus({ loading: false, initialized: true, failed: false, lastError: null })
    }

    const markFailed = (msg: string) => {
      if (cancelled || settledRef.current) return
      settledRef.current = true
      setStatus({ loading: false, initialized: false, failed: true, lastError: msg })
    }

    const poll = async () => {
      if (cancelled || settledRef.current) return
      const ok = await probeSysCheck()
      if (cancelled || settledRef.current) return
      if (ok) {
        markReady()
        return
      }
      // 单次失败不报 toast、不抛错，继续轮询；60s 后只换提示，仍继续。
      const elapsed = Date.now() - startedAt
      if (elapsed >= DEGRADED_AFTER_MS) {
        setStatus({
          loading: false,
          initialized: false,
          failed: true,
          lastError: `后端连接较慢，正在自动重试（已等待 ${Math.round(elapsed / 1000)}s）`,
        })
      } else {
        setStatus((s) => ({ ...s, lastError: '后端尚未响应，正在重试…' }))
      }
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]
      attempt += 1
      pollTimerId = setTimeout(poll, delay)
    }

    // 桌面端订阅 Tauri 事件（动态 import 避免 web 端打包报错）
    if (isTauri) {
      import('@tauri-apps/api/event')
        .then(async ({ listen }) => {
          if (cancelled) return
          const offReady = await listen<number>('backend-ready', () => markReady())
          const offTimeout = await listen<string>('backend-startup-timeout', (e) => {
            markFailed(typeof e.payload === 'string' ? e.payload : '后端启动超时')
          })
          const offTerm = await listen<number | null>('backend-terminated', (e) => {
            const code = e.payload
            // 健康门控：sidecar 退了但 /sys_check 仍 200（收编的孤儿后端在服务）
            // → 按就绪处理，不进失败态（2026-10-06 假「后端进程已退出」横幅）
            void isBackendAlive().then((alive) => {
              if (alive) markReady()
              else markFailed(`后端进程已退出 (code=${code ?? 'unknown'})`)
            })
          })
          tauriUnsubs.push(offReady, offTimeout, offTerm)
        })
        .catch((err) => {
          // 拿不到 @tauri-apps/api/event 不致命，继续走 HTTP 轮询
          console.warn('[useCheckBackend] 无法订阅 Tauri 事件:', err)
        })
    }

    // 立刻开始第一轮轮询
    poll()

    return () => {
      cancelled = true
      if (pollTimerId) clearTimeout(pollTimerId)
      tauriUnsubs.forEach((off) => {
        try {
          off()
        } catch {
          /* noop */
        }
      })
    }
  }, [tick])

  return { ...status, retry }
}
