/**
 * 笔记导出控制器：Markdown / PDF / Word / 长图 / 海报。
 *
 * 从 MarkdownViewer 抽出的原因：导出同时依赖 DOM 几何、截图加载策略、
 * html-to-image 超时兜底与海报离屏渲染，和阅读/跳转/滚动混在一个组件里
 * 时，任何一处改动都要重读另外两处。这里只接收 DOM ref 与当前内容。
 */
import { useEffect, useState, type RefObject } from 'react'
import { toast } from 'react-hot-toast'
import { toPng } from 'html-to-image'
import {
  downloadBlob,
  exportNoteFile,
  extractPosterSummary,
  type ExportFormat,
} from '@/services/export'
import type { PosterData } from '@/pages/HomePage/components/PosterCard.tsx'
import { normalizeMathDelimiters } from '@/lib/utils'
import { setScreenshotExportMode } from '@/utils/screenshotLoadPolicy'
import type { Task } from '@/store/taskStore'

const PLATFORM_LABELS: Record<string, string> = {
  bilibili: '哔哩哔哩',
  youtube: 'YouTube',
  douyin: '抖音',
  kuaishou: '快手',
  xiaohongshu: '小红书',
  local: '本地文件',
}

const formatDuration = (seconds?: number): string => {
  if (!seconds || Number.isNaN(seconds)) return ''
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

const formatPosterDate = (value?: string | Date): string => {
  if (!value) return ''
  const d = typeof value === 'string' ? new Date(value) : value
  if (isNaN(d.getTime())) return ''
  return d
    .toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
    .replace(/\//g, '-')
}

interface UseNoteExportOptions {
  task: Task | null
  content: string
  createTime: string
  captureRef: RefObject<HTMLDivElement | null>
  posterRef: RefObject<HTMLDivElement | null>
  setExportForceShow: (value: boolean) => void
}

/** html-to-image 在部分环境会偶发挂起或抛错（字体收集 / CDN 样式表）。 */
const captureNodeToPng = async (
  node: HTMLElement,
  opts: { pixelRatio: number; backgroundColor: string; filter?: (el: HTMLElement) => boolean },
): Promise<string> => {
  const withTimeout = <T,>(p: Promise<T>): Promise<T> =>
    Promise.race([
      p,
      new Promise<T>((_, reject) =>
        setTimeout(() => reject(new Error('截图超时，请重试')), 20000),
      ),
    ])
  try {
    return await withTimeout(
      toPng(node, {
        pixelRatio: opts.pixelRatio,
        backgroundColor: opts.backgroundColor,
        cacheBust: true,
        // 页面里存在依赖注入的跨域 CDN 样式表，字体收集会被网络抖动炸掉
        skipFonts: true,
        ...(opts.filter ? { filter: opts.filter } : {}),
      }),
    )
  } catch (e: any) {
    // 超时说明这次截图本身太重（长笔记/大量截图），再打一次同样重的
    // capture 只会让主线程更久无法恢复；直接交给上层报错。
    if (String(e?.message || '').includes('截图超时')) throw e
    return await withTimeout(
      toPng(node, { pixelRatio: opts.pixelRatio, backgroundColor: opts.backgroundColor }),
    )
  }
}

export const useNoteExport = ({
  task,
  content,
  createTime,
  captureRef,
  posterRef,
  setExportForceShow,
}: UseNoteExportOptions) => {
  const [exporting, setExporting] = useState<ExportFormat | null>(null)
  const [posterData, setPosterData] = useState<PosterData | null>(null)

  const buildPosterData = (): PosterData | null => {
    if (!task) return null
    const rawCover = task.audioMeta?.cover_url || ''
    const apiBase = String(import.meta.env.VITE_API_BASE_URL || 'api').replace(/\/$/, '')
    return {
      title: task.audioMeta?.title || '视频笔记',
      coverUrl: rawCover ? `${apiBase}/image_proxy?url=${encodeURIComponent(rawCover)}` : '',
      platform: PLATFORM_LABELS[task.audioMeta?.platform] || task.audioMeta?.platform || '',
      uploader: task.audioMeta?.raw_info?.uploader || '',
      duration: formatDuration(task.audioMeta?.duration),
      createdAt: formatPosterDate(createTime),
      summary: extractPosterSummary(normalizeMathDelimiters(content)),
      videoUrl: task.formData?.video_url || task.audioMeta?.raw_info?.webpage_url || '',
    }
  }

  // 长图：截取阅读区（视频信息条 + 正文）。超长内容按画布上限自适应降低倍率，
  // 上限同 MarkmapComponent（32767 边长 / 1.34 亿像素）。
  const waitForNoteShots = async (timeoutMs = 20000) => {
    const node = captureRef.current
    if (!node) return
    const start = Date.now()
    for (;;) {
      const imgs = Array.from(
        node.querySelectorAll('img[data-note-shot]'),
      ) as HTMLImageElement[]
      // 卸载态的占位图 naturalWidth=1：等在挂载且加载完成的图上
      if (imgs.every(im => im.complete && im.naturalWidth > 1)) return
      if (Date.now() - start >= timeoutMs) return // 超时照截：宁少勿卡死
      await new Promise(r => setTimeout(r, 150))
    }
  }

  const exportLongImage = async (title: string) => {
    const node = captureRef.current
    if (!node) {
      toast.error('内容尚未渲染完成')
      return
    }
    const rect = node.getBoundingClientRect()
    const maxSide = 32767
    // 面积上限从 2.68 亿降到 1.34 亿像素：2.68 亿 × 4B 的 ARGB 画布 = 1GB
    // 单次分配，叠加被内联的数百张截图很容易把渲染进程推到 OOM 边缘
    // （2026-10-17 白屏）。1.34 亿（约 512MB）对常规笔记仍取到 pixelRatio=2。
    const maxArea = 134000000
    const pixelRatio = Math.max(
      0.5,
      Math.min(
        2,
        maxSide / Math.max(1, rect.height),
        maxSide / Math.max(1, rect.width),
        Math.sqrt(maxArea / Math.max(1, rect.width * rect.height)),
      ),
    )
    const dataUrl = await captureNodeToPng(node, {
      pixelRatio,
      backgroundColor: getComputedStyle(document.body).backgroundColor || '#ffffff',
      // react-medium-image-zoom 的放大/缩小按钮是无障碍隐藏节点，截出来会变成裸按钮
      filter: el =>
        !(
          el instanceof HTMLElement &&
          (el.hasAttribute('data-rmiz-btn-zoom') || el.hasAttribute('data-rmiz-btn-unzoom'))
        ),
    })
    const blob = await (await fetch(dataUrl)).blob()
    downloadBlob(blob, `${title}.png`)
  }

  const handleExport = async (format: ExportFormat) => {
    if (exporting) return
    const title = task?.audioMeta?.title || 'note'

    // 海报的截图由下方 effect 在图片解码完成后收尾（含 exporting 复位）
    if (format === 'poster') {
      const data = buildPosterData()
      if (!data) {
        toast.error('当前没有可导出的笔记')
        return
      }
      setExporting(format)
      setPosterData(data)
      return
    }

    setExporting(format)
    try {
      if (format === 'markdown') {
        downloadBlob(new Blob([content], { type: 'text/markdown;charset=utf-8' }), `${title}.md`)
        toast.success('Markdown 已导出')
        return
      }
      if (format === 'pdf' || format === 'docx') {
        // 旧笔记在 IndexedDB 里是 \(...\) / \[...\] 写法，先归一化成 $ / $ 交给
        // 后端公式管线（新笔记已是 $ 写法，归一化幂等）
        const blob = await exportNoteFile({
          markdown: normalizeMathDelimiters(content),
          title,
          output_format: format,
        })
        downloadBlob(blob, `${title}.${format}`)
        toast.success(format === 'pdf' ? 'PDF 已导出' : 'Word 已导出')
        return
      }
      if (format === 'longimage') {
        // 导出始终包含截图：临时摘掉 hide-screenshots，等重渲染后再截
        setExportForceShow(true)
        // NoteImage 平时把离屏截图卸载成透明占位（省内存）；导出要整篇入画，
        // 先开导出模式强制全部挂上真 src，等加载完再 capture，截完恢复
        setScreenshotExportMode(true)
        let watchdog: number | undefined
        try {
          // 等两帧让 exportForceShow / 截图挂载生效；后台标签或锁屏下 rAF
          // 会被暂停，必须有定时器兜底，否则导出永远停在等待阶段。
          await new Promise<void>(resolve => {
            let settled = false
            const done = () => {
              if (settled) return
              settled = true
              resolve()
            }
            requestAnimationFrame(() => requestAnimationFrame(done))
            window.setTimeout(done, 300)
          })
          await waitForNoteShots()
          const timeout = new Promise<never>((_, reject) => {
            watchdog = window.setTimeout(
              () => reject(new Error('长图导出超时，请改用 PDF 或 Word')),
              90000,
            )
          })
          await Promise.race([exportLongImage(title), timeout])
          toast.success('长图已导出')
        } finally {
          if (watchdog) window.clearTimeout(watchdog)
          setScreenshotExportMode(false)
          setExportForceShow(false)
        }
        return
      }
    } catch (e: any) {
      toast.error(e?.message || '导出失败')
    } finally {
      setExporting(null)
    }
  }

  // 海报导出：PosterCard 已挂到屏幕外，等封面图解码完成、排版稳定后再截图
  useEffect(() => {
    if (!posterData || !posterRef.current) return
    let cancelled = false
    ;(async () => {
      const node = posterRef.current!
      const images = Array.from(node.querySelectorAll('img'))
      // 等所有图片进入终态（加载成功或失败都放行，失败交给 onError 降级）。
      // complete=true 且 naturalWidth=0 是「已失败」——error 事件可能在挂监听
      // 之前就触发了，不显式判断会永远挂住。
      await Promise.all(
        images.map(im => {
          if (!im.complete) {
            return new Promise<void>(resolve => {
              im.addEventListener('load', () => resolve(), { once: true })
              im.addEventListener('error', () => resolve(), { once: true })
            })
          }
          return Promise.resolve()
        }),
      )
      await new Promise(r => setTimeout(r, 80))
      if (cancelled) return
      try {
        const dataUrl = await captureNodeToPng(node, { pixelRatio: 2, backgroundColor: '#ffffff' })
        const blob = await (await fetch(dataUrl)).blob()
        downloadBlob(blob, `${posterData.title}.png`)
        toast.success('海报已导出')
      } catch (e: any) {
        console.error('[export] 海报截图失败:', e)
        toast.error(e?.message || '海报导出失败')
      } finally {
        setPosterData(null)
        setExporting(null)
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [posterData])

  return { exporting, posterData, handleExport }
}
