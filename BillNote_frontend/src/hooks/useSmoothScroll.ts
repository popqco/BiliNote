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

// 拖拽松手后的滑行外推时间（ms）：用松手前 ~120ms 的鼠标速度 × 该系数
// 得到总滑行距离，再交给当前档位的 lerp 指数衰减。与滚轮共用档位语义：
// 跟手=很快停，适中=滑一小段，动量=滑很远。
const TIER_FLING_MS: Record<SmoothScrollTier, number> = {
  direct: 250,
  medium: 550,
  momentum: 1100,
}

// 按下后位移超过该值才算拖动，否则是点击（链接/按钮/图片缩放照常）
const DRAG_THRESHOLD_PX = 6
// 速度采样窗口：松手惯性感来自最近这一段的鼠标速度
const FLING_SAMPLE_MS = 120
// 滑行距离上限（防甩飞）：不超过约 3 屏
const MAX_GLIDE_VIEWPORTS = 3

let finePointer: boolean | null = null
const isFinePointer = () => {
  if (finePointer === null) {
    finePointer = window.matchMedia('(pointer: fine)').matches
  }
  return finePointer
}

/**
 * 阅读区触摸式滚动体验：滚轮惯性 + 按住拖动跟随 + 松手滑行（Lenis 驱动）。
 *
 * 滚轮惯性
 * - 只在「设置开启 + 精细指针（鼠标/触控板）」时生效；触摸设备保持原生
 *   惯性（手机端本来就是这个手感），系统 reduce-motion 由 Lenis 自身降级。
 * - Lenis 驱动真实 scrollTop：滚动条拖动、键盘翻页、滚动事件监听都不受影响。
 * - 嵌套滚动隔离交给 lenis：allowNestedScroll 按「手势方向上有可滚内容」
 *   放行内层滚动区；纯横向手势（触控板横滑/shift+滚轮，Chromium 会把
 *   delta 换到 X 轴）直接走原生——代码块/宽表格横滚不会被劫持成页面滚动，
 *   纵向滚轮悬停在它们上面也照常滚页面。
 *
 * 按住拖动（左键，像手机那样抓着内容走）
 * - 按下后位移超过阈值才算拖动，小于阈值仍是点击（链接/按钮/图片缩放不受影响，
 *   被拖动吞掉的点击用一次性捕获监听抑制）。
 * - 1:1 跟踪鼠标轨迹；松手按最近 120ms 的速度外推滑行距离，交给当前档位
 *   的 lerp 指数衰减（跟手快停/动量滑远）。
 * - Shift+按住拖动不拦截，留给原生文字选择；拖动期间临时关掉 user-select
 *   并抑制链接/图片原生拖拽，双击选词（无位移）不受影响。
 * - 拖动开始后才 setPointerCapture：保证鼠标移出窗口也能持续跟踪并收到
 *   pointerup；提前捕获会把后续 click 重定向到视口、吞掉正常点击。
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

    // —— 按住拖动 + 松手惯性 ——
    let pointerId: number | null = null
    let startY = 0
    let top0 = 0
    let dragging = false
    let samples: Array<{ t: number; y: number }> = []
    let stylesApplied = false

    const applyDragStyles = () => {
      node.style.cursor = 'grabbing'
      node.style.userSelect = 'none'
      stylesApplied = true
    }
    const clearDragStyles = () => {
      if (!stylesApplied) return
      node.style.cursor = ''
      node.style.userSelect = ''
      stylesApplied = false
    }
    const preventNativeDrag = (e: DragEvent) => e.preventDefault()
    const suppressNextClick = () => {
      const kill = (e: MouseEvent) => {
        e.preventDefault()
        e.stopPropagation()
      }
      // once 语义 + 兜底自毁：pointerup 后 click 若没来（如在窗口外松手），
      // 不能让抑制器一直挂着吃掉之后一次正常点击
      window.addEventListener('click', kill, { capture: true, once: true })
      window.setTimeout(() => window.removeEventListener('click', kill, true), 300)
    }

    const onPointerMove = (e: PointerEvent) => {
      if (e.pointerId !== pointerId) return
      const now = performance.now()
      samples.push({ t: now, y: e.clientY })
      while (samples.length > 2 && now - samples[0].t > FLING_SAMPLE_MS) samples.shift()
      if (!dragging) {
        if (Math.abs(e.clientY - startY) < DRAG_THRESHOLD_PX) return
        dragging = true
        try {
          node.setPointerCapture(e.pointerId)
        } catch {
          /* 合成事件/重复捕获时可能抛错，跟踪不受影响 */
        }
        applyDragStyles()
        window.getSelection()?.removeAllRanges()
        window.addEventListener('dragstart', preventNativeDrag)
      }
      // 1:1 跟踪：内容跟着鼠标走（上拖 = 往下看，与手机抓纸语义一致）
      lenis.scrollTo(top0 + (startY - e.clientY), { immediate: true })
    }

    const endDrag = (e: PointerEvent, allowFling: boolean) => {
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', onPointerUp)
      window.removeEventListener('pointercancel', onPointerCancel)
      window.removeEventListener('dragstart', preventNativeDrag)
      const wasDragging = dragging
      if (e.pointerId === pointerId) {
        try {
          node.releasePointerCapture(e.pointerId)
        } catch {
          /* 未捕获过时忽略 */
        }
      }
      pointerId = null
      dragging = false
      clearDragStyles()
      if (!wasDragging) return
      suppressNextClick()
      if (!allowFling) return
      // 只用「松手时刻往前 FLING_SAMPLE_MS 内」的样本估速。采样只在
      // pointermove 里发生，鼠标停住后 move 停止、缓冲区会冻结在停住前
      // 的高速段——不按松手时刻重新裁剪的话，静止几秒再松手仍会按老速度
      // 整页飞出去（蓄力弹簧 bug）。静止超过窗口期 → 无足够样本 → 不滑行。
      const now = performance.now()
      const recent = samples.filter(s => now - s.t <= FLING_SAMPLE_MS)
      let velocity = 0 // px/ms，向上拖（往下看）为正
      if (recent.length >= 2) {
        const first = recent[0]
        const last = recent[recent.length - 1]
        const dt = last.t - first.t
        if (dt > 10) velocity = (first.y - last.y) / dt
      }
      const maxGlide = Math.min(4000, node.clientHeight * MAX_GLIDE_VIEWPORTS)
      const glide = Math.max(-maxGlide, Math.min(maxGlide, velocity * TIER_FLING_MS[tier]))
      if (Math.abs(glide) >= 24) {
        lenis.scrollTo(node.scrollTop + glide)
      }
    }
    const onPointerUp = (e: PointerEvent) => endDrag(e, true)
    const onPointerCancel = (e: PointerEvent) => endDrag(e, false)

    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0) return
      if (e.pointerType !== 'mouse' && e.pointerType !== 'pen') return
      if (e.shiftKey) return // Shift+拖 = 原生文字选择
      pointerId = e.pointerId
      startY = e.clientY
      top0 = node.scrollTop
      dragging = false
      samples = [{ t: performance.now(), y: e.clientY }]
      window.addEventListener('pointermove', onPointerMove)
      window.addEventListener('pointerup', onPointerUp)
      window.addEventListener('pointercancel', onPointerCancel)
    }

    node.addEventListener('pointerdown', onPointerDown)

    return () => {
      node.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', onPointerUp)
      window.removeEventListener('pointercancel', onPointerCancel)
      window.removeEventListener('dragstart', preventNativeDrag)
      clearDragStyles()
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
