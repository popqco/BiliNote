/**
 * 【原型】暗色模式配色方案评审（2026-10-15）
 *
 * 问题：现暗色是 shadcn 默认值——底 oklch(0.145 0 0)≈#0a0a0a 近纯黑、
 * 字 oklch(0.985 0 0)≈#fafafa 近纯白，正文对比度 18.96:1，用户反馈刺眼。
 * 业界共识（Material / GitHub Primer / Radix / HIG）：暗色底不宜纯黑、
 * 文字亮度应降到 8~13:1 区间，次级文字仍需 ≥4.5:1；主色在暗底上降饱和。
 *
 * 五个变体（含对照），通过 URL ?variant= 切换，浮动切换条在底部：
 *   现状（对照）→ A 冷灰蓝(低对比) → B 中性石墨 → C 暖夜奶咖 → D 层叠微紫
 * 对比度数字由 work/darkmode-palette-calc.py（oklch→sRGB + WCAG 公式）计算，
 * 全部变体的次级文字都 ≥4.5:1、深色字压主色按钮 ≥6:1。
 *
 * 一次性代码：评审结论落进 src/index.css 的 .dark 后，整个目录删除。
 */

export interface DarkVariant {
  /** URL ?variant= 的值；'base' 表示现状 */
  id: string
  /** html 上的类名；base 无类名。规则都带 .dark 前提，仅暗色下生效 */
  cssClass: string | null
  /** 切换条上显示的名字 */
  name: string
  /** 一句话设计说明（切换条信息卡展示） */
  desc: string
  /** 正文对比度（脚本计算值，信息卡展示） */
  contrast: string
  /** 参考来源 */
  refs: string[]
  /** antd / @ant-design/x 暗色 token 覆盖（Bubble/Sender 不读 CSS 变量） */
  antdToken?: Record<string, string>
}

export const darkVariants: DarkVariant[] = [
  {
    id: 'base',
    cssClass: null,
    name: '现状 · 近纯黑底+近纯白字',
    desc: '当前暗色（shadcn 默认）：正文对比度 18.96:1，就是这次要解决的刺眼来源；其余四个方案均为其对照。',
    contrast: '18.96:1',
    refs: [],
  },
  {
    id: 'A',
    cssClass: 'dm-dim',
    name: 'A · 冷灰蓝（低对比）',
    desc: '整体抬高底色亮度、压低文字亮度，正文降到 8.2:1，雾面阅读感，长时间看最省眼；代价是观感偏"灰"。',
    contrast: '8.22:1',
    refs: ['GitHub Dark Dimmed（官方为暗环境推出的低对比主题）', 'Linear 暗色'],
    antdToken: {
      colorBgBase: '#1e2327',
      colorBgLayout: '#1e2327',
      colorBgContainer: '#252b30',
      colorBgElevated: '#2a3035',
      colorBorderSecondary: '#3c4349',
      colorPrimary: '#599fd8',
      colorLink: '#599fd8',
      colorTextBase: '#b2bcc2',
    },
  },
  {
    id: 'B',
    cssClass: 'dm-graphite',
    name: 'B · 中性石墨',
    desc: '中性微冷石墨底（#1a1a1b/#242425），文字降到 10.8:1；VS Code / Obsidian 同类观感，最接近"标准暗色"但不刺眼。',
    contrast: '10.83:1',
    refs: ['VS Code Dark+（#1e1e1e 底 / #d4d4d4 字）', 'Obsidian 默认暗色'],
    antdToken: {
      colorBgBase: '#1a1a1b',
      colorBgLayout: '#1a1a1b',
      colorBgContainer: '#242425',
      colorBgElevated: '#292a2c',
      colorBorderSecondary: '#38393b',
      colorPrimary: '#719ad6',
      colorLink: '#719ad6',
      colorTextBase: '#cacccf',
    },
  },
  {
    id: 'C',
    cssClass: 'dm-warm',
    name: 'C · 暖夜奶咖',
    desc: '暖灰底 + 暖调米白字，与亮色定版「奶咖」同源；整体去冷蓝光，接近纸质夜读观感。正文 11.4:1。',
    contrast: '11.36:1',
    refs: ['亮色定版「奶咖低饱和」同源', 'Notion 暗色 #191919', '微信读书夜间'],
    antdToken: {
      colorBgBase: '#1f1b17',
      colorBgLayout: '#1f1b17',
      colorBgContainer: '#282420',
      colorBgElevated: '#2e2a25',
      colorBorderSecondary: '#3a3631',
      colorPrimary: '#799acb',
      colorLink: '#799acb',
      colorTextBase: '#d8d2c7',
    },
  },
  {
    id: 'D',
    cssClass: 'dm-m3',
    name: 'D · 层叠微紫',
    desc: 'Material 3 官方暗色思路：底色带极淡紫（#1f1d23），面板靠亮度分层，文字亮但主色充分降饱和。层级感最强。正文 12.6:1。',
    contrast: '12.57:1',
    refs: ['Material Design 3 dark（#1C1B1F / onSurface #E6E1E5 / 主色 tone-80）', 'Discord 暗色'],
    antdToken: {
      colorBgBase: '#1f1d23',
      colorBgLayout: '#1f1d23',
      colorBgContainer: '#2a272f',
      colorBgElevated: '#302d36',
      colorBorderSecondary: '#434149',
      colorPrimary: '#97c2f0',
      colorLink: '#97c2f0',
      colorTextBase: '#e0dee4',
    },
  },
]

export function getVariantFromUrl(): DarkVariant {
  const id = new URLSearchParams(window.location.search).get('variant')
  return darkVariants.find((v) => v.id === id) ?? darkVariants[0]
}

/** 应用/取消 html 类名（不碰 next-themes 的 .dark），返回是否为非 base 变体 */
export function applyVariantClass(v: DarkVariant): void {
  const root = document.documentElement
  root.classList.remove(...(darkVariants.map((x) => x.cssClass).filter(Boolean) as string[]))
  root.classList.remove('dm-proto')
  if (v.cssClass) {
    root.classList.add(v.cssClass, 'dm-proto')
  }
}

/** 更新 URL（replaceState，不打历史记录）并广播变化 */
export function setVariantInUrl(v: DarkVariant): void {
  const url = new URL(window.location.href)
  if (v.id === 'base') url.searchParams.delete('variant')
  else url.searchParams.set('variant', v.id)
  window.history.replaceState(null, '', url.toString())
  window.dispatchEvent(new CustomEvent('dm-variant-change'))
}
