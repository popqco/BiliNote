import { useState, useEffect, useRef, useMemo, memo, useCallback, lazy, Suspense, FC, Children, isValidElement } from 'react'
import ReactMarkdown from 'react-markdown'
import { Button } from '@/components/ui/button.tsx'
import { Copy, ArrowRight, Play, ExternalLink, Square, Loader2 } from 'lucide-react'
import { toast } from 'react-hot-toast'
import Error from '@/components/Lottie/error.tsx'
import Loading from '@/components/Lottie/Loading.tsx'
import Idle from '@/components/Lottie/Idle.tsx'
import StepBar from '@/pages/HomePage/components/StepBar.tsx'
import { fetchEnableModels } from '@/services/model.ts'
import { get_automation_config } from '@/services/automation.ts'
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
import { normalizeMathDelimiters, stripScreenshotImages, extractTocSection } from '@/lib/utils'
import {
  findSectionElement,
  findSectionTitleForTime,
  findStrictHeading,
  isTopSection,
  normalizeSectionTitle,
} from '@/lib/sectionLocator'
import { useTaskStore, ACTIVE_STATUSES } from '@/store/taskStore'
import { useChatJumpStore, type SourceJumpTarget } from '@/store/chatStore'
import { noteStyles } from '@/constant/note.ts'
import { MarkdownHeader } from '@/pages/HomePage/components/MarkdownHeader.tsx'
import TranscriptViewer from '@/pages/HomePage/components/transcriptViewer.tsx'
import { useSmoothScroll } from '@/hooks/useSmoothScroll'
import { useStopArmed } from '@/hooks/useStopArmed'
import NoteImage from '@/components/NoteImage'
import { withPairingToken } from '@/utils/workerConnection.ts'
import { buildVideoUrlAt, parseShotTime } from '@/utils/videoUrl.ts'
import ChatPanel from '@/pages/HomePage/components/ChatPanel.tsx'
import TocPanel from '@/pages/HomePage/components/TocPanel.tsx'
import VideoBanner from '@/pages/HomePage/components/VideoBanner.tsx'
import PosterCard from '@/pages/HomePage/components/PosterCard.tsx'
import { useNoteExport } from '@/hooks/useNoteExport'

interface VersionNote {
  ver_id: string
  content: string
  style: string
  model_name: string
  created_at?: string
}

interface MarkdownViewerProps {
  content?: string | VersionNote[]
  status: 'idle' | 'loading' | 'success' | 'failed' | 'stopped'
}

// 导图组件只在使用「导图」视图时加载：首屏/普通阅读不需要 markmap 大分包。
const MarkmapEditor = lazy(
  () => import('@/pages/HomePage/components/MarkmapComponent.tsx'),
)

const steps = [
  { label: '解析链接', key: 'PARSING' },
  { label: '下载音频', key: 'DOWNLOADING' },
  { label: '转写文字', key: 'TRANSCRIBING' },
  { label: '总结内容', key: 'SUMMARIZING' },
  { label: '保存完成', key: 'SUCCESS' },
]

// remark-gfm 默认 singleTilde=true：单个 ~ 也当删除线定界符，AI 写的范围号
// （如「3~5 年…3~5 款」）会被解析成整段删除线，且两个 ~ 被吃掉后在正文里
// 留下一条横线——看起来像文字被叠印/串行（2026-10-17 用户实拍）。
// 关掉后只有 ~~成对~~ 才是删除线，`3~5` 原样渲染。
const remarkPlugins = [[gfm, { singleTilde: false }], remarkMath]
const rehypePlugins = [rehypeKatex, rehypeSlug]

/**
 * 构建 ReactMarkdown components 对象，baseURL 用于修正图片路径。
 * 使用函数 + useMemo 避免每次渲染都创建新的函数实例。
 */
