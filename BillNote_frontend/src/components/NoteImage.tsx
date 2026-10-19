import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useInView } from 'react-intersection-observer'
import {
  getScreenshotExportMode,
  subscribeScreenshotExportMode,
} from '@/utils/screenshotLoadPolicy'

/** 装载/卸载带：可视区外 ±BAND px 内挂真 src，超出即卸载 */
const BAND = '2500px 0px 2500px 0px'

/** 首帧兜底比例：16:9 */
const DEFAULT_DIMS = { w: 1600, h: 900 }

/** 截图显示的最大高度（与原行为一致） */
const MAX_H = 500

/** 单张图片的真实尺寸：同一 src 反复挂/卸不再重测、不再改比例 */
const dimsBySrc = new Map<string, { w: number; h: number }>()

/**
 * 按笔记记住截图比例。同一次生成出的截图尺寸一致（实测同一篇全是
 * 1920×886），但这和 16:9 差着 6%。若每张图各自「先按 16:9 占位、加载后
 * 再缩」，滚动过程中每张图都会把下方内容拽一次（用户实拍：正文一直上下
 * 跳）。现在一篇里第一张量到真实比例后：
 * - 同笔记其余尚未测量的图**一起**改成该比例（一次收敛，不再逐张跳）；
 * - 比例写进 localStorage，下次打开这篇笔记首帧就是对的。
 */
const ratioByNote = new Map<string, number>()
const ratioListeners = new Map<string, Set<() => void>>()

function ratioKey(noteKey: string) {
  return `bilinote-shot-ratio:${noteKey}`
}

function readRatio(noteKey: string): number | null {
  if (!noteKey) return null
  const mem = ratioByNote.get(noteKey)
  if (mem) return mem
  try {
    const v = Number(localStorage.getItem(ratioKey(noteKey)))
    // 正常视频截图比例在 0.2（竖屏 9:16 的反向）到 5（超宽）之间
    if (Number.isFinite(v) && v > 0.2 && v < 5) {
      ratioByNote.set(noteKey, v)
      return v
    }
  } catch {
    /* 隐私模式/配额异常：退回默认比例 */
  }
  return null
}

function subscribeRatio(noteKey: string, cb: () => void) {
  let set = ratioListeners.get(noteKey)
  if (!set) {
    set = new Set()
    ratioListeners.set(noteKey, set)
  }
  set.add(cb)
  return () => {
    set.delete(cb)
    if (set.size === 0) ratioListeners.delete(noteKey)
  }
}

function rememberRatio(noteKey: string, ratio: number) {
  if (!noteKey) return
  if (!(ratio > 0.2 && ratio < 5)) return
  const prev = ratioByNote.get(noteKey)
  // 同一篇里尺寸一致：差不到 0.2% 不重复通知（避免无谓的整篇重渲染）
  if (prev && Math.abs(prev - ratio) / ratio < 0.002) return
  ratioByNote.set(noteKey, ratio)
  try {
    localStorage.setItem(ratioKey(noteKey), String(ratio))
  } catch {
    /* 忽略 */
  }
  ratioListeners.get(noteKey)?.forEach(cb => cb())
}

interface NoteImageProps {
  /** 显示用 src（截图为后端 1600px 缩略） */
  src: string
  /** 缩略端点不可用时的回退原图 */
  fallbackSrc?: string
  alt?: string
  className?: string
  /** 当前笔记 id：用于按篇记忆截图比例 */
  noteKey?: string
  /** 笔记阅读区的滚动容器：作为 IntersectionObserver 的 root 用。
   *  用视口当 root 时，中间这层 scroll 容器的裁剪会吃掉 rootMargin，
   *  装载/卸载带退化成「仅可视区」，滚动时大量重新加载。 */
  root?: HTMLElement | null
}

/**
 * 笔记内图片（原片截图专用）：懒挂 + **离屏卸载**。
 *
 * 2026-10-18 白屏根治：旧实现 `loading="lazy"` 只负责「晚了加载」，对已经
 * 滚过的图片一个都不放——790 张笔记读完 ≈ 4.5GB 解码位图常驻，渲染进程
 * 被 OOM 杀掉后就是白屏（前端只能 Ctrl+R）。这里在图片离开可视区后
 * 卸载真 src（Chromium 随即释放这张图的解码内存），滚回来再挂上
 * （后端磁盘缓存 + 浏览器 HTTP 缓存，重新加载是毫秒级）。
 *
 * 2026-10-20 版面稳定 + 不遮挡正文：
 * 1. **固定比例占位盒**：外层 div 自己用 aspect-ratio 撑住高度，img 只是
 *    盒内 absolute 图层——挂/卸只增删 src 属性，任何时刻都不参与文档流，
 *    滚动不会因为「某张图卸载/加载」而上下跳。
 * 2. **关键尺寸全部内联**：正文样式 `.markdown-body img { height: auto;
 *    max-width: 100% }` 的特异性 (0,1,1) 高于 Tailwind 工具类 (0,1,0)，
 *    会压掉 `h-full`。宽窗口下占位盒被 max-height 截到 500px、图片却按
 *    自身比例算成 640px，绝对定位的图片直接溢出去盖住下面的文字（用户
 *    2026-10-20 实拍）。内联样式优先级最高，width/height/position 一律
 *    内联，不再依赖任何工具类胜出；图片严格等于占位盒。
 * 3. 比例按笔记记忆并一次收敛（见上方 ratioByNote）。
 */
