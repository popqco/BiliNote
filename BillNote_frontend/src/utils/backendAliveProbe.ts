import { resolveApiBaseUrl } from '@/utils/workerConnection.ts'

// backend-terminated 的健康门控：sidecar 进程死了 ≠ 后端不可用。
// 8483 被健康后端服务时（孤儿后端被收编 / 手动拉起的后端 / 另一个 app 实例），
// 「后端进程已退出」横幅与红点是误报——一切以 /sys_check 是否 200 为准。
// 判定口径与 useCheckBackend 一致：HTTP 200 且 body.code === 0。
export async function isBackendAlive(timeoutMs = 2500): Promise<boolean> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(`${resolveApiBaseUrl()}/sys_check`, { signal: ctrl.signal })
    if (!res.ok) return false
    const json = await res.json().catch(() => null)
    return json?.code === 0
  }
  catch {
    return false
  }
  finally {
    clearTimeout(timer)
  }
}