function createMarkdownComponents(
  baseURL: string,
  scrollRoot: HTMLElement | null,
  thumbWidth: number,
  noteKey?: string,
  videoAt?: (seconds: number) => string | undefined,
) {
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
    // 连续多张原片截图（模型刷屏或存量笔记）排成紧凑网格：旧实现每张都是
    // 整行大图，10 张连排就是一面图墙（2026-10-27 用户反馈「像流水账」）。
    // 只处理「整段只有截图」的段落；图文混排保持原样，避免破坏排版。
    p: ({ children, ...props }: any) => {
      const meaningful = Children.toArray(children).filter(
        (child: any) => !(typeof child === 'string' && child.trim() === ''),
      )
      const shots = meaningful.filter(
        (child: any) =>
          isValidElement(child) &&
          typeof (child as any).props?.src === 'string' &&
          (child as any).props.src.includes('/static/screenshots/'),
      )
      if (shots.length >= 2 && shots.length === meaningful.length) {
        return (
          <div
            className={`my-5 grid gap-2 [&>div]:my-0 ${
              shots.length >= 3 ? 'grid-cols-2 sm:grid-cols-3' : 'grid-cols-2'
            }`}
          >
            {children}
          </div>
        )
      }
      return (
        <p className="leading-7 [&:not(:first-child)]:mt-6" {...props}>
          {children}
        </p>
      )
    },
    a: ({ href, children, ...props }: any) => {
      const isOriginLink =
        typeof children[0] === 'string' &&
        (children[0] as string).startsWith('原片 @')

      if (isOriginLink) {
        // 三位分钟（101:52）与 H:MM:SS 都要认，否则长视频 pill 显示成「原片」
        const timeMatch = (children[0] as string).match(/原片 @ (\d{1,3}:\d{2}(?::\d{2})?)/)
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
          // 目录锚点的 id 带 content-XXXX 后缀，而渲染后的 heading 根本
          // 没有 id——旧实现的精确/模糊匹配全部落空，目录点击毫无反应
          // （2026-10-03 browser-use 实测确认）。改走与问答来源徽章同一套
          // 定位机制：按链接文本找章节，顺带自动关掉原文面板并高亮。
          const text = (e.currentTarget.textContent || '').trim()
          const tid = useTaskStore.getState().currentTaskId
          if (!tid || !text) {
            toast.error('未找到对应章节')
            return
          }
          useChatJumpStore.getState().requestJump({
            task_id: tid,
            section_title: text,
          })
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
      if (typeof src === 'string' && src.startsWith('/')) {
        src = baseURL + src
      }
      // 原片截图是 4K JPEG（3840×2160，解码后单张约 31MB），重度笔记一篇
      // 引用可达数百张（实测最多 790 张）：显示走 1600px 缩略（后端磁盘
      // 缓存），点击放大仍取原图（zoomImg）。
      //
      // 2026-10-18 白屏根治：此前只做懒加载——装载不卸载，滚过的图解码位图
      // 全部常驻，长笔记读一遍累计数 GB，渲染进程被 OOM 杀掉整页白屏
      // （frontend_crash.log reason=OUT_OF_MEMORY 连发）。现交给 NoteImage：
      // 离开可视区 ±2500px 即卸载真 src 释放解码内存，滚回来再挂上；
      // 长图导出期间由 screenshotLoadPolicy 强制全部挂载（见 handleExport）。
      //
      // 2026-10-19 外层容器从 flex 改成普通块：flex 行里的内容宽度是
      // shrink-to-fit，占位盒的 width:100% 会反过来依赖内容宽度（空盒时
      // 塌成十几像素的破图尺寸）。块级布局下宽度就是正文列宽，占位盒的
      // aspect-ratio 才有稳定的计算基准。
      const isScreenshot =
        typeof src === 'string' && src.includes('/static/screenshots/')
      // <img> 发不出 X-Pairing-Token：远端手机必须把 token 拼进 query，
      // 否则 /api/img_thumb 401 → NoteImage 回退 4K 原图，一篇 100+ 张
      // 截图就是多传十几 MB（2026-10-23 实测）。宽度按视口取，手机 1200px。
      const displaySrc = isScreenshot
        ? withPairingToken(
            `${baseURL}/api/img_thumb?url=${encodeURIComponent(src)}&w=${thumbWidth}`,
          )
        : src

      // 2026-10-27：新笔记的截图 alt 里存了时间（`![12:20](…)`），
      // 据此渲染「▶ 12:20」徽章，点击直接打开原片对应时间点；
      // 老笔记 alt 为空则保持原样，不显示徽章。
      const shotSeconds = isScreenshot ? parseShotTime(props.alt) : null
      const shotHref = shotSeconds !== null && videoAt ? videoAt(shotSeconds) : undefined
      const shotLabel = shotSeconds !== null ? String(props.alt).trim() : ''

      return (
        <div className="relative my-6 w-full">
          <Zoom {...(isScreenshot ? { zoomImg: { src, alt: props.alt } } : {})}>
            {isScreenshot ? (
              <NoteImage
                src={displaySrc}
                fallbackSrc={src}
                alt={props.alt}
                noteKey={noteKey}
                root={scrollRoot}
                // 尺寸交给 NoteImage 的占位盒（img 是盒内 absolute 图层），
                // 这里只保留视觉样式
                className="cursor-zoom-in rounded-lg shadow-md transition-all hover:shadow-lg"
              />
            ) : (
              <img
                {...props}
                src={displaySrc}
                loading="lazy"
                decoding="async"
                className="h-auto w-full max-w-full cursor-zoom-in rounded-lg object-contain shadow-md transition-all hover:shadow-lg"
                style={{ maxHeight: '500px' }}
              />
            )}
          </Zoom>
          {shotSeconds !== null &&
            (shotHref ? (
              <a
                href={shotHref}
                target="_blank"
                rel="noopener noreferrer"
                title={`打开原片 ${shotLabel}`}
                className="absolute bottom-2 left-2 z-10 inline-flex items-center gap-1 rounded-full bg-black/65 px-2 py-0.5 text-[11px] font-medium text-white shadow-sm backdrop-blur-sm transition-colors hover:bg-black/85"
              >
                <Play className="h-3 w-3" />
                <span>{shotLabel}</span>
              </a>
            ) : (
              <span className="absolute bottom-2 left-2 z-10 inline-flex items-center gap-1 rounded-full bg-black/60 px-2 py-0.5 text-[11px] font-medium text-white backdrop-blur-sm">
                <Play className="h-3 w-3" />
                <span>{shotLabel}</span>
              </span>
            ))}
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
  const ensureTaskContent = useTaskStore(state => state.ensureTaskContent)
  const isMultiVersion = Array.isArray(currentTask?.markdown)
  const hasVersions =
    Array.isArray(currentTask?.markdown) &&
    currentTask.markdown.some(v => !!v?.content?.trim())
  // 正在生成新版本、但本地已有历史版本：不要用全屏进度页盖掉阅读器，
  // 改为顶部进度条 + 正常阅读历史版本（2026-10-25 用户反馈）。
  const showHistoryDuringGeneration = status === 'loading' && hasVersions
  const taskHasBody = Array.isArray(currentTask?.markdown)
    ? currentTask.markdown.length > 0
    : !!(currentTask?.markdown as string | undefined)?.trim()
  const [showTranscribe, setShowTranscribe] = useState(false)
  const [showChat, setShowChat] = useState<false | 'half' | 'full'>(false)
  // 手机窄视口（<768px，与 useIsMobile 同断点）：问答/原文走全屏浮层，
  // 不再与正文并排挤成“左右两条缝”（用户第三张实拍）。
  // 用 matchMedia 监听而非只读一次，横竖屏/分屏切换时自动跟随。
  const [isNarrow, setIsNarrow] = useState<boolean>(
    () =>
      typeof window !== 'undefined' &&
      typeof window.matchMedia !== 'undefined' &&
      window.matchMedia('(max-width: 767px)').matches,
  )
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia === 'undefined') return
    const mq = window.matchMedia('(max-width: 767px)')
    const onChange = (e: MediaQueryListEvent) => setIsNarrow(e.matches)
    setIsNarrow(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  // 手机屏幕物理宽度有限，1200px 缩略足够清晰，体积约为 1600px 的 60%。
  const thumbWidth = isNarrow ? 1200 : 1600
  const [viewMode, setViewMode] = useState<'map' | 'preview'>('preview')
  // 阅读区真正滚动的 Viewport 元素。切换笔记/版本时把它拉回顶部
  // （见下面的回顶 effect）。
  const readerViewportRef = useRef<HTMLDivElement>(null)
  // 同一元素的 state 镜像：作为截图懒挂/卸载 IO 的 root 传给 NoteImage
  // （挂载回调里同步，见 attachViewport）
  const [scrollRoot, setScrollRoot] = useState<HTMLElement | null>(null)
  // 滚轮惯性平滑（设置页可关/三档手感）：Lenis 包住上面的 Viewport。
  // 编程式滚动（回顶/章节跳转）必须走 smoothScrollTo，直接 element.scrollTo
  // 会和进行中的惯性动画互相拉扯。
  const {
    attach: attachSmoothScroll,
    scrollTo: smoothScrollTo,
  } = useSmoothScroll()
  const attachViewport = useCallback(
    (node: HTMLDivElement | null) => {
      readerViewportRef.current = node
      // 阅读区滚动容器同时作为截图懒挂/卸载的 IO root（见 NoteImage）
      setScrollRoot(node)
      attachSmoothScroll(node)
    },
    [attachSmoothScroll],
  )
  // 「停止生成」请求进行中：按钮转「正在停止…」并防连点
  const [stopping, setStopping] = useState(false)
  // 上一拍任务状态：只在「生成中 → 成功」时自动切到最新版本，
  // 不能每次阶段推进（下载→转写→总结）都把用户手动选中的历史版本弹回最新。
  const prevTaskStatusRef = useRef<string>(taskStatus)
  // 「停止」防误触武装（2026-10-18）：点完「重新生成/重试」后这块加载面板
  // 会立刻顶替刚才的「重新生成」按钮，第二下点击会停在刚变出的停止按钮上
  // （用户实拍：stop 紧跟 retry 到达）。启动后的 STOP_ARM_MS 内不渲染
  // 停止按钮（见 useStopArmed）。
  const stopArmed = useStopArmed(currentTask?.id)
  const contentCaptureRef = useRef<HTMLDivElement>(null)
  const posterRef = useRef<HTMLDivElement>(null)

  // 原片截图显隐开关（2026-10-05 用户需求）：看笔记时可一键隐藏截图快速读文字。
  // 全局记忆（localStorage）：所有笔记统一生效，下次打开保持上次选择。
  // 只影响屏幕查看——导出（PDF/Word/长图/复制）始终包含截图，
  // 长图导出期间用 exportForceShow 临时摘掉 hide-screenshots 类。
  const [showScreenshots, setShowScreenshots] = useState<boolean>(() => {
    try {
      return localStorage.getItem('bilinote-show-screenshots') !== '0'
    } catch {
      return true
    }
  })
  const [exportForceShow, setExportForceShow] = useState(false)
  const { exporting, posterData, handleExport } = useNoteExport({
    task: currentTask,
    content: selectedContent,
    createTime,
    captureRef: contentCaptureRef,
    posterRef,
    setExportForceShow,
  })
  const toggleShowScreenshots = () =>
    setShowScreenshots(v => {
      const next = !v
      try {
        localStorage.setItem('bilinote-show-screenshots', next ? '1' : '0')
      } catch {
        /* 忽略 */
      }
      return next
    })

  // 截图时间徽章的跳转地址：B 站/YouTube 拼时间点，其余平台退回原视频。
  const audioMeta = currentTask?.audioMeta
  const videoAt = useCallback(
    (seconds: number) =>
      buildVideoUrlAt({
        platform: audioMeta?.platform,
        videoId: audioMeta?.video_id,
        webpageUrl: audioMeta?.webpage_url || currentTask?.formData?.video_url,
        seconds,
      }),
    [
      audioMeta?.platform,
      audioMeta?.video_id,
      audioMeta?.webpage_url,
      currentTask?.formData?.video_url,
    ],
  )

  // 缓存 ReactMarkdown components，仅在 baseURL / 滚动容器 / 当前笔记变化时重建
  const markdownComponents = useMemo(
    () => createMarkdownComponents(baseURL, scrollRoot, thumbWidth, currentTask?.id, videoAt),
    [baseURL, scrollRoot, thumbWidth, currentTask?.id, videoAt],
  )

  // 正文预处理：隐藏截图时在 markdown 层摘掉截图标记（只靠 CSS 隐藏会留下
  // 截图外层的大边距，全篇莫名空行）；来源链接行不展示。
  const renderedMarkdown = useMemo(
    () =>
      normalizeMathDelimiters(
        (showScreenshots || exportForceShow
          ? selectedContent
          : stripScreenshotImages(selectedContent)
        ).replace(/^>\s*来源链接：[^\n]*\n*/m, ''),
      ),
    [selectedContent, showScreenshots, exportForceShow],
  )

  // 「## 目录」段抽出来单独渲染成紧凑网格（2026-10-14 用户反馈：几十条目录
  // 单列铺满整屏）。items 为 null 时按原样渲染整个 markdown。
  const tocSection = useMemo(() => extractTocSection(renderedMarkdown), [renderedMarkdown])

  // 多版本内容处理
  useEffect(() => {
    if (!currentTask) {
      prevTaskStatusRef.current = taskStatus
      return
    }

    if (!isMultiVersion) {
      setCurrentVerId('') // 清空旧版本 ID
      setModelName(currentTask.formData.model_name)
      setStyle(currentTask.formData.style)
      setCreateTime(currentTask.createdAt)
      setSelectedContent(currentTask?.markdown)
      prevTaskStatusRef.current = taskStatus
    } else {
      const versions = [...currentTask.markdown].sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
      )
      const latestVersion = versions[0]
      const previousStatus = prevTaskStatusRef.current
      prevTaskStatusRef.current = taskStatus
      if (!latestVersion) return
      const currentStillExists = versions.some(v => v.ver_id === currentVerId)
      // 生成中阶段推进时不打断用户查看历史版本；只有新版本刚成功（生成中→成功）
      // 或当前选中的版本已不存在时才自动切到最新版。
      const justFinished =
        ACTIVE_STATUSES.includes(previousStatus as any) && taskStatus === 'SUCCESS'
      if (!currentStillExists || justFinished) {
        setCurrentVerId(latestVersion.ver_id)
      }
    }
    // 依赖必须含 markdown 本体：回填的老笔记点开时先是空概要，
    // 正文由 Home 懒加载后写入 store（string→版本数组形态会变），
    // id/taskStatus 都不变，不监听 markdown 就永远显示旧的空态
    // （2026-10-04 手机实机：老笔记点开一片空白，新笔记正常）。
  }, [currentTask?.id, taskStatus, currentTask?.markdown, isMultiVersion, currentVerId])
  useEffect(() => {
    if (!currentTask || !isMultiVersion) return

    const currentVer = currentTask.markdown.find(v => v.ver_id === currentVerId)
    if (currentVer) {
      setModelName(currentVer.model_name)
      setStyle(currentVer.style)
      setCreateTime(currentVer.created_at || '')
      setSelectedContent(currentVer.content)
    }
  }, [currentVerId, currentTask?.id, currentTask?.markdown])
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
    const vp = readerViewportRef.current
    if (vp) smoothScrollTo(vp, 0, { immediate: true })
  }, [currentTask?.id, currentVerId, smoothScrollTo])

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
    // 全屏问答 / 窄屏（<768px，含手机）下面板替换了阅读区：跳转目标的
    // DOM 根本不存在，轮询必然落空——时间类来源静默无反应、章节类 5 秒
    // 后报「未找到对应章节」（2026-10-07 实锤主根因，用户实拍）。
    // 先把视图切回正文，再走下面的定位轮询。窄屏下原文面板同样让位；
    // 桌面端并排的原文面板不挡阅读区，保持不动。
    if (showChat !== false) setShowChat(false)
    if (isNarrow && showTranscribe) setShowTranscribe(false)
    // 同笔记跳转时 currentTask/currentVerId 都不变，靠这个信号触发定位
    setJumpSignal(s => s + 1)
  }, [jumpTarget, consumeJump, showChat, isNarrow, showTranscribe])

  // 从笔记 DOM 推断“覆盖某时间点”的章节标题：H2/H3 文本里带时间戳
  // （渲染后的原片 pill「原片（04:00）」、或无视频 id 笔记的裸尾缀
  // 「章节名 (04:00)」），取 ≤ start_time 的最大者。规则见 lib/sectionLocator。

  useEffect(() => {
    const target = activeJumpRef.current
    if (!target) return
    // 跨笔记跳转：目标笔记还没切过来时先等（切过来后依赖触发重跑）
    if (target.task_id !== currentTask?.id) return
    let sectionTitle = target.section_title || ''
    if (target.start_time != null) {
      // transcript 来源：按用户反馈只定位正文，不自动打开原文面板
      // （用户手动开的原文面板保持原样）。正文定位到覆盖该时间点的
      // 章节——按 heading 的 原片(MM:SS) 时间戳推断。
      if (!sectionTitle) {
        sectionTitle = findSectionTitleForTime(target.start_time, contentCaptureRef.current) || ''
      }
      if (!sectionTitle) {
        activeJumpRef.current = null
        return
      }
    } else {
      // 章节来源（引用徽章/目录）：正文定位时原文面板让位，否则用户
      // 看不出笔记滚到哪了（2026-10-03 用户实拍反馈：不自动关原文）。
      setShowTranscribe(false)
      setTranscriptFocusTime(null)
    }
    if (!sectionTitle.trim()) {
      activeJumpRef.current = null
      return
    }
    // 标题规范化、复合标题拆分与 LCS 兜底都在 lib/sectionLocator（纯函数）：
    // 时间片段两侧同口径剥离，避免「标题（00:03）」vs「标题 原片（00:03）」失配。
    // intro/开头 块没有可匹配的 heading，直接定位到笔记顶部。
    const topTarget = isTopSection(sectionTitle)
    const search = normalizeSectionTitle(sectionTitle)
    if (!search) {
      activeJumpRef.current = null
      return
    }
    // 找到 heading 后不能一滚了之：跨笔记跳转时笔记内容异步加载会
    // 中途重挂载阅读区（scrollTop 清零），一次 scrollTo 会被冲掉。
    // 这里持续校验目标位置，被冲掉就补滚，连续两轮稳定才算完成。
    // tries 从负数起步：时间徽章会新开转写面板，三列布局要几百毫秒
    // 才稳定，先等布局落定再开始计数，避免重试预算被布局抖动耗光。
    let tries = -4
    let stableTicks = 0
    // 绝对死线：内容异步加载（懒加载）会重置轮询预算，但不允许无限
    // 重试——10 秒还没定位到就放弃并提示。
    const deadline = Date.now() + 10000
    const timer = window.setInterval(() => {
      tries += 1
      const root = contentCaptureRef.current
      const headings = (root || document).querySelectorAll('h1, h2, h3, h4, h5, h6')
      let hit: Element | null = null
      if (topTarget) {
        // 开头定位：命中首个 heading 即可；全文一个 heading 都没有的
        // 话直接把 viewport 滚回顶部收工。
        hit = headings.length ? headings[0] : null
        if (!hit && tries >= 6) {
          const vp = readerViewportRef.current
          window.clearInterval(timer)
          activeJumpRef.current = null
          if (vp) smoothScrollTo(vp, 0, { immediate: true })
          return
        }
      } else {
        // 严格 substring（规范化后）优先；空标题不参与匹配，避免
        // search.includes('') 恒真把定位带到错误章节。
        hit = findStrictHeading(search, headings)
        if (!hit) {
          // 严格 substring 不中时走模糊兜底（复合标题 / 最长公共子串）。
          // 内容异步加载期间 heading 集合会变，每 tick 都重算，加载完成
          // 后自然收敛到正确目标。
          hit = findSectionElement(sectionTitle, contentCaptureRef.current)
        }
        if (!hit) {
          if (tries >= 40 || Date.now() > deadline) {
            window.clearInterval(timer)
            activeJumpRef.current = null
            toast.error('未找到对应章节')
          }
          return
        }
      }
      // 阅读区滚的是 Radix ScrollArea 内层 viewport（window 不滚）。
      // viewport 一律从命中标题就近取——readerViewportRef 可能指向
      // 重挂载前的旧节点，对它 scrollTo 是无声 no-op，定位会卡死在
      // 错误位置（browser-use 实测：转写面板开/关引发的布局变化期间）。
      const vp =
        (hit.closest('[data-slot="scroll-area-viewport"]') as HTMLElement | null) ||
        readerViewportRef.current
      const vpRect = vp ? vp.getBoundingClientRect() : null
      const offset = vpRect
        ? (hit as HTMLElement).getBoundingClientRect().top - vpRect.top
        : (hit as HTMLElement).getBoundingClientRect().top
      // 稳定判定加最短时长：转写面板开合会让列宽/内容高度连变几帧，
      // 过早判定“已稳定”会把定位留在错误位置。
      if (Math.abs(offset - 16) <= 48 && tries >= 6) {
        stableTicks += 1
        // 连续 6 tick（约 720ms）都稳才算完成：关转写/开面板会让列宽
        // 连变几帧，2 tick 的判定会撞上布局收缩过渡、把定位留在半路
        // （browser-use 实测偶发停在目标上方 ~2900px）。
        if (stableTicks >= 6) {
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
      if (tries >= 60 || Date.now() > deadline) {
        window.clearInterval(timer)
        activeJumpRef.current = null
        return
      }
      if (vp) {
        // 平滑滚动约 300-500ms，而校验间隔只有 120ms：每轮都用 smooth
        // 会不停重启动画、位置永远到不了目标（browser-use 实测滚 10 次
        // 全部从 0 重来）。前两次给 smooth，之后一律瞬时补滚。
        // Lenis 模式下 lerp 是指数逼近，每轮重设目标不重启动画、能收敛，
        // 所以同样走「前两次平滑、之后瞬时」的节奏即可。
        const targetTop = vp.scrollTop + offset - 16
        smoothScrollTo(vp, targetTop, { immediate: tries >= 3 })
      } else {
        hit.scrollIntoView({ behavior: tries >= 3 ? 'auto' : 'smooth', block: 'start' })
      }
    }, 120)
    return () => window.clearInterval(timer)
    // 跨笔记：currentTask?.id 切过来后重跑；内容异步加载：currentVerId
    // 变化后重跑（旧实现没这个依赖，新笔记 markdown 渲染完成前轮询
    // 可能已经在旧内容上耗尽了重试次数）。selectedContent：单版本
    // 老笔记正文由 Home 懒加载，currentVerId 不变——不监听内容本体，
    // 5 秒轮询预算会在内容到达前耗尽、误报「未找到对应章节」。
    // jumpSignal：同笔记跳转触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jumpSignal, currentTask?.id, currentVerId, selectedContent])
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
  // 导出控制器见 hooks/useNoteExport.ts

  /**
   * 停止正在生成/排队中的任务（后端立即中止 + 状态落「已停止」）。
   * 生成中/已停止视图共用；stopping 状态给按钮即时反馈。
   */
  const handleStop = async (taskId: string) => {
    if (stopping) return
    setStopping(true)
    try {
      await useTaskStore.getState().cancelTask(taskId)
    } finally {
      setStopping(false)
    }
  }

  /**
   * 失败/已停止页的「重试」：不要直接拿老任务 formData 去发——参数快照前的
   * 老任务没有 model_name/provider_id，直接发只会复现"后台炸响应已发出、任务
   * 永远排队中"（且新版 taskStore 会弹红 toast 拦截）。这里先自动补齐一次可用
   * 模型，补不上才提示去选（摘自 mobile-split 4f3d28e）。
   */
  const handleRetryWithModelFill = async () => {
    const task = getCurrentTask()
    const fd: any = task?.formData || {}
    if (fd.model_name && fd.provider_id) {
      retryTask(task!.id)
      return
    }
    try {
      const [models, autoCfg] = await Promise.all([
        fetchEnableModels().catch(() => []),
        get_automation_config().catch(() => null),
      ])
      const list = Array.isArray(models) ? models : []
      // 优先用任务快照里的模型名（用户上次就是这么选的），否则用第一个可用模型，
      // 再否则用当前自动化配置的模型。
      const pick =
        list.find((m: any) => m.model_name === fd.model_name) ||
        list[0] ||
        null
      const gen = (autoCfg as any)?.gen
      const model_name = pick?.model_name || gen?.model_name
      const provider_id = pick?.provider_id || gen?.provider_id
      if (!model_name || !provider_id) {
        toast.error('没有可用模型可补，请先到「设置 → AI模型设置」添加并启用一个模型')
        return
      }
      useTaskStore.getState().updateTaskContent(task!.id, {
        formData: { ...fd, model_name, provider_id },
      } as any)
      retryTask(task!.id, { ...fd, model_name, provider_id, task_id: task!.id })
    } catch (e) {
      console.error('重试补模型失败：', e)
      toast.error('重试前补模型失败，请到新建表单重选模型后再提交')
    }
  }

  if (status === 'loading' && !showHistoryDuringGeneration) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center space-y-4 text-muted-foreground">
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
        {/* 停止生成（2026-10-16 用户需求）：模型/参数设置错了时不必干等整条
            流程（含转写重试、总结降级阶梯）跑完，停止后可直接改设置重来。
            2026-10-18 防误触：刚启动的 2 秒内不渲染（用户点完「重新生成」
            的下一拍就点到这里，会把刚起来的新任务停掉）。 */}
        {currentTask && stopArmed && (
          <Button
            variant="outline"
            className="border-red-300 text-red-600 hover:bg-red-50 hover:text-red-700 dark:border-red-500/40 dark:text-red-300 dark:hover:bg-red-500/10"
            onClick={() => handleStop(currentTask.id)}
            disabled={stopping}
          >
            <Square className="mr-2 h-4 w-4 fill-current" />
            {stopping ? '正在停止…' : '停止生成'}
          </Button>
        )}
      </div>
    )
  }

  if (status === 'idle') {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center space-y-3 text-muted-foreground">
        <Idle />
        <div className="text-center">
          <p className="text-lg font-bold">输入视频链接并点击"生成笔记"</p>
          <p className="mt-2 text-xs text-muted-foreground">支持哔哩哔哩、YouTube 、抖音等视频平台</p>
        </div>
      </div>
    )
  }

  if (status === 'failed' && !hasVersions) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-4 space-y-3">
        <Error />
        <div className="text-center">
          <p className="text-lg font-bold text-red-500">笔记生成失败</p>
          <p className="mt-2 mb-2 max-w-xl text-center text-xs break-all text-red-400">
            {currentTask?.message || '请检查后台或稍后再试'}
          </p>

          <Button onClick={handleRetryWithModelFill} size="lg">
            重试
          </Button>
        </div>
      </div>
    )
  }

  // 已停止（用户点了「停止生成」）：中性色而非失败红——停止是主动操作，
  // 不是错误。重试按钮沿用补模型逻辑，改完模型直接重跑。
  if (status === 'stopped' && !hasVersions) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-4 space-y-3">
        <Square className="text-muted-foreground h-10 w-10" fill="currentColor" />
        <div className="text-center">
          <p className="text-lg font-bold">已停止生成</p>
          <p className="mt-2 mb-2 max-w-xl text-center text-xs break-all text-muted-foreground">
            {currentTask?.message || '任务已被手动停止，可修改模型/参数后重新生成'}
          </p>

          <Button onClick={handleRetryWithModelFill} size="lg">
            重新生成
          </Button>
        </div>
      </div>
    )
  }

  // 面板接管阅读区（窄屏工作台 / 全屏问答）时，容器去掉 py-2 上下留白并
  // 把背景拉平成 bg-background：面板与上方工具栏、下方底部 Tab 完全贴合，
  // 消除 2026-10-04 用户圈出的两条空隙。阅读/导图模式维持原样。
  const panelTakeover =
    showChat === 'full' || (isNarrow && (showChat === 'half' || showTranscribe))
  // 笔记里是否真的带原片截图：没有就不显示开关按钮（避免无用按钮）
  const hasScreenshots =
    typeof selectedContent === 'string' && selectedContent.includes('/static/screenshots/')

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {showHistoryDuringGeneration && (
        // 生成新版本时不再全屏盖住阅读器：顶部显示进度/停止，下面继续看历史版本。
        <div className="shrink-0 border-b border-amber-200/80 bg-amber-50 dark:border-amber-500/25 dark:bg-amber-500/10">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2 sm:px-4">
            <span className="flex items-center gap-1.5 text-sm font-medium text-amber-800 dark:text-amber-200">
              <Loader2 className="h-4 w-4 animate-spin" />
              正在生成新版本…
            </span>
            <span className="text-xs text-muted-foreground">
              当前查看历史版本，完成后自动切到最新版
            </span>
            {currentTask?.message && (
              <span className="text-xs text-muted-foreground">{currentTask.message}</span>
            )}
            {currentTask && stopArmed && (
              <Button
                variant="outline"
                size="sm"
                className="ml-auto h-8 border-red-300 text-red-600 hover:bg-red-50 hover:text-red-700 dark:border-red-500/40 dark:text-red-300 dark:hover:bg-red-500/10"
                onClick={() => handleStop(currentTask.id)}
                disabled={stopping}
              >
                <Square className="mr-1.5 h-3.5 w-3.5 fill-current" />
                {stopping ? '正在停止…' : '停止生成'}
              </Button>
            )}
          </div>
          <div className="px-3 pb-2 sm:px-4">
            <StepBar steps={steps} currentStep={taskStatus} />
          </div>
        </div>
      )}
      {hasVersions && status === 'failed' && (
        <div className="shrink-0 border-b border-red-200/80 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-200 sm:px-4">
          <div className="flex flex-wrap items-center gap-2">
            <span>新版本生成失败，当前显示历史版本：{currentTask?.message || '请稍后重试'}</span>
            <Button variant="outline" size="sm" className="h-7" onClick={handleRetryWithModelFill}>
              重试
            </Button>
          </div>
        </div>
      )}
      {hasVersions && status === 'stopped' && (
        <div className="shrink-0 border-b border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground sm:px-4">
          <div className="flex flex-wrap items-center gap-2">
            <span>已停止生成，当前显示历史版本</span>
            <Button variant="outline" size="sm" className="h-7" onClick={handleRetryWithModelFill}>
              重新生成
            </Button>
          </div>
        </div>
      )}
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
        showScreenshots={hasScreenshots ? showScreenshots : undefined}
        onToggleShowScreenshots={hasScreenshots ? toggleShowScreenshots : undefined}
      />

      {viewMode === 'map' ? (
        <div className="flex w-full flex-1 overflow-hidden bg-card">
          <div className={'w-full'}>
            <Suspense
              fallback={
                <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
                  正在加载导图…
                </div>
              }
            >
              <MarkmapEditor
                value={selectedContent}
                onChange={() => {}}
                height="100%" // 根据需求可以设定百分比或固定高度
                title={currentTask?.audioMeta?.title || '思维导图'}
              />
            </Suspense>
          </div>
        </div>
      ) : (
        <div className={`flex flex-1 overflow-hidden ${panelTakeover ? 'bg-background' : 'bg-card py-2'}`}>
          {selectedContent && selectedContent !== 'loading' && selectedContent !== 'empty' ? (
            <>
              {showChat === 'full' && currentTask ? (
                <div className="h-full w-full">
                  <ChatPanel taskId={currentTask.id} mode="full" onModeChange={setShowChat} />
                </div>
              ) : (
              <>
              {/* 窄屏工作台（C 方案）：问答 / 原文替换阅读区（与 ScrollArea 互斥）。
                  容器只定高不滚（overflow-hidden），滚动全部交给 ChatPanel /
                  TranscriptViewer 内部——状态行与输入区因此是真·固定，
                  不随内容滑动（2026-10-04 用户五连拍根因：外层 main 整体滚动，
                  一切 sticky 都失效）。桌面端走下面的并排逻辑。 */}
              {isNarrow && (showChat === 'half' || showTranscribe) && currentTask ? (
                showChat === 'half' ? (
                  <div className="min-h-0 min-w-0 flex-1">
                    <ChatPanel taskId={currentTask.id} mode="half" onModeChange={setShowChat} />
                  </div>
                ) : (
                  // 原文面板自带圆角边框卡片，留 8px 内边距避免贴死屏幕边
                  <div className="min-h-0 min-w-0 flex-1 p-2">
                    <TranscriptViewer focusTime={transcriptFocusTime} />
                  </div>
                )
              ) : (
              <ScrollArea viewportRef={attachViewport} className="min-w-0 flex-1">
                {/* 导出长图的截图根：视频信息条 + 正文都包进来。
                    hide-screenshots：用户关掉截图显示时只藏查看（导出期间被摘掉） */}
                <div
                  ref={contentCaptureRef}
                  className={`bg-background pb-6${showScreenshots || exportForceShow ? '' : ' hide-screenshots'}`}
                >
                <div className="px-2">
                  <VideoBanner
                    audioMeta={currentTask?.audioMeta}
                    videoUrl={currentTask?.formData?.video_url}
                  />
                </div>
                {/* 目录段抽出来单独渲染成紧凑网格（可点击跳转）；正文里不再
                    重复渲染原目录段。放在 .markdown-body 外面渲染：github-markdown-css
                    的 ol 默认样式会给它叠一层行序编号，与面板自己的序号列打架
                    （2026-10-15 用户实拍）。 */}
                {tocSection.items && (
                  <div className="px-2">
                    <TocPanel items={tocSection.items} />
                  </div>
                )}
                <div className={'markdown-body w-full px-2'}>
                  {/* 历史笔记存在 IndexedDB 里是 \(...\) / \[...\] 旧写法，
                      渲染时归一化成 remark-math 能识别的 $ / $$ 即可显示，
                      不用做数据迁移（新笔记后端入库时已归一化）。
                      隐藏截图查看时直接在 markdown 层摘掉截图标记——只靠 CSS
                      隐藏会留下截图外层的大边距，笔记里全是莫名空行
                      （2026-10-14 用户反馈）。 */}
                  <ReactMarkdown
                    remarkPlugins={remarkPlugins}
                    rehypePlugins={rehypePlugins}
                    components={markdownComponents}
                  >
                    {tocSection.items
                      ? `${tocSection.before}\n\n${tocSection.after}`
                      : renderedMarkdown}
                  </ReactMarkdown>
                </div>
                </div>
              </ScrollArea>
              )}
              {/* 桌面端并排逻辑保持不变。窄屏走上面的替换式面板，这里只在非窄屏渲染。 */}
              {!isNarrow && showTranscribe && (
                // w-1/3：转写+问答同开时三列并排（笔记 flex-1 / 转写 1/3 /
                // 问答 1/3）。旧值 w-2/4 与问答 w-1/2 相加占满 100%，笔记
                // 阅读区被挤成 0 宽度直接消失——点时间徽章后“整屏都是
                // 转写墙”，用户完全失去笔记上下文（2026-10-03 实拍反馈：
                // 时间戳跳转不直观）。
                <div className="ml-2 w-1/3 shrink-0">
                  <TranscriptViewer focusTime={transcriptFocusTime} />
                </div>
              )}
              {/* 侧边问答模式：markdown + ChatPanel 各占一半；原文面板
                  同开时压缩到 1/3 给笔记让位 */}
              {!isNarrow && showChat === 'half' && currentTask && (
                <div className={`ml-2 h-full shrink-0 ${showTranscribe ? 'w-1/3' : 'w-1/2'}`}>
                  <ChatPanel taskId={currentTask.id} mode="half" onModeChange={setShowChat} />
                </div>
              )}
              </>
              )}
            </>
          ) : currentTask?.contentError ? (
            <div className="flex h-full w-full items-center justify-center">
              <div className="flex w-[300px] flex-col items-center text-center">
                <Error />
                <p className="mb-3 text-sm text-muted-foreground">{currentTask.contentError}</p>
                <Button
                  type="button"
                  size="sm"
                  onClick={() => void ensureTaskContent(currentTask.id, { force: true })}
                >
                  重新加载笔记
                </Button>
              </div>
            </div>
          ) : currentTask && (currentTask.contentLoading || taskHasBody) ? (
            // 正文正在路上：不能再显示「输入视频链接」空态——用户会以为点开失败。
            <div className="flex h-full w-full items-center justify-center">
              <div className="flex w-[300px] flex-col items-center text-center">
                <Loading />
                <p className="mb-2 text-muted-foreground">正在加载笔记正文…</p>
                <p className="text-xs text-muted-foreground">
                  正文优先传输，截图会在阅读时按需加载
                </p>
              </div>
            </div>
          ) : currentTask ? (
            <div className="flex h-full w-full items-center justify-center">
              <div className="flex w-[300px] flex-col items-center text-center">
                <p className="mb-3 text-sm text-muted-foreground">这篇笔记暂无正文</p>
                <Button
                  type="button"
                  size="sm"
                  onClick={() => void ensureTaskContent(currentTask.id, { force: true })}
                >
                  重新加载笔记
                </Button>
              </div>
            </div>
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
