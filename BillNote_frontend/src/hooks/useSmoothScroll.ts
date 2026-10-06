import { useCallback, useEffect, useRef, useState } from 'react'
import Lenis from 'lenis'
import 'lenis/dist/lenis.css'
import { useSystemStore, type SmoothScrollTier } from '@/store/configStore'

// 三档手感（lerp：每帧向目标位置指数逼近的比例，越小滑行越远、停得越慢）：
// - direct 跟手：收敛快，接近原生但消除一格一顿的跳变
// - medium 适中：Lenis 默认附近，连贯不飘
// - momentum 动量：滑行明显，接近手机松手后的动量
const TIER_PARAMS: Record<SmoothScrollTier, { lerp: number; wheelMultiplier: number }> = {
  direct: { lerp: 0.25, wheelMultiplier: 1 },
  medium: { lerp: 0.1, wheelMultiplier: 1 },
  momentum: { lerp: 0.05, wheelMultiplier: 1.15 },
}

let finePointer: boolean | null = null
const isFinePointer = () => {
  if (finePointer === null) {
    finePointer = window.matchMedia('(pointer: fine)').matches
  }
  return finePointer
}

/**
 * 阅读区滚轮惯性平滑：把 ScrollArea 的 Viewport 包进 Lenis。
 *
 * - 只在「设置开启 + 精细指针（鼠标/触控板）」时生效；触摸设备保持原生
 *   惯性（手机端本来就是这个手感），系统 reduce-motion 由 Lenis 自身降级。
 * - Lenis 驱动真实 scrollTop：滚动条拖动、键盘翻页、滚动事件监听都不受影响。
 * - 嵌套滚动隔离交给 lenis：allowNestedScroll 按「手势方向上有可滚内容」
 *   放行内层滚动区；纯横向手势（触控板横滑/shift+滚轮，Chromium 会把
 *   delta 换到 X 轴）直接走原生——代码块/宽表格横滚不会被劫持成页面滚动，
 *   纵向滚轮悬停在它们上面也照常滚页面。
 *
 * 返回：
 * - attach：挂到滚动容器元素上（配合 ScrollArea 的 viewportRef）；
 * - lenisRef：Lenis 实例（编程式滚动必须走 scrollTo 辅助，直接
 *   element.scrollTo 会和进行中的惯性动画互相拉扯）；
 * - scrollTo：编程式滚动统一入口，无 Lenis 时退回原生平滑滚动。
 */
export function useSmoothScroll() {
  const lenisRef = useRef<Lenis | null>(null)
  const nodeRef = useRef<HTMLElement | null>(null)
  const [node, setNode] = useState<HTMLElement | null>(null)
  const enabled = useSystemStore(s => s.smoothScrollEnabled)
  const tier = useSystemStore(s => s.smoothScrollTier)

  useEffect(() => {
    if (!node || !enabled || !isFinePointer()) return
    // Radix Viewport 的第一个子元素是内容包裹层，显式传给 Lenis 量尺寸
    const lenis = new Lenis({
      wrapper: node,
      content: (node.firstElementChild as HTMLElement) ?? node,
      autoRaf: true,
      allowNestedScroll: true,
      ...TIER_PARAMS[tier],
    })
    lenisRef.current = lenis
    return () => {
      lenis.destroy()
      if (lenisRef.current === lenis) lenisRef.current = null
    }
  }, [node, enabled, tier])

  const scrollTo = useCallback(
    (el: HTMLElement, top: number, opts?: { immediate?: boolean }) => {
      const lenis = lenisRef.current
      // 只在 el 就是当前被包裹的视口时走 Lenis：跳转逻辑从 DOM 反查到的
      // viewport 可能是别的滚动容器（如文档级兜底搜索命中转写面板）。
      if (lenis && nodeRef.current === el) {
        lenis.scrollTo(top, { immediate: opts?.immediate })
      } else {
        el.scrollTo({ top, behavior: opts?.immediate ? 'auto' : 'smooth' })
      }
    },
    [],
  )

  const attach = useCallback((n: HTMLElement | null) => {
    nodeRef.current = n
    setNode(n)
  }, [])

  return { lenisRef, attach, scrollTo }
}