export default function NoteImage({
  src,
  fallbackSrc,
  alt,
  className,
  noteKey = '',
  root,
}: NoteImageProps) {
  const exportMode = useSyncExternalStore(
    subscribeScreenshotExportMode,
    getScreenshotExportMode,
  )
  const ratioHint = useSyncExternalStore(
    useCallback((cb: () => void) => subscribeRatio(noteKey, cb), [noteKey]),
    useCallback(() => readRatio(noteKey), [noteKey]),
  )
  // 观察器挂在**始终存在**的占位盒上：img 卸载后若 ref 跟着消失，
  // 就再也收不到「滚回来」的通知了
  const { ref, inView } = useInView({
    root: root ?? undefined,
    rootMargin: BAND,
    // 首帧不预挂：长笔记一次挂几百张会把内存又打回去。
    // 观察器的首个回调在挂载后立刻到达，可视区内不会缺图。
    initialInView: false,
  })
  const [dims, setDims] = useState(() => {
    const known = dimsBySrc.get(src)
    if (known) return known
    return ratioHint ? { w: Math.round(ratioHint * 1000), h: 1000 } : DEFAULT_DIMS
  })
  const measuredRef = useRef(false)
  const failedRef = useRef(false)

  // 本篇第一张图量到比例后，其余没被单独测过的占位盒一起改成同一比例
  // （一次收敛，避免滚动时每张图各跳一次）
  useEffect(() => {
    if (measuredRef.current || !ratioHint) return
    setDims(prev => {
      if (Math.abs(prev.w / prev.h - ratioHint) / ratioHint < 0.002) return prev
      return { w: Math.round(ratioHint * 1000), h: 1000 }
    })
  }, [ratioHint])

  const attached = exportMode || inView
  const displaySrc = attached ? src : undefined

  return (
    <div
      ref={ref}
      data-note-shot-box="1"
      // 宽度/比例/上限全部内联：正文里的 img 规则特异性比工具类高，
      // 这里把盒子的高度来源固定住（宽度=正文列宽，高度=宽度/比例，封顶 500）
      style={{
        position: 'relative',
        width: '100%',
        aspectRatio: `${dims.w} / ${dims.h}`,
        maxHeight: MAX_H,
      }}
    >
      {/* img 必须始终存在：react-medium-image-zoom 在挂载时抓取 img 元素并
          直接绑 click 监听，条件渲染会让它抓到 null、点击放大失效。
          这里只动态增删 src 属性——同一个 DOM 节点，卸载也能释放解码内存。

          **不能**只靠 Tailwind 的 absolute/h-full/w-full：`.markdown-body img`
          的 height:auto 会把 h-full 压掉，宽列下图片比占位盒高出一截并盖住
          正文。下面这些尺寸一律内联，保证图片严格等于占位盒。 */}
      <img
        data-note-shot="1"
        {...(displaySrc !== undefined ? { src: displaySrc } : {})}
        width={dims.w}
        height={dims.h}
        alt={attached ? alt ?? '' : ''}
        // 装载带（±2500px）已经把范围限死了，这里要的是「进带就真加载」：
        // 交给浏览器原生 lazy 的话，图片可能拖到临近可视区才请求，笔记比例
        // 迟迟量不出来，首次打开时反而会在滚动中突然整篇改比例。
        loading="eager"
        decoding="async"
        // 缩略端点不可用（旧后端/缓存损坏）时回退原图：宁慢勿裂。
        // 用 ref 直接改 src（不触发 React 重渲染），避免与挂/卸逻辑打架
        onError={e => {
          const el = e.currentTarget as HTMLImageElement
          if (fallbackSrc && !failedRef.current && el.getAttribute('src') === src) {
            failedRef.current = true
            el.setAttribute('src', fallbackSrc)
          }
        }}
        onLoad={e => {
          const el = e.currentTarget as HTMLImageElement
          if (el.naturalWidth > 1 && el.naturalHeight > 1) {
            const w = el.naturalWidth
            const h = el.naturalHeight
            dimsBySrc.set(src, { w, h })
            measuredRef.current = true
            // 本图用精确尺寸；其余尚未测量的图通过 store 一起采用该比例
            setDims(prev => (prev.w === w && prev.h === h ? prev : { w, h }))
            rememberRatio(noteKey, w / h)
          }
        }}
        className={className}
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width: '100%',
          height: '100%',
          objectFit: 'contain',
          cursor: 'zoom-in',
        }}
      />
    </div>
  )
}
