/**
 * 章节定位规则（纯逻辑，不依赖 React）。
 *
 * 从这里往外只暴露字符串/元素匹配：时间后缀识别、标题规范化、复合标题与
 * 最长公共子串兜底。MarkdownViewer 只负责取 DOM 与滚动，不再内嵌这套规则。
 */

const ORIGIN_TIME = /原片\s*[（(](\d{1,3}):(\d{2})(?::(\d{2}))?[）)]/
// 裸尾缀锚定行尾：避免误吃正文中段的括号数字；「(16:9)」这类比例
// 因秒位要求两位数字天然不命中
const BARE_TAIL_TIME = /[（(](\d{1,3}):(\d{2})(?::(\d{2}))?[）)]\s*$/

const HEADINGS = 'h1, h2, h3, h4, h5, h6'

export const timeSuffixToSeconds = (m: RegExpMatchArray): number => {
  const a = parseInt(m[1], 10)
  const b = parseInt(m[2], 10)
  const c = m[3] ? parseInt(m[3], 10) : null
  return c === null ? a * 60 + b : a * 3600 + b * 60 + c
}

/** 时间片段两边同口径剥掉再比：heading 里的「原片（00:03）」pill 与目录尾缀 */
export const stripTimeBits = (s: string): string =>
  s
    .replace(/原片\s*[（(]\s*\d{1,3}:\d{2}(?::\d{2})?\s*[）)]/g, '')
    .replace(/[（(]\s*\d{1,3}:\d{2}(?::\d{2})?\s*[）)]/g, '')

export const normalizeSectionTitle = (s: string): string =>
  stripTimeBits(s).replace(/[-：:\s*[\]]/g, '').toLowerCase()

export const isTopSection = (title: string): boolean =>
  ['intro', '开头'].includes(title.trim())

/** 从笔记 DOM 推断“覆盖某时间点”的章节标题（≤ seconds 的最大时间戳）。 */
export const findSectionTitleForTime = (
  seconds: number,
  root: ParentNode | null,
): string | null => {
  if (!root) return null
  let best: { el: Element; time: number } | null = null
  for (const h of root.querySelectorAll(HEADINGS)) {
    const text = h.textContent || ''
    const m = text.match(ORIGIN_TIME) || text.match(BARE_TAIL_TIME)
    if (!m) continue
    const t = timeSuffixToSeconds(m)
    if (t <= seconds + 1 && (!best || t > best.time)) {
      best = { el: h, time: t }
    }
  }
  if (!best) return null
  // 去掉时间戳后缀再当 section_title 用，避免匹配串过长
  const cleaned = (best.el.textContent || '')
    .replace(ORIGIN_TIME, '')
    .replace(BARE_TAIL_TIME, '')
    .replace(/[\s*]+$/, '')
    .trim()
  return cleaned || null
}

export const lcsLen = (a: string, b: string): number => {
  if (!a || !b) return 0
  let prev: number[] = new Array(b.length + 1).fill(0)
  let best = 0
  for (let i = 1; i <= a.length; i++) {
    const cur: number[] = new Array(b.length + 1).fill(0)
    for (let j = 1; j <= b.length; j++) {
      if (a.charCodeAt(i - 1) === b.charCodeAt(j - 1)) {
        cur[j] = prev[j - 1] + 1
        if (cur[j] > best) best = cur[j]
      }
    }
    prev = cur
  }
  return best
}

/** 规范化后的严格 substring 匹配；空标题不参与（includes('') 恒真）。 */
export const findStrictHeading = (search: string, headings: Iterable<Element>): Element | null => {
  for (const h of headings) {
    const norm = normalizeSectionTitle(h.textContent || '')
    if (norm && (norm.includes(search) || search.includes(norm))) return h
  }
  return null
}

/**
 * 目录/引用标题是模型转写的自由文本，常与正文标题措辞不一致（改写、两节合并）。
 * 严格 substring 失败后的兜底：先按「A / B」拆复合标题逐段严格匹配，再退到
 * 最长公共子串（≥8 个规范化字符、且唯一最佳领先亚军 ≥2）。
 */
export const findSectionElement = (
  rawTitle: string,
  root: ParentNode | null,
): Element | null => {
  const scope = root || document
  const cands: { el: Element; norm: string }[] = []
  for (const h of scope.querySelectorAll(HEADINGS)) {
    const norm = normalizeSectionTitle(h.textContent || '')
    if (norm) cands.push({ el: h, norm })
  }
  const findByStrict = (q: string) =>
    cands.find(c => c.norm.includes(q) || q.includes(c.norm)) || null
  const parts = rawTitle
    .split(/\s*[/／]\s*/)
    .map(p => p.trim())
    .filter(Boolean)
  if (parts.length > 1) {
    for (const p of parts) {
      const q = normalizeSectionTitle(p)
      if (!q) continue
      const m = findByStrict(q)
      if (m) return m.el
    }
  }
  const q = normalizeSectionTitle(rawTitle)
  if (!q) return null
  const scored = cands
    .map(c => ({ el: c.el, score: lcsLen(q, c.norm) }))
    .sort((x, y) => y.score - x.score)
  if (!scored.length || scored[0].score < 8) return null
  if (scored.length > 1 && scored[0].score - scored[1].score < 2) return null
  return scored[0].el
}
