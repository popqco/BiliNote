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
