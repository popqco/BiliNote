import { useTaskStore, ACTIVE_STATUSES, type Task } from '@/store/taskStore'
import { useStopArmed } from '@/hooks/useStopArmed'
import { Badge } from '@/components/ui/badge.tsx'
import { cn } from '@/lib/utils.ts'
import { Square, Trash } from 'lucide-react'
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
import { buildImageProxyUrl } from "@/utils/workerConnection.ts";
import {FC, useState, useEffect, useMemo} from 'react'

interface NoteHistoryProps {
  onSelect: (taskId: string) => void
  selectedId: string | null
}

/** 状态徽章：区分排队/解析/下载/转写/总结/完成/失败（而不是笼统的「等待中」）
    2026-10-11 亮色对比度修复：白字压 500 系纯色只有 2~3:1（徽标是 10px 小字），
    亮色改「100 浅底 + 700/800 深字」（≥4.5:1）。
    2026-10-15 暗色配色改版：暗色同样改「同色相 20% 低透明底 + 200 系浅字」——
    原「500 系纯色实心 + 白字」白字只有 2~3:1，而且一片纯色小方块在暗底上很扎眼。
    文字必须用 200 系浅色：主色/深色系当文字压同色浅底只有 3.75:1 不达标。 */
const STATUS_META: Record<string, { label: string; cls: string }> = {
  SUCCESS: { label: '已完成', cls: 'bg-primary text-white dark:bg-primary/20 dark:text-primary-soft' },
  FAILED: { label: '失败', cls: 'bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-200' },
  FAILD: { label: '失败', cls: 'bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-200' },
  // 用户主动停止（/cancel_task）：中性灰——不是失败，是可随时重来的终态
  CANCELLED: { label: '已停止', cls: 'bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-200' },
  PENDING: { label: '排队中', cls: 'bg-amber-100 text-amber-800 dark:bg-amber-500/20 dark:text-amber-200' },
  PARSING: { label: '解析中', cls: 'bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-200' },
  DOWNLOADING: { label: '下载中', cls: 'bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-200' },
  TRANSCRIBING: { label: '转写中', cls: 'bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-200' },
  SUMMARIZING: { label: '总结中', cls: 'bg-violet-100 text-violet-700 dark:bg-violet-500/20 dark:text-violet-200' },
  SAVING: { label: '保存中', cls: 'bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-200' },
  RUNNING: { label: '生成中', cls: 'bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-200' },
}

/** 进行中的状态（卡片上给「停止生成」小按钮）：不是这些就没有可停的。
 *  列表与 taskStore 共用一份（startedAt 记时也用这套），防止两边漂移。 */
const isActiveStatus = (s: string) => ACTIVE_STATUSES.includes(s as any)

/**
 * 卡片右下角的「停止生成」小按钮。
 *
 * 2026-10-18 误触修复：槽位常驻（不可用时 visibility:hidden 占位）——
 * 旧实现只在任务进行中才渲染这个图标，它出现的位置恰好是删除按钮原来的
 * 位置（flex 右对齐，新图标把删除按钮往左挤），光标停在原地就会点到
 * 刚冒出来的停止按钮。常驻占位后删除按钮永远不动，且生成刚启动的
 * 2 秒内不显示/不可用（防误触窗口，见 useStopArmed）。
 */
const CardStopButton: FC<{ task: Task }> = ({ task }) => {
  const armed = useStopArmed(task.id)
  const visible = isActiveStatus(task.status) && armed
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            size="small"
            variant="ghost"
            aria-label="停止生成"
            tabIndex={visible ? 0 : -1}
            onClick={e => {
              e.stopPropagation()
              if (!visible) return
              // 生成中/排队中的卡片可直接停止（不必先选中它）：
              // 设置错了模型时第一时间止损（2026-10-16 用户需求）
              useTaskStore.getState().cancelTask(task.id)
            }}
            className={cn('shrink-0', !visible && 'invisible')}
          >
            <Square className="text-muted-foreground h-4 w-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          <p>停止生成</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

const NoteHistory: FC<NoteHistoryProps> = ({ onSelect, selectedId }) => {
  const tasks = useTaskStore(state => state.tasks)
  const removeTask = useTaskStore(state => state.removeTask)
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
          const meta = STATUS_META[task.status] || { label: '等待中', cls: 'bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-200' }
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
                            ? buildImageProxyUrl(task.audioMeta.cover_url)
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
                    'min-w-10 rounded p-0.5 px-1.5 text-center whitespace-nowrap',
                    meta.cls,
                  )}
                >
                  {statusLabel}
                </div>
                {task.origin === 'auto' && (
                  <div className="rounded border border-orange-300 bg-orange-50 p-0.5 px-1 whitespace-nowrap text-orange-700 dark:border-orange-500/40 dark:bg-orange-500/15 dark:text-orange-300">
                    自动
                  </div>
                )}
              </div>

              <div className="flex items-center gap-1">
                <CardStopButton task={task} />
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
            {/* 进行中提示（排队位次/降级重试/失败原因）：轮询会把后端 message 合并进来，
                之前只有失败卡才展示——总结阶段一蹲几分钟还零反馈，看起来像卡死（2026-10-06） */}
            {task.message && (
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
