import { useRef, useState, useSyncExternalStore } from 'react'
import { useInView } from 'react-intersection-observer'
import {
  getScreenshotExportMode,
  subscribeScreenshotExportMode,
} from '@/utils/screenshotLoadPolicy'

/** 装载/卸载带：可视区外 ±BAND px 内挂真 src，超出即卸载 */
const BAND = '2500px 0px 2500px 0px'

/** 首帧兜底尺寸：截图绝大多数是 16:9，加载过一次后换成真实宽高 */
const DEFAULT_DIMS = { w: 1600, h: 900 }

interface NoteImageProps {
  /** 显示用 src（截图为后端 1600px 缩略） */
  src: string
  /** 缩略端点不可用时的回退原图 */
  fallbackSrc?: string
  alt?: string
  className?: string
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
 * **直接摘掉 src 属性**（Chromium 随即释放这张图的解码内存），滚回来再挂上
 * （后端磁盘缓存 + 浏览器 HTTP 缓存，重新加载是毫秒级）。
 *
 * 卸载用「摘 src」而不是换 1×1 透明图：换占位图后浏览器以那张图的真实
 * 内在尺寸（1×1）参与布局，显式 width/height 属性只在图片不可用时才生效
 * ——flex 居中容器里的 shrink-to-fit 子项会整块塌成一条线（实测）。
 * 摘掉 src 后图片回到「不可用」态，width/height 属性提供内在尺寸，
 * 版面在挂载/卸载间保持稳定；alt 同步置空，避免卸载态露出替代文字。
 */
export default function NoteImage({ src, fallbackSrc, alt, className, root }: NoteImageProps) {
  const exportMode = useSyncExternalStore(
    subscribeScreenshotExportMode,
    getScreenshotExportMode,
  )
  // initialInView=true：首帧直接加载（行为与旧版一致），观察结果回来后再
  // 决定是否卸载；否则首帧会先卸再装，白白抖一下
  const { ref, inView } = useInView({
    root: root ?? undefined,
    rootMargin: BAND,
    initialInView: true,
  })
  const [dims, setDims] = useState(DEFAULT_DIMS)
  const failedRef = useRef(false)

  const attached = exportMode || inView
  const displaySrc = attached ? src : undefined

  return (
    <img
      ref={ref}
      data-note-shot="1"
      {...(displaySrc !== undefined ? { src: displaySrc } : {})}
      width={dims.w}
      height={dims.h}
      alt={attached ? alt ?? '' : ''}
      loading="lazy"
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
          setDims(prev => (prev.w === w && prev.h === h ? prev : { w, h }))
        }
      }}
      className={className}
      style={{ maxHeight: '500px' }}
    />
  )
}
