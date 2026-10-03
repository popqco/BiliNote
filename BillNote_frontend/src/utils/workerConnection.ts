/**
 * Worker 连接信息：Viewer 直连 Worker 的地址 + 配对 token（票 3 的底座）。
 *
 * 存储：localStorage('bilinote-worker-connection')，Viewer 本机持久化，重启不丢。
 * baseURL 解析优先级：用户已配对的 Worker 地址 > 构建期 VITE_API_BASE_URL > '/api' 兜底。
 * 本机 All-in-One（没配过 Worker）走原逻辑，行为零变化。
 */

export interface WorkerConnection {
  /** Worker 地址，例如 http://100.64.0.5:8483（不带 /api 后缀，保存时已规范化） */
  baseUrl: string
  /** 配对 token（票 1 后端签发，明文存本机 localStorage，仅发给已配对的 Worker） */
  token: string
}

const STORAGE_KEY = 'bilinote-worker-connection'

function normalizeBaseUrl(raw: string): string {
  return raw.trim().replace(/\/+$/, '').replace(/\/api$/, '')
}

export function loadWorkerConnection(): WorkerConnection | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<WorkerConnection>
    if (!parsed.baseUrl || !parsed.token) return null
    return { baseUrl: normalizeBaseUrl(parsed.baseUrl), token: parsed.token }
  }
  catch {
    return null
  }
}

export function saveWorkerConnection(conn: WorkerConnection): WorkerConnection {
  const normalized = { baseUrl: normalizeBaseUrl(conn.baseUrl), token: conn.token }
  localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized))
  return normalized
}

export function clearWorkerConnection(): void {
  localStorage.removeItem(STORAGE_KEY)
}

/** API 根地址：配对地址 + '/api'，或构建期 env，或同源 '/api'（直服场景）。 */
export function resolveApiBaseUrl(): string {
  const conn = loadWorkerConnection()
  if (conn?.baseUrl) return `${conn.baseUrl}/api`
  const fromEnv = (import.meta as any).env?.VITE_API_BASE_URL as string | undefined
  if (fromEnv && fromEnv.length > 0) return fromEnv.replace(/\/$/, '')
  return '/api'
}

/** 图片/封面等去掉 /api 后缀的裸 host：同源直服时为空字符串（相对路径即可）。 */
export function resolveAssetBaseUrl(): string {
  const conn = loadWorkerConnection()
  if (conn?.baseUrl) return conn.baseUrl
  const fromEnv = (import.meta as any).env?.VITE_API_BASE_URL as string | undefined
  if (!fromEnv || fromEnv.length === 0) return ''
  return String(fromEnv).replace('/api', '').replace(/\/$/, '')
}

/**
 * 给 URL 补配对 token：<img> 发不出自定义头，已配对时拼 ?pairing_token=
 * （后端 PairingAuthMiddleware 允许 query 传 token，header 优先）。
 * 未配对（本机 All-in-One 回环免鉴）原样返回，保持 URL 干净。
 * 仅处理同源 / 相对路径（/api/…、/static/…）；绝对外链不动。
 */
export function withPairingToken(url: string): string {
  const token = loadWorkerConnection()?.token
  if (!token) return url
  // 指向配对 Worker 自身的绝对地址也要补 token（手机 Viewer 拼出的就是绝对地址）；
  // 真正的外链（B 站 CDN 等）不动。判断：同源相对路径，或 host 与配对地址一致。
  if (/^https?:\/\//i.test(url)) {
    try {
      const base = loadWorkerConnection()?.baseUrl
      if (!base) return url
      if (new URL(url).origin !== new URL(base).origin) return url
    } catch {
      return url
    }
  }
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}pairing_token=${encodeURIComponent(token)}`
}

/**
 * 拼 image_proxy 图片地址：走 withPairingToken 统一补 token。
 */
export function buildImageProxyUrl(rawUrl: string): string {
  const apiBase = resolveApiBaseUrl().replace(/\/$/, '')
  return withPairingToken(`${apiBase}/image_proxy?url=${encodeURIComponent(rawUrl)}`)
}
