import { useEffect, useState } from 'react'

/** 移动端断点：窄于 768px 视为手机（与 Tailwind md 断点对齐）。 */
const MOBILE_BREAKPOINT = 768

/**
 * 是否手机视口：首屏用 matchMedia 初值（避免闪一下桌面布局），
 * 再监听 resize/variant 变化。SSR 不存在，直接返回 false 即可（本项目纯 CSR）。
 */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState<boolean>(() =>
    typeof window !== 'undefined' && typeof window.matchMedia !== 'undefined'
      ? window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`).matches
      : false,
  )

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia === 'undefined') return
    const mq = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`)
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches)
    // 初次挂载再对齐一次（横竖屏切换后新开页面等边缘场景）
    setIsMobile(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return isMobile
}
