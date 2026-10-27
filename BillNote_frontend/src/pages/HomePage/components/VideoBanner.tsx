import { ExternalLink } from 'lucide-react'
import type { AudioMeta } from '@/store/taskStore'
import { buildImageProxyUrl } from '@/utils/workerConnection.ts'

interface VideoBannerProps {
  audioMeta?: AudioMeta
  videoUrl?: string
}

/** 平台 label 映射 */
const platformLabel: Record<string, string> = {
  bilibili: '哔哩哔哩',
  youtube: 'YouTube',
  douyin: '抖音',
  xiaohongshu: '小红书',
  wechat_article: '微信公众号',
  wechat_channels: '微信视频号',
}

export default function VideoBanner({ audioMeta, videoUrl }: VideoBannerProps) {
  if (!audioMeta) return null

  const rawCover = audioMeta.cover_url
  // 通过后端代理加载封面，避免跨域/Referrer 限制（已配对 Viewer 自动带 token）。
  // w=800：手机整宽/桌面 112px 缩略都够清晰，体积约为原图的 1/4。
  const coverUrl = rawCover ? buildImageProxyUrl(rawCover, 800) : ''
  const title = audioMeta.title
  // 轻量笔记视图把 raw_info 里的两个字段拍平了；老数据仍回退 raw_info。
  const uploader = audioMeta.uploader || audioMeta.raw_info?.uploader || ''
  const platform = platformLabel[audioMeta.platform] || audioMeta.platform || ''
  const originalUrl = videoUrl || audioMeta.webpage_url || audioMeta.raw_info?.webpage_url || ''

  return (
    <div className="bg-slate-800 relative mb-4 overflow-hidden rounded-lg">
      {/* 模糊背景封面 */}
      <div className="absolute inset-0">
        {coverUrl ? (
          <img
            src={coverUrl}
            alt=""
            referrerPolicy="no-referrer"
            className="h-full w-full object-cover blur-md brightness-[0.4] scale-110"
          />
        ) : (
          <div className="h-full w-full bg-gradient-to-r from-blue-600 to-indigo-700" />
        )}
        {/* 暗化兜底层（2026-10-11）：封面加载失败/偏亮时，白字曾直接压在浅底上看不清；
            容器 bg-slate-800 + 本层保证任何情况下白字对比度 ≥ 4.5:1 */}
        <div className="absolute inset-0 bg-black/30" />
      </div>

      {/* 内容层：手机窄屏上下排（封面 16:9 全宽 + 标题全显两行），桌面端保持左右排。 */}
      <div className="relative flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:gap-4 sm:px-5">
        {/* 封面缩略图 */}
        {coverUrl && (
          <img
            src={coverUrl}
            alt={title}
            referrerPolicy="no-referrer"
            className="aspect-video w-full shrink-0 rounded-md object-cover shadow-md sm:h-16 sm:w-28 sm:aspect-auto"
          />
        )}

        {/* 文字信息：标题最多两行（之前 truncate 单行，手机上只剩几个字） */}
        <div className="min-w-0 flex-1">
          <h2 className="line-clamp-2 text-base font-bold text-white" title={title}>
            {title}
          </h2>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-white/70">
            {uploader && <span className="truncate">{uploader}</span>}
            {uploader && platform && <span className="text-white/40">·</span>}
            {platform && <span>{platform}</span>}
          </div>
        </div>

        {/* 跳转原视频 */}
        {originalUrl && (
          <a
            href={originalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="flex shrink-0 items-center gap-1.5 self-end rounded-full bg-white/15 px-3 py-1.5 text-xs font-medium text-white backdrop-blur-sm transition-colors hover:bg-white/25 sm:self-auto"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            <span>原视频</span>
          </a>
        )}
      </div>
    </div>
  )
}
