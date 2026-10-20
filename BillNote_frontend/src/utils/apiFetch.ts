/**
 * 裸 fetch 的统一出口。
 *
 * 只做三件事：按当前配对地址拼 URL、带配对 token、允许覆盖 base（配对页在
 * 保存前要探任意 Worker 地址）。故意不解析 {code,msg,data} 信封——健康检查
 * 与配对页需要看到真实 HTTP 状态，而且不希望触发共享 axios 的全局 toast。
 */
import { loadWorkerConnection, resolveApiBaseUrl } from '@/utils/workerConnection.ts'

export interface ApiFetchOptions extends RequestInit {
  /** 覆盖 API 根（配对页保存前探任意地址）；默认 resolveApiBaseUrl() */
  baseUrl?: string
  /** 探活/配对校验等公共接口：不自动带配对 token */
  noAuth?: boolean
}

export function apiUrl(path: string, baseUrl?: string): string {
  if (/^https?:\/\//i.test(path)) return path
  const base = (baseUrl || resolveApiBaseUrl()).replace(/\/$/, '')
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

export async function apiFetch(path: string, init: ApiFetchOptions = {}): Promise<Response> {
  const { baseUrl, noAuth, headers, ...rest } = init
  const merged = new Headers(headers || {})
  if (!noAuth) {
    const token = loadWorkerConnection()?.token
    if (token) merged.set('X-Pairing-Token', token)
  }
  return fetch(apiUrl(path, baseUrl), { ...rest, headers: merged })
}
