import { useState, useEffect, useRef, useMemo, memo, FC } from 'react'
import ReactMarkdown from 'react-markdown'
import { Button } from '@/components/ui/button.tsx'
import { Copy, ArrowRight, Play, ExternalLink } from 'lucide-react'
import { toast } from 'react-hot-toast'
import Error from '@/components/Lottie/error.tsx'
import Loading from '@/components/Lottie/Loading.tsx'
import Idle from '@/components/Lottie/Idle.tsx'
import StepBar from '@/pages/HomePage/components/StepBar.tsx'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { atomDark as codeStyle } from 'react-syntax-highlighter/dist/esm/styles/prism'
import Zoom from 'react-medium-image-zoom'
import 'react-medium-image-zoom/dist/styles.css'
import gfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeSlug from 'rehype-slug'
import 'katex/dist/katex.min.css'
import 'github-markdown-css/github-markdown-light.css'
import { ScrollArea } from '@/components/ui/scroll-area.tsx'
import { normalizeMathDelimiters } from '@/lib/utils'
import { useTaskStore } from '@/store/taskStore'
import { useChatJumpStore, type SourceJumpTarget } from '@/store/chatStore'
import { noteStyles } from '@/constant/note.ts'
import { MarkdownHeader } from '@/pages/HomePage/components/MarkdownHeader.tsx'
import TranscriptViewer from '@/pages/HomePage/components/transcriptViewer.tsx'
import MarkmapEditor from '@/pages/HomePage/components/MarkmapComponent.tsx'
import ChatPanel from '@/pages/HomePage/components/ChatPanel.tsx'
import VideoBanner from '@/pages/HomePage/components/VideoBanner.tsx'
import { toPng } from 'html-to-image'
import PosterCard, { type PosterData } from '@/pages/HomePage/components/PosterCard.tsx'
import {
  downloadBlob,
  exportNoteFile,
  extractPosterSummary,
  type ExportFormat,
} from '@/services/export'

interface VersionNote {
  ver_id: string
  content: string
  style: string
  model_name: string
  created_at?: string
}

interface MarkdownViewerProps {
  content: string | VersionNote[]
  status: 'idle' | 'loading' | 'success' | 'failed'
}

const steps = [
  { label: '解析链接', key: 'PARSING' },
  { label: '下载音频', key: 'DOWNLOADING' },
  { label: '转写文字', key: 'TRANSCRIBING' },
  { label: '总结内容', key: 'SUMMARIZING' },
  { label: '保存完成', key: 'SUCCESS' },
]

const remarkPlugins = [gfm, remarkMath]
const rehypePlugins = [rehypeKatex, rehypeSlug]

const PLATFORM_LABELS: Record<string, string> = {
  bilibili: '哔哩哔哩',
  youtube: 'YouTube',
  douyin: '抖音',
  xiaohongshu: '小红书',
}

