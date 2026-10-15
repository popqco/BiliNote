import { FC } from 'react'
import { ListTree, Play } from 'lucide-react'
import type { TocEntry } from '@/lib/utils'
import { useTaskStore } from '@/store/taskStore'
import { useChatJumpStore } from '@/store/chatStore'

/**
 * 笔记「目录」的紧凑渲染（2026-10-14 用户反馈：三四十条的单列目录铺满整屏）。
 * - 多列「按列填充」（CSS columns）：条目沿第一列往下排满再进第二列——
 *   按行填充时读序在列间来回折返，长目录读起来很累（2026-10-15 用户实拍，
 *   53 条要折返 18 次），按列填充则是三段连续下行；
 * - 点击条目 → 走问答来源徽章同一套定位机制跳到对应章节（自动高亮 2s）；
 * - 左侧序号：条目自带编号优先，没有就按阅读顺序补序号（列填序 = 读序）；
 * - 带原片时间入口的条目，行尾显示时间徽标，点击直接开原视频对应时间点。
 */
const TocPanel: FC<{ items: TocEntry[] }> = ({ items }) => {
  const jumpToSection = (title: string) => {
    const tid = useTaskStore.getState().currentTaskId
    if (!tid || !title) return
    useChatJumpStore.getState().requestJump({ task_id: tid, section_title: title })
  }

  return (
    <div className="border-border/60 bg-muted/30 my-3 rounded-xl border px-4 py-3">
      <div className="mb-2 flex items-center gap-2">
        <ListTree className="text-primary h-4 w-4" />
        <span className="text-primary text-sm font-semibold">目录</span>
        <span className="text-muted-foreground text-xs">{items.length} 节 · 点击跳转</span>
      </div>
      <ol className="list-none gap-x-6 sm:columns-2 xl:columns-3">
        {items.map((it, idx) => (
          <li key={`${it.title}-${idx}`} className="mb-0.5 min-w-0 break-inside-avoid">
            <div
              role="button"
              tabIndex={0}
              title={it.title}
              onClick={() => jumpToSection(it.title)}
              onKeyDown={e => {
                if (e.key === 'Enter') jumpToSection(it.title)
              }}
              className="hover:bg-accent group flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-[3px] transition-colors"
            >
              <span className="text-muted-foreground w-5 shrink-0 text-right text-[11px] tabular-nums">
                {it.num ?? idx + 1}
              </span>
              <span className="group-hover:text-primary truncate text-[13px] leading-5">
                {it.title}
              </span>
              {it.time && it.href ? (
                <a
                  href={it.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={e => e.stopPropagation()}
                  className="ml-auto inline-flex shrink-0 items-center gap-0.5 rounded-full bg-blue-50 px-1.5 py-px text-[11px] font-medium text-blue-700 transition-colors hover:bg-blue-100 dark:bg-blue-500/15 dark:text-blue-300 dark:hover:bg-blue-500/25"
                >
                  <Play className="h-2.5 w-2.5" />
                  {it.time}
                </a>
              ) : it.time ? (
                <span className="text-muted-foreground ml-auto shrink-0 text-[11px] tabular-nums">
                  {it.time}
                </span>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}

export default TocPanel
