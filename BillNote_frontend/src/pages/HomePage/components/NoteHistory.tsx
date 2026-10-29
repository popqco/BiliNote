import { useTaskStore, ACTIVE_STATUSES, type Task } from '@/store/taskStore'
import { useCategoryStore } from '@/store/categoryStore'
import { useStopArmed } from '@/hooks/useStopArmed'
import { cn } from '@/lib/utils.ts'
import { Settings2, Square, Tag, Trash } from 'lucide-react'
import { Button } from '@/components/ui/button.tsx'
import { Checkbox } from '@/components/ui/checkbox.tsx'
import { NoteCategoryMenu } from '@/components/NoteCategoryMenu.tsx'
import { CategoryManageDialog } from '@/components/CategoryManageDialog.tsx'
import Fuse from 'fuse.js'

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'
import LazyImage from "@/components/LazyImage.tsx";
import { buildImageProxyUrl } from "@/utils/workerConnection.ts";
import {FC, ReactNode, useState, useEffect, useMemo} from 'react'

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
 * 旧实现只在任务进行中才渲染这个图标，它会在按钮组里凭空出现（右侧
 * 按钮组向左生长），光标恰在那一带的话一点就误停；且生成刚启动的
 * 2 秒内不显示/不可用（防误触窗口，见 useStopArmed）。常驻占位还能
 * 让按钮组宽度恒定、删除按钮的视觉位置纹丝不动。
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

/** 分类筛选 chip（横向滚动一行，数量多了不挤爆窄面板） */
const FilterChip: FC<{
  active: boolean
  color?: string
  onClick: () => void
  children: ReactNode
}> = ({ active, color, onClick, children }) => (
  <button
    type="button"
    onClick={onClick}
    className={cn(
      'flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] whitespace-nowrap transition-colors',
      active
        ? 'border-primary bg-primary/10 text-primary'
        : 'border-border text-muted-foreground hover:text-foreground',
    )}
  >
    {color && (
      <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: color }} />
    )}
    {children}
  </button>
)