const formatDuration = (seconds?: number): string => {
  if (!seconds || seconds <= 0) return ''
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  return h > 0 ? `${h}:${mm}:${String(s).padStart(2, '0')}` : `${mm}:${String(s).padStart(2, '0')}`
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

/**
 * 构建 ReactMarkdown components 对象，baseURL 用于修正图片路径。
 * 使用函数 + useMemo 避免每次渲染都创建新的函数实例。
 */
function createMarkdownComponents(baseURL: string) {
  return {
    h1: ({ children, ...props }: any) => (
      <h1
        className="text-primary my-6 scroll-m-20 text-3xl font-extrabold tracking-tight lg:text-4xl"
        {...props}
      >
        {children}
      </h1>
    ),
    h2: ({ children, ...props }: any) => (
      <h2
        className="text-primary mt-10 mb-4 scroll-m-20 border-b pb-2 text-2xl font-semibold tracking-tight first:mt-0"
        {...props}
      >
        {children}
      </h2>
    ),
    h3: ({ children, ...props }: any) => (
      <h3
        className="text-primary mt-8 mb-4 scroll-m-20 text-xl font-semibold tracking-tight"
        {...props}
      >
        {children}
      </h3>
    ),
    h4: ({ children, ...props }: any) => (
      <h4
        className="text-primary mt-6 mb-2 scroll-m-20 text-lg font-semibold tracking-tight"
        {...props}
      >
        {children}
      </h4>
    ),
    p: ({ children, ...props }: any) => (
      <p className="leading-7 [&:not(:first-child)]:mt-6" {...props}>
        {children}
      </p>
    ),
    a: ({ href, children, ...props }: any) => {
      const isOriginLink =
        typeof children[0] === 'string' &&
        (children[0] as string).startsWith('原片 @')

      if (isOriginLink) {
        const timeMatch = (children[0] as string).match(/原片 @ (\d{2}:\d{2})/)
        const timeText = timeMatch ? timeMatch[1] : '原片'

        return (
          <span className="origin-link my-2 inline-flex">
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-full bg-blue-50 px-3 py-1 text-sm font-medium text-blue-700 transition-colors hover:bg-blue-100 dark:bg-blue-500/15 dark:text-blue-300 dark:hover:bg-blue-500/25"
              {...props}
            >
              <Play className="h-3.5 w-3.5" />
              <span>原片（{timeText}）</span>
            </a>
          </span>
        )
      }

      // 处理笔记内部锚点链接（如目录跳转）
      if (href?.startsWith('#')) {
        const handleAnchorClick = (e: React.MouseEvent) => {
          e.preventDefault()
          const id = decodeURIComponent(href.slice(1))

          // 1. 优先精确匹配 id
          let target = document.getElementById(id)

          // 2. 精确失败时按 heading 文本模糊匹配
          // LLM 生成的目录锚点可能和 heading 实际文本不完全一致
          //（例如 heading 带 *Content-[00:00]* 后缀，目录链接里没有）
          if (!target) {
            const normalize = (s: string) =>
              s.replace(/[-：:\s*\[\]]/g, '').toLowerCase()
            const search = normalize(id)
            const headings = document.querySelectorAll('h1, h2, h3, h4, h5, h6')
            for (const h of headings) {
              const text = h.textContent || ''
              if (normalize(text).includes(search) || search.includes(normalize(text))) {
                target = h
                break
              }
            }
          }

          if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' })
          } else {
            toast.error('未找到对应章节')
          }
        }

        return (
          <a
            href={href}
            onClick={handleAnchorClick}
            className="text-primary hover:text-primary/80 inline-flex items-center gap-0.5 font-medium underline underline-offset-4"
            {...props}
          >
            {children}
          </a>
        )
      }

      return (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary hover:text-primary/80 inline-flex items-center gap-0.5 font-medium underline underline-offset-4"
          {...props}
        >
          {children}
          {href?.startsWith('http') && (
            <ExternalLink className="ml-0.5 inline-block h-3 w-3" />
          )}
        </a>
      )
    },
    img: ({ node, ...props }: any) => {
      let src = props.src
      if (src.startsWith('/')) {
        src = baseURL + src
      }
      props.src = src

      return (
        <div className="my-8 flex w-full justify-center">
          <Zoom>
            <img
              {...props}
              // w-full + max-w-full：图片永远不超过正文列宽（笔记里嵌 1920×1080
              // 原片截图，缺任一个都会把整篇内容撑宽、右侧被裁）
              className="h-auto w-full max-w-full cursor-zoom-in rounded-lg object-contain shadow-md transition-all hover:shadow-lg"
              style={{ maxHeight: '500px' }}
            />
          </Zoom>
        </div>
      )
    },
    strong: ({ children, ...props }: any) => (
      <strong className="text-primary font-bold" {...props}>
        {children}
      </strong>
    ),
    li: ({ children, ...props }: any) => {
      const rawText = String(children)
      const isFakeHeading = /^(\*\*.+\*\*)$/.test(rawText.trim())

      if (isFakeHeading) {
        return (
          <div className="text-primary my-4 text-lg font-bold">{children}</div>
        )
      }

      return (
        <li className="my-1" {...props}>
          {children}
        </li>
      )
    },
    ul: ({ children, ...props }: any) => (
      <ul className="my-6 ml-6 list-disc [&>li]:mt-2" {...props}>
        {children}
      </ul>
    ),
    ol: ({ children, ...props }: any) => (
      <ol className="my-6 ml-6 list-decimal [&>li]:mt-2" {...props}>
        {children}
      </ol>
    ),
    blockquote: ({ children, ...props }: any) => (
      <blockquote
        className="border-primary/20 text-muted-foreground mt-6 border-l-4 pl-4 italic"
        {...props}
      >
        {children}
      </blockquote>
    ),
    code: ({ inline, className, children, ...props }: any) => {
      const match = /language-(\w+)/.exec(className || '')
      const codeContent = String(children).replace(/\n$/, '')

      if (!inline && match) {
        return (
          <div className="group bg-muted relative my-6 overflow-hidden rounded-lg border shadow-sm">
            <div className="bg-muted text-muted-foreground flex items-center justify-between px-4 py-1.5 text-sm font-medium">
              <div>{match[1].toUpperCase()}</div>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(codeContent)
                  toast.success('代码已复制')
                }}
                className="bg-background/80 hover:bg-background flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors"
              >
                <Copy className="h-3.5 w-3.5" />
                复制
              </button>
            </div>
            <SyntaxHighlighter
              style={codeStyle}
              language={match[1]}
              PreTag="div"
              className="!bg-muted !m-0 !p-0"
              customStyle={{
                margin: 0,
                padding: '1rem',
                background: 'transparent',
                fontSize: '0.9rem',
              }}
              {...props}
            >
              {codeContent}
            </SyntaxHighlighter>
          </div>
        )
      }

      return (
        <code
          className="bg-muted relative rounded px-[0.3rem] py-[0.2rem] font-mono text-sm"
          {...props}
        >
          {children}
        </code>
      )
    },
    table: ({ children, ...props }: any) => (
      // 宽表格只允许横向滚动：之前写的是 overflow-y-auto（纵向），滚轮滚到宽表格上时
      // 会被这层内嵌视口吃掉、正文反而滚不动，体感也是「滚动条互相干扰」的一种。
      <div className="my-6 w-full overflow-x-auto">
        <table className="w-full border-collapse text-sm" {...props}>
          {children}
        </table>
      </div>
    ),
    th: ({ children, ...props }: any) => (
      <th
        className="border-muted-foreground/20 border px-4 py-2 text-left font-medium [&[align=center]]:text-center [&[align=right]]:text-right"
        {...props}
      >
        {children}
      </th>
    ),
    td: ({ children, ...props }: any) => (
      <td
        className="border-muted-foreground/20 border px-4 py-2 text-left [&[align=center]]:text-center [&[align=right]]:text-right"
        {...props}
      >
        {children}
      </td>
    ),
    hr: ({ ...props }: any) => (
      <hr className="border-muted-foreground/20 my-8" {...props} />
    ),
  }
}

