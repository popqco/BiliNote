import { useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'

/** 摘要海报的全部展示数据，由 MarkdownViewer 组装 */
export interface PosterData {
  title: string
  coverUrl: string
  platform: string
  uploader: string
  duration: string
  createdAt: string
  summary: string
  videoUrl: string
}

/**
 * 分享海报（摘要卡片）：封面 + 标题 + 元信息 + 笔记首段总结 + 原视频二维码。
 * 固定 750px 宽、固定亮色配色——海报是分享出去的图，不跟随应用暗色主题，
 * 导出时由 MarkdownViewer 放到屏幕外用 html-to-image 截成 PNG。
 */
export default function PosterCard({
  title,
  coverUrl,
  platform,
  uploader,
  duration,
  createdAt,
  summary,
  videoUrl,
}: PosterData) {
  const [coverOk, setCoverOk] = useState(true)

  return (
    <div
      className="w-[750px] overflow-hidden rounded-2xl bg-white text-[#1f2328]"
      style={{ fontFamily: "'PingFang SC', 'Microsoft YaHei', 'Noto Sans SC', sans-serif" }}
    >
      {coverUrl && coverOk && (
        <img
          src={coverUrl}
          alt={title}
          onError={() => setCoverOk(false)}
          className="h-[420px] w-full object-cover"
          // 以 CORS 方式加载封面：后端代理保证带 ACAO 头，浏览器缓存的是
          // 「干净」的 CORS 缓存；否则 no-cors 旧缓存会毒化 html-to-image
          // 截图时的 fetch，海报导出随机失败
          crossOrigin="anonymous"
        />
      )}

      <div className="px-10 pb-9 pt-7">
        <h1 className="text-[26px] font-bold leading-snug">{title}</h1>

        <div className="mt-3 flex flex-wrap items-center gap-2 text-[13px] text-[#6b7280]">
          {uploader && <span>{uploader}</span>}
          {uploader && platform && <span className="text-[#d1d5db]">·</span>}
          {platform && <span>{platform}</span>}
          {duration && <span className="text-[#d1d5db]">·</span>}
          {duration && <span>{duration}</span>}
          {createdAt && <span className="text-[#d1d5db]">·</span>}
          {createdAt && <span>{createdAt}</span>}
        </div>

        <div className="my-5 h-px bg-[#ececf1]" />

        {summary && <p className="text-[15px] leading-7 text-[#3f4451]">{summary}</p>}

        <div className="mt-8 flex items-end justify-between gap-6">
          <div className="min-w-0">
            <div className="text-[12px] font-medium text-[#9ca3af]">原视频链接</div>
            <div className="mt-1 break-all text-[12px] leading-5 text-[#6b7280]">
              {videoUrl || '—'}
            </div>
            <div className="mt-4 text-[12px] font-medium text-[#111827]">
              BiliNote · AI 视频笔记
            </div>
          </div>
          {videoUrl && (
            <div className="shrink-0 rounded-lg border border-[#ececf1] bg-white p-2">
              <QRCodeSVG value={videoUrl} size={96} bgColor="#ffffff" fgColor="#1f2328" level="M" />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
