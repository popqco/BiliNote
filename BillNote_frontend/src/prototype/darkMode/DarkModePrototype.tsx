/**
 * 【原型】暗色配色评审切换器（2026-10-15，评审后整个 darkMode/ 目录删除）
 *
 * 挂在 RootLayout 内。开发构建下在页面底部中央浮一个高对比切换条：
 * ← / → 循环切换五个变体（现状对照 + A/B/C/D），键盘 ←/→ 同效，
 * 点击名称展开该方案的设计说明、正文对比度与参考来源；变体写入 ?variant=，
 * 可分享、刷新保持。选非对照变体时自动切到暗色主题（原主题存 localStorage，
 * 但变体切换不改写；回「现状」后仍可用右上角主题按钮自行切回）。
 * 生产构建不渲染。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTheme } from 'next-themes'
import './variants.css'
import {
  applyVariantClass,
  darkVariants,
  getVariantFromUrl,
  setVariantInUrl,
  type DarkVariant,
} from './variants'

export default function DarkModePrototype() {
  const { resolvedTheme, setTheme } = useTheme()
  const [variant, setVariant] = useState<DarkVariant>(() => getVariantFromUrl())
  const [showInfo, setShowInfo] = useState(false)
  const restoredTheme = useRef(false)

  // 评审便利：URL 带非「现状」变体时自动切暗色；回到「现状」时恢复进入前的主题
  const prevTheme = useRef<string | null>(null)
  useEffect(() => {
    if (variant.id !== 'base' && resolvedTheme === 'light') {
      if (!restoredTheme.current) {
        prevTheme.current = resolvedTheme
        restoredTheme.current = true
      }
      setTheme('dark')
    } else if (variant.id === 'base' && restoredTheme.current && prevTheme.current) {
      // 仅当用户停留在「现状」时恢复，避免打断手动切主题
      restoredTheme.current = false
    }
  }, [variant.id, resolvedTheme, setTheme])

  useEffect(() => {
    applyVariantClass(variant)
  }, [variant])

  // URL 同步与广播放到 effect 里：StrictMode 会双调用 updater，之前把
  // setVariantInUrl 放进 setVariant 的 updater 里，会触发跨组件 setState
  // 打断本次渲染，导致状态更新被丢弃（实测：URL 变了但类名没挂上）。
  useEffect(() => {
    setVariantInUrl(variant)
  }, [variant])

  const cycle = useCallback((dir: 1 | -1) => {
    setVariant((cur) => {
      const idx = darkVariants.findIndex((v) => v.id === cur.id)
      return darkVariants[(idx + dir + darkVariants.length) % darkVariants.length]
    })
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (e.key === 'ArrowLeft') {
        e.preventDefault()
        cycle(-1)
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        cycle(1)
      } else if (e.key === 'Escape') {
        setShowInfo(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cycle])

  if (import.meta.env.PROD) return null

  const dark = resolvedTheme === 'dark'

  return (
    <div className="fixed bottom-5 left-1/2 z-[9999] -translate-x-1/2 select-none" data-dm-prototype>
      {showInfo && (
        <div
          className="absolute bottom-full left-1/2 mb-2 w-96 -translate-x-1/2 rounded-xl p-3.5 text-xs leading-relaxed text-white/90 shadow-2xl"
          style={{ background: 'oklch(0.22 0 0 / 95%)', backdropFilter: 'blur(8px)' }}
        >
          <div className="mb-1 font-semibold text-white">{variant.name}</div>
          <div className="mb-2">{variant.desc}</div>
          {variant.refs.length > 0 && (
            <div className="text-white/60">参考：{variant.refs.join(' · ')}</div>
          )}
          <div className="mt-2 border-t border-white/10 pt-2 text-white/45">
            ←/→ 切换变体 · 变体参数在 URL ?variant= 上，可直接分享 · 暗色配色原型
          </div>
        </div>
      )}
      <div
        className="flex items-center gap-1 rounded-full py-1.5 pl-2 pr-3 shadow-2xl"
        style={{ background: 'oklch(0.22 0 0 / 92%)', backdropFilter: 'blur(8px)' }}
      >
        <span className="mr-1 rounded-full bg-white/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white/70">
          原型
        </span>
        <button
          type="button"
          onClick={() => cycle(-1)}
          className="flex h-7 w-7 items-center justify-center rounded-full text-white/80 transition-colors hover:bg-white/15 hover:text-white"
          aria-label="上一个配色方案"
        >
          ←
        </button>
        <button
          type="button"
          onClick={() => setShowInfo((s) => !s)}
          className="min-w-56 px-1 text-xs font-medium text-white"
          title="查看方案说明"
        >
          {dark ? variant.name : `暗色配色原型（当前为亮色主题，可切右上角月亮）`}
        </button>
        <button
          type="button"
          onClick={() => cycle(1)}
          className="flex h-7 w-7 items-center justify-center rounded-full text-white/80 transition-colors hover:bg-white/15 hover:text-white"
          aria-label="下一个配色方案"
        >
          →
        </button>
      </div>
    </div>
  )
}