const MarkdownViewer: FC<MarkdownViewerProps> = memo(({ status }) => {
  const [copied, setCopied] = useState(false)
  const [currentVerId, setCurrentVerId] = useState<string>('')
  const [selectedContent, setSelectedContent] = useState<string>('')
  const [modelName, setModelName] = useState<string>('')
  const [style, setStyle] = useState<string>('')
  const [createTime, setCreateTime] = useState<string>('')
  // 确保baseURL没有尾部斜杠
  const baseURL = (String(import.meta.env.VITE_API_BASE_URL || '').replace('/api','') || '').replace(/\/$/, '')
  const getCurrentTask = useTaskStore.getState().getCurrentTask
  const currentTask = useTaskStore(state => state.getCurrentTask())
  const taskStatus = currentTask?.status || 'PENDING'
  const retryTask = useTaskStore.getState().retryTask
  const isMultiVersion = Array.isArray(currentTask?.markdown)
  const [showTranscribe, setShowTranscribe] = useState(false)
  const [showChat, setShowChat] = useState<false | 'half' | 'full'>(false)
  const [viewMode, setViewMode] = useState<'map' | 'preview'>('preview')
  const svgRef = useRef<SVGSVGElement>(null)
  // 阅读区真正滚动的 Viewport 元素。切换笔记/版本时把它拉回顶部
  // （见下面的回顶 effect）。
  const readerViewportRef = useRef<HTMLDivElement>(null)
  // 导出（PDF/Word/长图/海报）状态与 DOM 引用
  const [exporting, setExporting] = useState<ExportFormat | null>(null)
  const [posterData, setPosterData] = useState<PosterData | null>(null)
  const contentCaptureRef = useRef<HTMLDivElement>(null)
  const posterRef = useRef<HTMLDivElement>(null)

  // 缓存 ReactMarkdown components，仅在 baseURL 变化时重建
  const markdownComponents = useMemo(() => createMarkdownComponents(baseURL), [baseURL])

  // 多版本内容处理
  useEffect(() => {
    if (!currentTask) return

    if (!isMultiVersion) {
      setCurrentVerId('') // 清空旧版本 ID
      setModelName(currentTask.formData.model_name)
      setStyle(currentTask.formData.style)
      setCreateTime(currentTask.createdAt)
      setSelectedContent(currentTask?.markdown)
    } else {
      const latestVersion = [...currentTask.markdown].sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
      )[0]

      if (latestVersion) {
        setCurrentVerId(latestVersion.ver_id)
      }
    }
  }, [currentTask?.id, taskStatus])
  useEffect(() => {
    if (!currentTask || !isMultiVersion) return

    const currentVer = currentTask.markdown.find(v => v.ver_id === currentVerId)
    if (currentVer) {
      setModelName(currentVer.model_name)
      setStyle(currentVer.style)
      setCreateTime(currentVer.created_at || '')
      setSelectedContent(currentVer.content)
    }
  }, [currentVerId, currentTask?.id])
  // 切换笔记/版本时把阅读区拉回顶部：
  // ScrollArea 的 Viewport 是常驻复用的（Radix 结构），切笔记只换里面的 markdown，
  // 滚动位置会原样保留——长文切短文直接停在半山腰，用户还得手动拉回去。
  // 这里在内容 id 变化后把 viewport 拉回顶部；目录锚点跳转不受影响
  // （那是点击事件里单独做的 scrollIntoView）。
  useEffect(() => {
    // 跳转待执行时不把阅读区拉回顶部：切笔记后内容异步加载会多次触发
    // 本 effect，抢先 scrollTo(0) 会把跳转定位刚滚到的位置清掉。
    const pending = activeJumpRef.current
    if (pending && pending.task_id === currentTask?.id) return
    readerViewportRef.current?.scrollTo({ top: 0 })
  }, [currentTask?.id, currentVerId])

  // 问答来源跳转：切笔记后定位到对应章节 / 打开原文并定位时间。
  //
  // 竞态教训（2026-10-03 browser-use 实测抓到）：旧实现把轮询 effect 直接
  // 挂在 [jumpTarget, currentTask?.id] 上并在 effect 里同步 consumeJump()——
  // consume 会把 store 的 jumpTarget 置空 → effect 依赖变化立即重跑 →
  // cleanup 在轮询第一次 tick 之前就把定时器清掉，跳转从来没真正滚动过。
  // 现在分两层：请求到达时先把目标摘到 ref（consume 不再影响定位流程），
  // 定位 effect 依赖「跳转信号 + 当前笔记 + 内容版本」，内容异步渲染
  // （切笔记/版本变化）后自动重跑，轮询等 heading 出现再滚动。
  const jumpTarget = useChatJumpStore(state => state.jumpTarget)
  const consumeJump = useChatJumpStore(state => state.consumeJump)
  const activeJumpRef = useRef<SourceJumpTarget | null>(null)
  const [jumpSignal, setJumpSignal] = useState(0)
  const [transcriptFocusTime, setTranscriptFocusTime] = useState<number | null>(null)
  useEffect(() => {
    if (!jumpTarget) return
    activeJumpRef.current = jumpTarget
    consumeJump()
    // 同笔记跳转时 currentTask/currentVerId 都不变，靠这个信号触发定位
    setJumpSignal(s => s + 1)
  }, [jumpTarget, consumeJump])

  useEffect(() => {
    const target = activeJumpRef.current
    if (!target) return
    // 跨笔记跳转：目标笔记还没切过来时先等（切过来后依赖触发重跑）
    if (target.task_id !== currentTask?.id) return
    // transcript 来源：打开原文面板并定位时间
    if (target.start_time != null && !target.section_title) {
      activeJumpRef.current = null
      setShowTranscribe(true)
      setTranscriptFocusTime(target.start_time)
      return
    }
    const title = (target.section_title || '').trim()
    if (!title) {
      activeJumpRef.current = null
      setTranscriptFocusTime(null)
      return
    }
    const normalize = (s: string) => s.replace(/[-：:\s*[\]]/g, '').toLowerCase()
    const search = normalize(title)
    if (!search) return
    // 找到 heading 后不能一滚了之：跨笔记跳转时笔记内容异步加载会
    // 中途重挂载阅读区（scrollTop 清零），一次 scrollTo 会被冲掉。
    // 这里持续校验目标位置，被冲掉就补滚，连续两轮稳定才算完成。
    let tries = 0
    let stableTicks = 0
    const timer = window.setInterval(() => {
      tries += 1
      const root = contentCaptureRef.current
      const headings = (root || document).querySelectorAll('h1, h2, h3, h4, h5, h6')
      let hit: Element | null = null
      for (const h of headings) {
        const text = h.textContent || ''
        const norm = normalize(text)
        if (norm.includes(search) || search.includes(norm)) {
          hit = h
          break
        }
      }
      if (!hit) {
        if (tries >= 40) {
          window.clearInterval(timer)
          activeJumpRef.current = null
          toast.error('未找到对应章节')
        }
        return
      }
      // 阅读区滚的是 Radix ScrollArea 内层 viewport（window 不滚）。
      // readerViewportRef 透传到 Radix Viewport 偶发为 null（ref 合并时机），
      // 优先用 ref，拿不到就从目标 heading 就近找 viewport，保证能滚。
      const vp =
        readerViewportRef.current ||
        (hit.closest('[data-slot="scroll-area-viewport"]') as HTMLElement | null)
      const vpRect = vp ? vp.getBoundingClientRect() : null
      const offset = vpRect
        ? (hit as HTMLElement).getBoundingClientRect().top - vpRect.top
        : (hit as HTMLElement).getBoundingClientRect().top
      if (Math.abs(offset - 16) <= 48) {
        stableTicks += 1
        if (stableTicks >= 2) {
          window.clearInterval(timer)
          activeJumpRef.current = null
          // 高亮目标章节 2s：直接操作 DOM class，避免重建 markdown components
          // 导致整篇笔记重新渲染（大笔记会闪）。
          const el = hit as HTMLElement
          const prev = el.style.transition
          el.style.transition = 'background-color 0.3s'
          el.style.backgroundColor = 'rgba(250, 204, 21, 0.25)'
          window.setTimeout(() => {
            el.style.backgroundColor = ''
            el.style.transition = prev
          }, 2000)
        }
        return
      }
      stableTicks = 0
      if (tries >= 50) {
        window.clearInterval(timer)
        activeJumpRef.current = null
        return
      }
      if (vp) {
        // 平滑滚动约 300-500ms，而校验间隔只有 120ms：每轮都用 smooth
        // 会不停重启动画、位置永远到不了目标（browser-use 实测滚 10 次
        // 全部从 0 重来）。前两次给 smooth，之后一律瞬时补滚。
        vp.scrollTo({
          top: vp.scrollTop + offset - 16,
          behavior: tries >= 3 ? 'auto' : 'smooth',
        })
      } else {
        hit.scrollIntoView({ behavior: tries >= 3 ? 'auto' : 'smooth', block: 'start' })
      }
    }, 120)
    return () => window.clearInterval(timer)
    // 跨笔记：currentTask?.id 切过来后重跑；内容异步加载：currentVerId
    // 变化后重跑（旧实现没这个依赖，新笔记 markdown 渲染完成前轮询
    // 可能已经在旧内容上耗尽了重试次数）。jumpSignal：同笔记跳转触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jumpSignal, currentTask?.id, currentVerId])
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(selectedContent)
      setCopied(true)
      toast.success('已复制到剪贴板')
      setTimeout(() => setCopied(false), 2000)
    } catch (e) {
      toast.error('复制失败')
    }
  }
  const alertButton = {
    id: 'alert',
    title: '测试警告',
    content: '⚠️',
    onClick: () => alert('你点击了自定义按钮！'),
  }
  const exportButton = {
    id: 'export',
    title: '导出思维导图',
    content: '⤓',
    onClick: () => {
      const svgEl = svgRef.current
      if (!svgEl) return
      // 同上面的序列化逻辑
      const serializer = new XMLSerializer()
      const source = serializer.serializeToString(svgEl)
      const blob = new Blob(['<?xml version="1.0" encoding="UTF-8"?>', source], {
        type: 'image/svg+xml;charset=utf-8',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'mindmap.svg'
      a.click()
      URL.revokeObjectURL(url)
    },
  }
  const buildPosterData = (): PosterData | null => {
    const task = getCurrentTask()
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
      summary: extractPosterSummary(normalizeMathDelimiters(selectedContent)),
      videoUrl: task.formData?.video_url || task.audioMeta?.raw_info?.webpage_url || '',
    }
  }

  // html-to-image 在部分环境会偶发挂起或抛错（字体收集 / CDN 样式表）：
  // 先用带防御参数的组合，超时后退回最简参数再试一次，保证按钮不会永久卡死
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
    } catch {
      return await withTimeout(
        toPng(node, { pixelRatio: opts.pixelRatio, backgroundColor: opts.backgroundColor }),
      )
    }
  }

  // 长图：截取阅读区（视频信息条 + 正文）。超长内容按画布上限自适应降低倍率，
  // 上限同 MarkmapComponent（32767 边长 / 2.68 亿像素）
  const exportLongImage = async (title: string) => {
    const node = contentCaptureRef.current
    if (!node) {
      toast.error('内容尚未渲染完成')
      return
    }
    const rect = node.getBoundingClientRect()
    const maxSide = 32767
    const maxArea = 268000000
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
    const task = getCurrentTask()
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
        downloadBlob(new Blob([selectedContent], { type: 'text/markdown;charset=utf-8' }), `${title}.md`)
        toast.success('Markdown 已导出')
        return
      }
      if (format === 'pdf' || format === 'docx') {
        // 旧笔记在 IndexedDB 里是 \(...\) / \[...\] 写法，先归一化成 $ / $ 交给
        // 后端公式管线（新笔记已是 $ 写法，归一化幂等）
        const blob = await exportNoteFile({
          markdown: normalizeMathDelimiters(selectedContent),
          title,
          output_format: format,
        })
        downloadBlob(blob, `${title}.${format}`)
        toast.success(format === 'pdf' ? 'PDF 已导出' : 'Word 已导出')
        return
      }
      if (format === 'longimage') {
        await exportLongImage(title)
        toast.success('长图已导出')
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
      const images = Array.from(node.querySelectorAll("img"))
      // 等所有图片进入终态（加载成功或失败都放行，失败交给 onError 降级）。
      // 注意 complete=true 且 naturalWidth=0 是「已失败」——error 事件可能
      // 在我们挂监听之前就触发了，不显式判断会永远挂住
      await Promise.all(
        images.map(im => {
          if (!im.complete) {
            return new Promise<void>(resolve => {
              im.addEventListener("load", () => resolve(), { once: true })
              im.addEventListener("error", () => resolve(), { once: true })
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
  }, [posterData])

  if (status === 'loading') {
    return (
      <div className="flex h-screen w-full flex-col items-center justify-center space-y-4 text-muted-foreground">
        <StepBar steps={steps} currentStep={taskStatus} />
        <Loading className="h-5 w-5" />
        <div className="text-center text-sm">
          <p className="text-lg font-bold">正在生成笔记，请稍候…</p>
          <p className="mt-2 text-xs text-muted-foreground">
            {currentTask?.queuePosition
              ? `排队中 · 前面还有 ${Math.max(0, currentTask.queuePosition - 1)} 个任务`
              : currentTask?.message || '这可能需要几分钟时间，取决于视频长度'}
          </p>
        </div>
      </div>
    )
  }

  if (status === 'idle') {
    return (
      <div className="flex h-screen w-full flex-col items-center justify-center space-y-3 text-muted-foreground">
        <Idle />
        <div className="text-center">
          <p className="text-lg font-bold">输入视频链接并点击"生成笔记"</p>
          <p className="mt-2 text-xs text-muted-foreground">支持哔哩哔哩、YouTube 、抖音等视频平台</p>
        </div>
      </div>
    )
  }

  if (status === 'failed' && !isMultiVersion) {
    return (
      <div className="flex h-screen w-full flex-col items-center justify-center gap-4 space-y-3">
        <Error />
        <div className="text-center">
          <p className="text-lg font-bold text-red-500">笔记生成失败</p>
          <p className="mt-2 mb-2 max-w-xl text-center text-xs break-all text-red-400">
            {currentTask?.message || '请检查后台或稍后再试'}
          </p>

          <Button onClick={() => retryTask(currentTask.id)} size="lg">
            重试
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden">
      <MarkdownHeader
        currentTask={currentTask}
        isMultiVersion={isMultiVersion}
        currentVerId={currentVerId}
        setCurrentVerId={setCurrentVerId}
        modelName={modelName}
        style={style}
        noteStyles={noteStyles}
        onCopy={handleCopy}
        onExport={handleExport}
        exporting={exporting}
        createAt={createTime}
        showTranscribe={showTranscribe}
        setShowTranscribe={setShowTranscribe}
        showChat={showChat}
        setShowChat={setShowChat}
        viewMode={viewMode}
        setViewMode={setViewMode}
      />

      {viewMode === 'map' ? (
        <div className="flex w-full flex-1 overflow-hidden bg-card">
          <div className={'w-full'}>
            <MarkmapEditor
              value={selectedContent}
              onChange={() => {}}
              height="100%" // 根据需求可以设定百分比或固定高度
              title={currentTask?.audioMeta?.title || '思维导图'}
            />
          </div>
        </div>
      ) : (
        <div className="flex flex-1 overflow-hidden bg-card py-2">
          {selectedContent && selectedContent !== 'loading' && selectedContent !== 'empty' ? (
            <>
              {showChat === 'full' && currentTask ? (
                <div className="h-full w-full">
                  <ChatPanel taskId={currentTask.id} mode="full" onModeChange={setShowChat} />
                </div>
              ) : (
              <>
              <ScrollArea viewportRef={readerViewportRef} className="min-w-0 flex-1">
                {/* 导出长图的截图根：视频信息条 + 正文都包进来 */}
                <div ref={contentCaptureRef} className="bg-background pb-6">
                <div className="px-2">
                  <VideoBanner
                    audioMeta={currentTask?.audioMeta}
                    videoUrl={currentTask?.formData?.video_url}
                  />
                </div>
                <div className={'markdown-body w-full px-2'}>
                  <ReactMarkdown
                    remarkPlugins={remarkPlugins}
                    rehypePlugins={rehypePlugins}
                    components={markdownComponents}
                  >
                    {/* 历史笔记存在 IndexedDB 里是 \(...\) / \[...\] 旧写法，
                        渲染时归一化成 remark-math 能识别的 $ / $$ 即可显示，
                        不用做数据迁移（新笔记后端入库时已归一化） */}
                    {normalizeMathDelimiters(
                      selectedContent.replace(/^>\s*来源链接：[^\n]*\n*/m, ''),
                    )}
                  </ReactMarkdown>
                </div>
                </div>
              </ScrollArea>
              {showTranscribe && (
                <div className={'ml-2 w-2/4'}>
                  <TranscriptViewer focusTime={transcriptFocusTime} />
                </div>
              )}
              {/* 侧边问答模式：markdown + ChatPanel 各占一半 */}
              {showChat === 'half' && currentTask && (
                <div className="ml-2 h-full w-1/2 shrink-0">
                  <ChatPanel taskId={currentTask.id} mode="half" onModeChange={setShowChat} />
                </div>
              )}
              </>
              )}
            </>
          ) : (
            <div className="flex h-full w-full items-center justify-center">
              <div className="w-[300px] flex-col justify-items-center">
                <div className="bg-primary-light mb-4 flex h-16 w-16 items-center justify-center rounded-full">
                  <ArrowRight className="text-primary h-8 w-8" />
                </div>
                <p className="mb-2 text-muted-foreground">输入视频链接并点击"生成笔记"按钮</p>
                <p className="text-xs text-muted-foreground">支持哔哩哔哩、YouTube等视频网站</p>
              </div>
            </div>
          )}
        </div>
      )}
      {/* 摘要海报的屏幕外挂载点：仅导出期间渲染，截图由 effect 负责 */}
      {posterData && (
        <div style={{ position: 'fixed', left: -10000, top: 0, zIndex: -1 }} aria-hidden>
          <div ref={posterRef}>
            <PosterCard {...posterData} />
          </div>
        </div>
      )}
    </div>
  )
})

MarkdownViewer.displayName = 'MarkdownViewer'

export default MarkdownViewer
