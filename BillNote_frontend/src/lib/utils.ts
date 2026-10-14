import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * 把 AI 写出的各类 LaTeX 定界符统一成 remark-math 唯一能识别的两种：
 * 行内 `\(...\)` → `$...$`，块级 `\[...\]` → `$$...$$`。
 *
 * 后端入库时也会做同样的归一化（note_helper.normalize_math_delimiters），
 * 但历史笔记存在 IndexedDB 里是旧写法，渲染时再转一次即可正常显示，
 * 不用做数据迁移。与后端实现保持一致：只换定界符，不碰公式本体。
 */
export function normalizeMathDelimiters(markdown: string | null | undefined): string {
  if (!markdown) return markdown ?? ''
  // 块级：\[ ... \] → $$...$$（多行，非贪婪）
  const block = markdown.replace(/\\\[(.+?)\\\]/gs, (_, body) => `$$${body}$$`)
  // 行内：\( ... \) → $...$（单行，避免吞掉后面的正文）
  return block.replace(/\\\((.+?)\\\)/g, (_, body) => `$${body}$`)
}

/** 正文里的原片截图 markdown（含可能的链接包裹）。 */
const SCREENSHOT_IMG_RE = /!\[[^\]]*\]\([^)]*\/static\/screenshots\/[^)]*\)/g

/**
 * 「隐藏原片截图」查看模式用：把截图标记从正文里摘掉，只留说明文字。
 *
 * 背景（2026-10-14 用户反馈）：截图渲染时会套一层带大边距的 div，只靠 CSS
 * `display:none` 藏图片，那层边距还在——笔记里留下超长行距与莫名空行。
 * 因此隐藏模式直接在 markdown 层移除截图标记：
 * - 图片后紧跟的说明文字保留（如 `![](xx.jpg) 画面显示 loading` → `画面显示 loading`）；
 * - 列表项形如 `- ![](xx.jpg)：字幕“…”` 时，顺手去掉截图后残留的冒号；
 * - 整行只有截图（没有说明文字）的行直接丢掉，避免留下空行。
 *
 * 只影响屏幕查看：长图导出走 exportForceShow 用完整 markdown 重渲染，
 * PDF/Word 导出直接把原始 markdown 交给后端渲染，都不经过这里。
 */
export function stripScreenshotImages(markdown: string | null | undefined): string {
  if (!markdown || !markdown.includes('/static/screenshots/')) return markdown ?? ''
  const out: string[] = []
  for (const line of markdown.split('\n')) {
    if (!line.includes('/static/screenshots/')) {
      out.push(line)
      continue
    }
    const stripped = line
      .replace(SCREENSHOT_IMG_RE, '')
      // 截图后紧跟的冒号是「图：说明」的分隔符，图没了要一起去掉
      .replace(/^(\s*(?:[-*+]\s+|\d+\.\s+)?)[：:]\s*/, '$1')
      .replace(/[ \t]+$/, '')
    // 整行只有图 → 丢行；只剩一个列表符号（如 `-`）也丢，避免留下孤零零的破折号
    if (/^\s*(?:[-*+]|\d+\.)?\s*$/.test(stripped)) continue
    out.push(stripped)
  }
  return out.join('\n')
}

/** 笔记目录条目（从「## 目录」段落解析出来） */
export interface TocEntry {
  /** 条目自带编号（如 "3"），没有则 undefined */
  num?: string
  title: string
  /** 原片时间标签，如 "00:40" */
  time?: string
  /** 原片跳转链接（点击打开原视频对应时间点），没有则 undefined */
  href?: string
}

const TOC_ORIGIN_LINK_RE = /\[原片\s*@\s*(\d{1,3}:\d{2}(?::\d{2})?)\]\(([^)]+)\)/g
const TOC_MARKER_RE = /\*?Content-(?:\[(\d{1,3}:\d{2}(?::\d{2})?)\]|(\d{1,3}:\d{2}(?::\d{2})?))\*?/g

/** 解析单条目录项（「- 1. 标题 [原片 @ 00:40](url)」/「- 1\. 标题」等形态） */
function parseTocItem(raw: string): TocEntry | null {
  let text = raw.trim()
  let href: string | undefined
  let time: string | undefined

  let m: RegExpExecArray | null
  TOC_ORIGIN_LINK_RE.lastIndex = 0
  while ((m = TOC_ORIGIN_LINK_RE.exec(text))) {
    time = m[1]
    href = m[2]
  }
  if (href) text = text.replace(TOC_ORIGIN_LINK_RE, '')

  TOC_MARKER_RE.lastIndex = 0
  while ((m = TOC_MARKER_RE.exec(text))) {
    if (!time) time = m[1] || m[2]
  }
  if (!href) text = text.replace(TOC_MARKER_RE, '')

  // 整条就是一个链接（旧版锚点目录）：取链接文本与地址
  const whole = /^\[(.+?)\]\((.+?)\)\s*$/.exec(text.trim())
  if (whole) {
    text = whole[1]
    if (!href) href = whole[2]
  }

  text = text
    .replace(/\*\*?/g, '')
    .replace(/\\([.()[\]])/g, '$1')
    .trim()
  let num: string | undefined
  const nm = /^(\d{1,3})\s*[.、)]\s*/.exec(text)
  if (nm) {
    num = nm[1]
    text = text.slice(nm[0].length)
  }
  text = text.replace(/[：:]\s*$/, '').trim()
  if (!text) return null
  return { num, title: text, time, href }
}

/**
 * 把正文里「## 目录」段落抽出来单独渲染（2026-10-14 用户反馈：37 条目录
 * 单列铺满整屏，占地方且不优雅）。返回 {before, items, after}：
 * items 为 null 表示没有可解析的目录段，调用方按原样渲染整个 markdown。
 * 判定保守：标题为「目录」的 #~### 段，后面全是列表行，遇到下一个标题收尾；
 * 段内出现任何非列表内容（模型偶尔写导语）就整体放弃自定义渲染。
 */
export function extractTocSection(markdown: string): {
  before: string
  items: TocEntry[] | null
  after: string
} {
  const none = { before: markdown, items: null as TocEntry[] | null, after: '' }
  if (!markdown) return none
  const lines = markdown.split('\n')
  let headingIdx = -1
  for (let i = 0; i < lines.length; i++) {
    const m = /^(#{1,3})\s*(.+?)\s*$/.exec(lines[i])
    if (!m) continue
    if (m[2].replace(/[\s*：:]/g, '').toLowerCase().startsWith('目录')) {
      headingIdx = i
      break
    }
  }
  if (headingIdx < 0) return none

  const items: TocEntry[] = []
  let endIdx = lines.length
  for (let i = headingIdx + 1; i < lines.length; i++) {
    const line = lines[i]
    if (/^#{1,6}\s/.test(line)) {
      endIdx = i
      break
    }
    const t = line.trim()
    if (!t) continue
    const li = /^(?:[-*+]|\d+[.)])\s+(.+)$/.exec(t)
    if (!li) return none
    const entry = parseTocItem(li[1])
    if (entry) items.push(entry)
  }
  if (items.length < 2) return none
  return {
    before: lines.slice(0, headingIdx).join('\n'),
    items,
    after: lines.slice(endIdx).join('\n'),
  }
}
