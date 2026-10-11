/**
 * 【原型】亮色模式配色方案评审（2026-10-11）
 *
 * 问题：现亮色模式背景是纯白（oklch(1 0 0)），用户反馈太刺眼、配色不舒适。
 * 本原型在不动任何业务组件的前提下，用 CSS 变量覆盖 + antd token 覆盖
 * 提供四个参考成熟设计系统的亮色底色方向，供用户挑选后再正式实现。
 *
 * 五个变体（含对照），通过 URL ?variant= 切换，浮动切换条在底部：
 *   现状（纯白对照）→ A 暖纸奶油 → B 冷雾灰蓝 → C 豆沙绿 → D 奶咖低饱和
 *
 * 一次性代码：评审结论落进 src/index.css 的 :root 后，整个目录删除。
 */

export interface LightVariant {
  /** URL ?variant= 的值；'base' 表示现状 */
  id: string
  /** html 上的类名；base 无类名 */
  cssClass: string | null
  /** 切换条上显示的名字 */
  name: string
  /** 一句话设计说明（切换条信息卡展示） */
  desc: string
  /** 参考来源 */
  refs: string[]
  /** antd token 覆盖（仅亮色下生效） */
  antdToken?: Record<string, string>
}

export const lightVariants: LightVariant[] = [
  {
    id: 'base',
    cssClass: null,
    name: '现状 · 纯白',
    desc: '当前亮色模式（背景 oklch(1 0 0) 纯白），作对照用。',
    refs: [],
  },
  {
    id: 'A',
    cssClass: 'lm-warm',
    name: 'A · 暖纸奶油',
    desc: '奶油暖白底 + 暖灰文字，像纸不用纯白；信息层级靠米色深浅分层。',
    refs: ['Notion 亮色底 #F7F7F5', 'Obsidian「米黄护眼」主题'],
    antdToken: {
      colorBgBase: '#f8f5ee',
      colorBgLayout: '#f1ede3',
      colorBorderSecondary: '#e3ddcf',
    },
  },
  {
    id: 'B',
    cssClass: 'lm-mist',
    name: 'B · 冷雾灰蓝',
    desc: '冷调浅灰蓝底 + 白卡片，蓝灰文字；偏工具感，和现有蓝色主色最搭。',
    refs: ['Linear 亮色底 #F7F8F8', 'GitHub Primer 画布 #F6F8FA'],
    antdToken: {
      colorBgBase: '#f5f7f9',
      colorBgLayout: '#eef1f4',
      colorBorderSecondary: '#dfe4ea',
    },
  },
  {
    id: 'C',
    cssClass: 'lm-sage',
    name: 'C · 豆沙绿',
    desc: '经典护眼豆沙绿的现代化降饱和版，绿底绿卡片；中文用户最熟悉的护眼方向。',
    refs: ['护眼豆沙绿 #C7EDCC（降饱和重调）', 'Obsidian「护眼绿」主题'],
    antdToken: {
      colorBgBase: '#e9f0e8',
      colorBgLayout: '#e2ebe2',
      colorBorderSecondary: '#cddbcf',
    },
  },
  {
    id: 'D',
    cssClass: 'lm-greige',
    name: 'D · 奶咖低饱和',
    desc: '奶咖暖灰底 + 主色降饱和（蓝→雾蓝莫兰迪），整体最"静"，按钮不再扎眼。',
    refs: ['Things 3 亮色暖灰 #F6F5F4', 'Raycast 亮色 / 莫兰迪低饱和'],
    antdToken: {
      colorBgBase: '#f6f4f0',
      colorBgLayout: '#efede8',
      colorBorderSecondary: '#e0ddd5',
      colorPrimary: '#476a9c',
      colorLink: '#476a9c',
    },
  },
]

export function getVariantFromUrl(): LightVariant {
  const id = new URLSearchParams(window.location.search).get('variant')
  return lightVariants.find((v) => v.id === id) ?? lightVariants[0]
}

/** 应用/取消 html 类名，返回是否为非 base 变体 */
export function applyVariantClass(v: LightVariant): void {
  const root = document.documentElement
  root.classList.remove(...lightVariants.map((x) => x.cssClass).filter(Boolean) as string[])
  root.classList.remove('lm-proto')
  if (v.cssClass) {
    root.classList.add(v.cssClass, 'lm-proto')
  }
}

/** 更新 URL（replaceState，不打历史记录）并广播变化 */
export function setVariantInUrl(v: LightVariant): void {
  const url = new URL(window.location.href)
  if (v.id === 'base') url.searchParams.delete('variant')
  else url.searchParams.set('variant', v.id)
  window.history.replaceState(null, '', url.toString())
  window.dispatchEvent(new CustomEvent('lm-variant-change'))
}
