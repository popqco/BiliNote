// 全量语料回归：note_results/*.json 的最终 markdown 过"净化+剪枝"管线
// 断言：1) 树中无空/垃圾节点  2) 原文每个标题行和列表项文本都保留为树节点
// 用法：node _corpus_check.mjs <dir> [limit]
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { Transformer } from 'markmap-lib'

const dir = process.argv[2] || 'D:/Program Files/BiliNote/note_results'
const limit = Number(process.argv[3] || 0)
const transformer = new Transformer()

function fixedStrip(markdown) {
  return (markdown || '')
    .replace(/!\[[^\]]*\]\([^)]*\)[ \t]*\*{1,2}/g, '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/<img\b[^>]*>/gi, '')
    .replace(/(\]\([^)]*(?:\?t=|\/video\/)[^\)]*\))[ \t]*\*{1,2}(?=[ \t]*$)/gm, '$1')
    .replace(/^[ \t]*[-*+]?[ \t]*\*{1,2}[ \t]*$/gm, '')
    .replace(/^[ \t]*[-*+][ \t]*$/gm, '')
}

function decodeForCheck(s) {
  return (s || '')
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)))
    .replace(/<[^>]+>/g, '')
    .trim()
}

function pruneEmptyNodes(node, isRoot = false) {
  const content = decodeForCheck(node.content)
  const children = (node.children || []).map(c => pruneEmptyNodes(c)).flat()
  if (!content && !isRoot) return children
  return { ...node, children }
}

function collectContents(node, acc) {
  const c = decodeForCheck(node.content)
  if (c) acc.push(c)
  ;(node.children || []).forEach(ch => collectContents(ch, acc))
  return acc
}

// 从 markdown 提取期望文本：标题行 + 列表项
function expectedTexts(md) {
  const out = []
  for (const raw of md.split(/\r?\n/)) {
    const line = raw.trim()
    let m
    if ((m = line.match(/^#{1,6}\s+(.*)$/))) out.push(m[1])
    else if ((m = line.match(/^[-*+]\s+(.*)$/))) out.push(m[1])
    else if ((m = line.match(/^\d+\.\s+(.*)$/))) out.push(m[1])
  }
  // 归一化：剥 markdown 行内语法（加粗/斜体/链接/行内代码），去空白
  return out
    .map(t => t
      .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/[*_`~]+/g, '')
      .replace(/<img\b[^>]*>/gi, '')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
      .replace(/\s+/g, '')
    )
    .filter(t => t.length > 0)
}

const files = readdirSync(dir).filter(f => f.endsWith('.json') && !f.includes('.status.')).sort()
const selected = limit ? files.slice(0, limit) : files
function pipeline(md, mode) {
  const stripped = mode === 'current'
    ? (md || '').replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/<img\b[^>]*>/gi, '')
    : fixedStrip(md)
  const { root: rawRoot } = transformer.transform(stripped)
  return mode === 'current' ? rawRoot : pruneEmptyNodes(rawRoot, true)
}
let redFiles = 0
let totalEmpty = 0
let regressed = 0
const problems = []
for (const f of selected) {
  let md = ''
  try {
    const j = JSON.parse(readFileSync(join(dir, f), 'utf8'))
    md = j.markdown || ''
  } catch { continue }
  if (!md.trim()) continue
  const cur = collectContents(pipeline(md, 'current'), [])
  const fix = collectContents(pipeline(md, 'fixed'), [])
  // 修复后不应有空/垃圾节点
  const emptyCount = fix.filter(c => !c.replace(/\s+/g, '')).length
  const junkCount = fix.filter(c => /^\*{1,2}$/.test(c)).length
  // 内容回归检查：fixed 缺失的文本必须是 current 也缺失的（即 markmap 固有行为），否则为回归
  const exp = new Set(expectedTexts(md))
  const norm = arr => new Set(arr.map(c => c.replace(/\s+/g, '')))
  const curSet = norm(cur)
  const fixSet = norm(fix)
  const newMissing = [...exp].filter(t => !fixSet.has(t) && curSet.has(t))
  if (emptyCount || junkCount || newMissing.length) {
    redFiles++
    totalEmpty += emptyCount + junkCount
    regressed += newMissing.length
    problems.push({ f: f.slice(0, 14), emptyCount, junkCount, newMissing: newMissing.slice(0, 4) })
  }
}
console.log(`corpus: ${selected.length} notes checked`)
console.log(`RED files: ${redFiles}, empty/junk nodes: ${totalEmpty}, REGRESSED texts (lost vs current): ${regressed}`)
problems.slice(0, 15).forEach(p => console.log(JSON.stringify(p)))
console.log(redFiles === 0 ? 'GREEN' : 'RED')
