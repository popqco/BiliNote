import { useTaskStore } from '@/store/taskStore'
import { ScrollArea } from '@/components/ui/scroll-area.tsx'
import { Badge } from '@/components/ui/badge.tsx'
import { cn } from '@/lib/utils.ts'
import { Trash } from 'lucide-react'
import { Button } from '@/components/ui/button.tsx'
import PinyinMatch from 'pinyin-match'
import Fuse from 'fuse.js'

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'
import LazyImage from "@/components/LazyImage.tsx";
import {FC, useState, useEffect, useMemo} from 'react'

interface NoteHistoryProps {
  onSelect: (taskId: string) => void
  selectedId: string | null
}

/** 状态徽章：区分排队/解析/下载/转写/总结/完成/失败（而不是笼统的「等待中」） */
const STATUS_META: Record<string, { label: string; cls: string }> = {
  SUCCESS: { label: '已完成', cls: 'bg-primary' },
  FAILED: { label: '失败', cls: 'bg-red-500' },
  FAILD: { label: '失败', cls: 'bg-red-500' },
  PENDING: { label: '排队中', cls: 'bg-amber-500' },
  PARSING: { label: '解析中', cls: 'bg-sky-500' },
  DOWNLOADING: { label: '下载中', cls: 'bg-sky-500' },
  TRANSCRIBING: { label: '转写中', cls: 'bg-sky-500' },
  SUMMARIZING: { label: '总结中', cls: 'bg-violet-500' },
  SAVING: { label: '保存中', cls: 'bg-sky-500' },
  RUNNING: { label: '生成中', cls: 'bg-sky-500' },
}

const NoteHistory: FC<NoteHistoryProps> = ({ onSelect, selectedId }) => {
  const tasks = useTaskStore(state => state.tasks)
  const removeTask = useTaskStore(state => state.removeTask)
  // 确保baseURL没有尾部斜杠
  const baseURL = (String(import.meta.env.VITE_API_BASE_URL || 'api')).replace(/\/$/, '')
  const [rawSearch, setRawSearch] = useState('')
  const [search, setSearch] = useState('')
  const fuse = useMemo(() => new Fuse(tasks, {
    keys: ['audioMeta.title'],
    threshold: 0.4 // 匹配精度（越低越严格）
  }), [tasks])
  useEffect(() => {
    const timer = setTimeout(() => {
      if (rawSearch === '') return
      setSearch(rawSearch)
    }, 300) // 300ms 防抖

    return () => clearTimeout(timer)
  }, [rawSearch])
  const filteredTasks = search.trim()
      ? fuse.search(search).map(result => result.item)
      : tasks
  if (filteredTasks.length === 0) {
    return (
        <>
          <div className="mb-2">
            <input
                type="text"
                placeholder="搜索笔记标题..."
                className="border-border bg-background focus:border-primary w-full rounded border px-3 py-1 text-sm outline-none"
                value={search}
                onChange={e => setSearch(e.target.value)}
            />
          </div>
          <div className="border-border bg-muted/40 rounded-md border py-6 text-center">
            <p className="text-muted-foreground text-sm">暂无记录</p>
          </div>
        </>

    )
  }


  return (
    <>
      <div className="mb-2">
        <input
            type="text"
            placeholder="搜索笔记标题..."
            className="border-border bg-background focus:border-primary w-full rounded border px-3 py-1 text-sm outline-none"
            value={search}
            onChange={e => setSearch(e.target.value)}
        />
      </div>
      <div className="flex flex-col gap-2 overflow-hidden">
        {filteredTasks.map(task => {
          const meta = STATUS_META[task.status] || { label: '等待中', cls: 'bg-neutral-400' }
          const statusLabel =
            task.status === 'PENDING' && task.queuePosition
              ? `${meta.label} · 第${task.queuePosition}位`
              : meta.label
          return (
          <div
            key={task.id}
            onClick={() => onSelect(task.id)}
            className={cn(
              'border-border flex cursor-pointer flex-col rounded-md border p-3',
              selectedId === task.id && 'border-primary bg-primary-light dark:bg-primary/15'
            )}
          >
            <div
              className={cn('flex items-center gap-4')}
            >
              {/* 封面图 */}
              {task.platform === 'local' ? (
                <img
                  src={
                    task.audioMeta.cover_url ? `${task.audioMeta.cover_url}` : '/placeholder.png'
                  }
                  alt="封面"
                  className="h-10 w-12 rounded-md object-cover"
                />
              ) : (
                  <LazyImage

                      src={
                        task.audioMeta.cover_url
                            ? `${baseURL}/image_proxy?url=${encodeURIComponent(task.audioMeta.cover_url)}`
                            : '/placeholder.png'
                      }
                      alt="封面"
                  />
              )}

              {/* 标题 + 状态 */}

              <div className="flex w-full items-center justify-between gap-2">
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <div className="line-clamp-2 max-w-[180px] flex-1 overflow-hidden text-sm text-ellipsis">
                        {task.audioMeta.title || '未命名笔记'}
                      </div>
                    </TooltipTrigger>
                    <TooltipContent>
                      <p>{task.audioMeta.title || '未命名笔记'}</p>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </div>
            </div>
            <div className={'mt-2 flex items-center justify-between text-[10px]'}>
              <div className="flex shrink-0 items-center gap-1">
                <div
                  className={cn(
                    'min-w-10 rounded p-0.5 px-1.5 text-center whitespace-nowrap text-white',
                    meta.cls,
                  )}
                >
                  {statusLabel}
                </div>
                {task.origin === 'auto' && (
                  <div className="rounded border border-orange-300 bg-orange-50 p-0.5 px-1 whitespace-nowrap text-orange-500 dark:border-orange-500/40 dark:bg-orange-500/15 dark:text-orange-300">
                    自动
                  </div>
                )}
              </div>

              <div>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        type="button"
                        size="small"
                        variant="ghost"
                        onClick={e => {
                          e.stopPropagation()
                          removeTask(task.id)
                        }}
                        className="shrink-0"
                      >
                        <Trash className="text-muted-foreground h-4 w-4" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      <p>删除</p>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </div>
            </div>
            {(task.status === 'FAILED' || task.status === 'FAILD') && task.message && (
              <div
                className="text-muted-foreground mt-1 line-clamp-2 w-full text-[10px]"
                title={task.message}
              >
                {task.message}
              </div>
            )}
          </div>
          )
        })}
      </div>
    </>
  )
}

export default NoteHistory