const NoteHistory: FC<NoteHistoryProps> = ({ onSelect, selectedId }) => {
  const tasks = useTaskStore(state => state.tasks)
  const removeTask = useTaskStore(state => state.removeTask)
  const categories = useCategoryStore(state => state.categories)
  const loadCategories = useCategoryStore(state => state.load)

  const [rawSearch, setRawSearch] = useState('')
  const [search, setSearch] = useState('')
  // 分类筛选：all=全部 / none=未分类 / <category id>
  const [catFilter, setCatFilter] = useState<'all' | 'none' | string>('all')
  // 批量归类模式：200+ 存量笔记靠它一次归类，而不是点几百次
  const [bulk, setBulk] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [manageOpen, setManageOpen] = useState(false)

  useEffect(() => {
    void loadCategories()
  }, [loadCategories])

  /** 任务的有效分类 id（分类被删后的孤儿 id 按未分类算） */
  const catIdOf = (task: Task): string | null => {
    const id = task.category
    return id && categories.some(c => c.id === id) ? id : null
  }

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

  // 清空搜索框要立刻还原列表（否则防抖分支的 return 把旧关键词永远留在那儿）
  useEffect(() => {
    if (rawSearch === '') setSearch('')
  }, [rawSearch])

  // 当前筛选的分类被删掉后回到「全部」，否则列表会卡在永远为空的状态
  useEffect(() => {
    if (catFilter !== 'all' && catFilter !== 'none' && !categories.some(c => c.id === catFilter)) {
      setCatFilter('all')
    }
  }, [categories, catFilter])

  const searched = search.trim()
    ? fuse.search(search).map(result => result.item)
    : tasks
  const filteredTasks = catFilter === 'all'
    ? searched
    : catFilter === 'none'
      ? searched.filter(t => !catIdOf(t))
      : searched.filter(t => catIdOf(t) === catFilter)

  const counts = useMemo(() => {
    const per = new Map<string, number>()
    let none = 0
    for (const t of tasks) {
      const id = t.category && categories.some(c => c.id === t.category) ? t.category : null
      if (!id) none += 1
      else per.set(id, (per.get(id) || 0) + 1)
    }
    return { none, per }
  }, [tasks, categories])

  // 首屏只渲染 40 条：300 张卡常驻会把手机主线程占满（每张两个 TooltipProvider），
  // 且历史 pane 在笔记 Tab 下仍然挂载。搜索/筛选变化时重置页数。
  const [visibleCount, setVisibleCount] = useState(40)
  useEffect(() => {
    setVisibleCount(40)
  }, [search, catFilter])

  const visibleTasks = filteredTasks.slice(0, visibleCount)

  const toggleSelect = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const exitBulk = () => {
    setBulk(false)
    setSelected(new Set())
  }

  return (
    <>
      <div className="mb-2 flex items-center gap-2">
        <input
            type="text"
            placeholder="搜索笔记标题..."
            className="border-border bg-background focus:border-primary min-w-0 flex-1 rounded border px-3 py-1 text-sm outline-none"
            value={rawSearch}
            onChange={e => setRawSearch(e.target.value)}
        />
        <Button
          type="button"
          size="sm"
          variant={bulk ? 'default' : 'outline'}
          onClick={() => (bulk ? exitBulk() : setBulk(true))}
        >
          {bulk ? '完成' : '选择'}
        </Button>
      </div>

      <div className="mb-2 flex items-center gap-1 overflow-x-auto pb-0.5">
        <FilterChip active={catFilter === 'all'} onClick={() => setCatFilter('all')}>
          全部 {tasks.length}
        </FilterChip>
        <FilterChip active={catFilter === 'none'} onClick={() => setCatFilter('none')}>
          未分类 {counts.none}
        </FilterChip>
        {categories.map(cat => (
          <FilterChip
            key={cat.id}
            color={cat.color}
            active={catFilter === cat.id}
            onClick={() => setCatFilter(cat.id)}
          >
            {cat.name} {counts.per.get(cat.id) || 0}
          </FilterChip>
        ))}
        <button
          type="button"
          aria-label="管理分类"
          title="管理分类"
          onClick={() => setManageOpen(true)}
          className="text-muted-foreground hover:text-foreground border-border ml-auto shrink-0 rounded-full border p-1"
        >
          <Settings2 className="h-3 w-3" />
        </button>
      </div>

      {bulk && (
        <div className="border-border bg-muted/40 mb-2 flex flex-wrap items-center gap-2 rounded-md border px-2 py-1.5 text-[11px]">
          <span className="text-muted-foreground">已选 {selected.size}</span>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-[11px]"
            onClick={() => setSelected(new Set(filteredTasks.map(t => t.id)))}
          >
            全选当前筛选（{filteredTasks.length}）
          </Button>
          <NoteCategoryMenu
            taskIds={Array.from(selected)}
            onManage={() => setManageOpen(true)}
            onDone={exitBulk}
          >
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-6 px-2 text-[11px]"
              disabled={selected.size === 0}
            >
              加入分类
            </Button>
          </NoteCategoryMenu>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-[11px]"
            onClick={exitBulk}
          >
            取消
          </Button>
        </div>
      )}

      {filteredTasks.length === 0 ? (
        <div className="border-border bg-muted/40 rounded-md border py-6 text-center">
          <p className="text-muted-foreground text-sm">
            {tasks.length === 0 ? '暂无记录' : '没有符合筛选条件的笔记'}
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2 overflow-hidden">
          {visibleTasks.map(task => {
            const meta = STATUS_META[task.status] || { label: '等待中', cls: 'bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-200' }
            const statusLabel =
              task.status === 'PENDING' && task.queuePosition
                ? `${meta.label} · 第${task.queuePosition}位`
                : meta.label
            const catId = catIdOf(task)
            const cat = catId ? categories.find(c => c.id === catId) : undefined
            const isSelected = selected.has(task.id)
            return (
            <div
              key={task.id}
              onClick={() => {
                if (bulk) {
                  toggleSelect(task.id)
                  return
                }
                onSelect(task.id)
              }}
              className={cn(
                'border-border flex cursor-pointer flex-col rounded-md border p-3',
                selectedId === task.id && 'border-primary bg-primary-light dark:bg-primary/15',
                isSelected && 'border-primary ring-primary/40 ring-1',
              )}
            >
              <div
                className={cn('flex items-center gap-4')}
              >
                {bulk && (
                  <Checkbox
                    checked={isSelected}
                    className="shrink-0"
                    onClick={e => e.stopPropagation()}
                    onCheckedChange={() => toggleSelect(task.id)}
                  />
                )}
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
                              ? buildImageProxyUrl(task.audioMeta.cover_url, 320)
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
                  {cat && (
                    <NoteCategoryMenu
                      taskIds={[task.id]}
                      onManage={() => setManageOpen(true)}
                    >
                      <button
                        type="button"
                        onClick={e => e.stopPropagation()}
                        className="max-w-[110px] truncate rounded p-0.5 px-1 whitespace-nowrap"
                        style={{ background: `${cat.color}24`, color: cat.color }}
                        title={`分类：${cat.name}`}
                      >
                        {cat.name}
                      </button>
                    </NoteCategoryMenu>
                  )}
                </div>

                <div className="flex items-center gap-1">
                  <NoteCategoryMenu
                    taskIds={[task.id]}
                    onManage={() => setManageOpen(true)}
                  >
                    <Button
                      type="button"
                      size="small"
                      variant="ghost"
                      aria-label="归类"
                      onClick={e => e.stopPropagation()}
                      className="shrink-0"
                    >
                      <Tag className="text-muted-foreground h-4 w-4" />
                    </Button>
                  </NoteCategoryMenu>
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
          {filteredTasks.length > visibleCount && (
            <Button
              type="button"
              variant="outline"
              className="w-full"
              onClick={() => setVisibleCount(c => c + 40)}
            >
              加载更多（还有 {filteredTasks.length - visibleCount} 条）
            </Button>
          )}
        </div>
      )}

      <CategoryManageDialog open={manageOpen} onOpenChange={setManageOpen} />
    </>
  )
}

export default NoteHistory
