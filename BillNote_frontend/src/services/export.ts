import axios from 'axios'
import type { AxiosError } from 'axios'

const API_BASE = String(import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '')

/** 导出菜单里的全部格式 */
export type ExportFormat = 'markdown' | 'pdf' | 'docx' | 'longimage' | 'poster'

/** 需要后端渲染的格式 */
export type ServerExportFormat = 'pdf' | 'docx'

export interface ExportNotePayload {
  markdown: string
  title: string
  output_format: ServerExportFormat
}

/**
 * 后端渲染 PDF / Word 并返回文件流。
 * 故意不走 utils/request 的统一实例：它的响应拦截器按 {code,msg,data} JSON
 * 信封解析，会把二进制流误判成业务失败。这里用独立 axios 调用拿 blob。
 */
export async function exportNoteFile(payload: ExportNotePayload): Promise<Blob> {
  try {
    const resp = await axios.post(`${API_BASE}/export_note`, payload, {
      responseType: 'blob',
      timeout: 120000,
    })
    return resp.data as Blob
  } catch (e) {
    throw new Error(await extractErrorMessage(e))
  }
}

async function extractErrorMessage(e: unknown): Promise<string> {
  // responseType: 'blob' 时错误体也是 Blob，先还原成 JSON 再读 detail
  const response = (e as AxiosError)?.response
  const data = response?.data as unknown
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text())
      return String(parsed?.detail || parsed?.msg || '导出失败')
    } catch {
      // 错误体不是 JSON，走兜底文案
    }
  }
  return `导出失败（${response?.status || '网络错误'}），请稍后再试`
}

/** Blob → 触发浏览器下载（全项目统一范式，同 MarkdownViewer / MarkmapComponent） */
export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

/**
 * 从笔记 markdown 里提取海报用的摘要：丢标题行/图片行/来源链接引用行，
 * 取第一段有内容的文本，去掉行内标记后截断。
 */
export function extractPosterSummary(markdown: string, maxLen = 140): string {
  const cleaned = markdown
    .replace(/^>\s*来源链接：[^\n]*\n*/m, '')
    .split('\n')
    .filter(line => !/^\s{0,3}#{1,6}\s/.test(line)) // 标题行
    .filter(line => !/^\s*!\[/.test(line)) // 图片行
    .join('\n')

  const firstPara =
    cleaned
      .split(/\n\s*\n/)
      .map(s => s.trim())
      .find(Boolean) || ''

  const text = firstPara
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\$\$([^$]+)\$\$/g, '$1')
    .replace(/\$([^$]+)\$/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^[-*+]\s+/, '')
    .replace(/\\([($])/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()

  if (text.length <= maxLen) return text
  return text.slice(0, maxLen) + '…'
}
