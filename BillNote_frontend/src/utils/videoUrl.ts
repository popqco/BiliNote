/**
 * 原片截图时间徽章的跳转地址（2026-10-27）。
 *
 * 后端把 `*Screenshot-[12:20]*` 替换成 `![12:20](/static/screenshots/x.jpg)`，
 * alt 里存的时间就是这里用来拼链接的依据。规则与后端
 * `note_helper.replace_content_markers` 保持一致：B 站 `?t=`/`&t=`，
 * YouTube `&t=Ns`；抖音/小红书/微信没有可靠时间点参数，退回原视频地址。
 */
export function buildVideoUrlAt(opts: {
  platform?: string
  videoId?: string
  webpageUrl?: string
  seconds: number
}): string | undefined {
  const seconds = Math.max(0, Math.floor(opts.seconds))
  if (!Number.isFinite(seconds)) return opts.webpageUrl || undefined

  const platform = String(opts.platform || '').toLowerCase()
  const videoId = String(opts.videoId || '').trim()

  if (platform === 'youtube' && videoId) {
    return `https://www.youtube.com/watch?v=${videoId}&t=${seconds}s`
  }
  if (platform === 'bilibili' && videoId) {
    // 多 P：BV1xx_p3 → BV1xx?p=3（与后端 replace_content_markers 同口径）
    const vid = videoId.replace('_p', '?p=')
    const sep = vid.includes('?') ? '&' : '?'
    return `https://www.bilibili.com/video/${vid}${sep}t=${seconds}`
  }
  return opts.webpageUrl || undefined
}

/** "12:20" / "101:52" → 秒；格式不合法返回 null。 */
export function parseShotTime(label: string | undefined | null): number | null {
  const match = String(label || '').trim().match(/^(\d{1,3}):([0-5]\d)$/)
  if (!match) return null
  return Number(match[1]) * 60 + Number(match[2])
}
