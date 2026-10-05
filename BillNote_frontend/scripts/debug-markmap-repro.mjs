// 诊断回路：真实笔记 markdown → stripMindmapImages（当前实现）→ markmap transform → 找空/垃圾节点
// 用法：node _repro_map.mjs <markdown路径> [fixed]
import { readFileSync } from 'node:fs'
import { Transformer } from 'markmap-lib'

const file = process.argv[2] || 'D:/Program Files/BiliNote/_debug_map/perfume_v0.md'
const mode = process.argv[3] || 'current'
const md = readFileSync(file, 'utf8')

function currentStrip(markdown) {
  return (markdown || '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/<img\b[^>]*>/gi, '')
}

function fixedStrip(markdown) {
  let s = (markdown || '')
    // 截图图片 + 斜体包裹残留的收尾星号一起剥
    .replace(/!\[[^\]]*\]\([^)]*\)[ \t]*\*{1,2}/g, '')
    // 无星号的普通图片
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/<img\b[^>]*>/gi, '')
    // 原片跳转链接行尾的收尾星号：`...?t=12)*`
    .replace(/(\]\([^)]*(?:\?t=|\/video\/)[^\)]*\))[ \t]*\*{1,2}(?=[ \t]*$)/gm, '$1')
    // 剥完只剩列表符/星号的行（空 li、孤立星号段）——markmap 会渲染成空节点
    .replace(/^[ \t]*[-*+]?[ \t]*\*{1,2}[ \t]*$/gm, '')
    .replace(/^[ \t]*[-*+][ \t]*$/gm, '')
  return s
}

const strip = mode === 'fixed' ? fixedStrip : currentStrip

// 拟新增的树剪枝：空内容节点用子节点原地顶替（无子节点则删除）
function pruneEmptyNodes(node, isRoot = false) {
  const content = (node.content || '')
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)))
    .replace(/<[^>]+>/g, '')
    .trim()
  const children = (node.children || []).map(c => pruneEmptyNodes(c)).flat()
  if (!content && !isRoot) return children
  return { ...node, children }
}

const transformer = new Transformer()
const rawRoot = transformer.transform(strip(md)).root
const root = mode === 'fixed' ? pruneEmptyNodes(rawRoot) : rawRoot

const emptyNodes = []
const asteriskTail = []
function walk(node, path) {
  const raw = node.content || ''
  const trimmed = raw.replace(/\s+/g, ' ').trim()
  if (!trimmed) emptyNodes.push({ path, content: JSON.stringify(raw).slice(0, 60) })
  else if (/^\*{1,2}$/.test(trimmed)) emptyNodes.push({ path, content: JSON.stringify(raw).slice(0, 60) })
  if (trimmed && /\*{1,2}$/.test(trimmed)) asteriskTail.push(trimmed.slice(0, 70))
  ;(node.children || []).forEach((c, i) => walk(c, `${path}/${i}`))
}
walk(root, '')

console.log(`mode=${mode}`)
console.log(`empty/junk nodes: ${emptyNodes.length}`)
emptyNodes.slice(0, 25).forEach(n => console.log('  EMPTY', n.path, n.content))
console.log(`asterisk-tail nodes: ${asteriskTail.length}`)
asteriskTail.slice(0, 12).forEach(n => console.log('  TAIL', n))
console.log(emptyNodes.length === 0 && asteriskTail.length === 0 ? 'GREEN' : 'RED')
