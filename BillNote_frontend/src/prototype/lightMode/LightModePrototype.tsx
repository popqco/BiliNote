/**
 * 【原型】亮色配色评审切换器（2026-10-11，评审后整个 lightMode/ 目录删除）
 *
 * 挂在 RootLayout 内。开发构建下在页面底部中央浮一个高对比切换条：
 * ← / → 循环切换五个变体（现状对照 + A/B/C/D），键盘 ←/→ 同效，
 * 点击名称展开该方案的设计说明与参考来源；变体写入 ?variant=，可分享、刷新保持。
 * 仅亮色主题生效（暗色下只提示）；生产构建不渲染。
 */
import { useCallback, useEffect, useState } from 'react'
import { useTheme } from 'next-themes'
import './variants.css'
import {
  applyVariantClass,
  getVariantFromUrl,
  lightVariants,
  setVariantInUrl,
  type LightVariant,
} from './variants'

export default function LightModePrototype() {
  const { resolvedTheme, setTheme } = useTheme()
  const [variant, setVariant] = useState<LightVariant>(() => getVariantFromUrl())
  const [showInfo, setShowInfo] = useState(false)

  // 评审便利：URL 带非「现状」变体时自动切亮色，打开 ?variant=X 即见效果（不改 base 时的主题）
  useEffect(() => {
    if (variant.id !== 'base' && resolvedTheme === 'dark') setTheme('light')
  }, [variant.id, resolvedTheme, setTheme])

  useEffect(() => {
    if (resolvedTheme === 'dark') applyVariantClass(lightVariants[0])
    else applyVariantClass(variant)
  }, [resolvedTheme, variant])

  const cycle = useCallback((dir: 1 | -1) => {
    setVariant((cur) => {
      const idx = lightVariants.findIndex((v) => v.id === cur.id)
      const next = lightVariants[(idx + dir + lightVariants.length) % lightVariants.length]
      setVariantInUrl(next)
      return next
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
    <div
      className="fixed bottom-5 left-1/2 z-[9999] -translate-x-1/2 select-none"
      data-lm-prototype
    >
      {showInfo && !dark && (
        <div
          className="absolute bottom-full left-1/2 mb-2 w-80 -translate-x-1/2 rounded-xl p-3.5 text-xs leading-relaxed text-white/90 shadow-2xl"
          style={{ background: 'oklch(0.22 0 0 / 95%)', backdropFilter: 'blur(8px)' }}
        >
          <div className="mb-1 font-semibold text-white">{variant.name}</div>
          <div className="mb-2">{variant.desc}</div>
          {variant.refs.length > 0 && (
            <div className="text-white/60">
              参考：
              {variant.refs.join(' · ')}
            </div>
          )}
          <div className="mt-2 border-t border-white/10 pt-2 text-white/45">
            ←/→ 切换变体 · 变体参数在 URL ?variant= 上，可直接分享
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
          className="min-w-36 px-1 text-xs font-medium text-white"
          title="查看方案说明"
        >
          {dark ? '亮色配色原型（当前为暗色主题）' : variant.name}
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
