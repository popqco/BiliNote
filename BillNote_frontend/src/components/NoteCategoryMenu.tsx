import { ReactNode, useMemo } from 'react'
import { Check, Settings2, Tag } from 'lucide-react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu.tsx'
import { useCategoryStore } from '@/store/categoryStore'
import { useTaskStore } from '@/store/taskStore'
import { cn } from '@/lib/utils.ts'

interface NoteCategoryMenuProps {
  /** 目标任务的 id（批量模式下是全部选中项） */
  taskIds: string[]
  /** 触发控件（asChild，会接管点击） */
  children: ReactNode
  /** 「管理分类…」入口（历史面板里传打开管理对话框的回调） */
  onManage?: () => void
  /** 归类动作完成后回调（批量模式用它退出选择态） */
  onDone?: () => void
  align?: 'start' | 'end'
}

/**
 * 归类下拉菜单（单选分类）。
 *
 * 触发控件点击不会冒泡到卡片（卡片点击是「打开笔记」），所以调用方无需再包
 * stopPropagation；菜单本身走 Portal，也不会触发卡片的 onClick。
 */
export const NoteCategoryMenu = ({
  taskIds,
  children,
  onManage,
  onDone,
  align = 'end',
}: NoteCategoryMenuProps) => {
  const categories = useCategoryStore(state => state.categories)
  const load = useCategoryStore(state => state.load)
  const assign = useCategoryStore(state => state.assign)
  const tasks = useTaskStore(state => state.tasks)

  const ids = useMemo(() => Array.from(new Set((taskIds || []).filter(Boolean))), [taskIds])

  const doAssign = async (categoryId: string | null) => {
    const ok = await assign(ids, categoryId)
    if (ok) onDone?.()
  }

  // 选中态：全部分类一致才高亮，混合选择不高亮任何一项
  const current = useMemo(() => {
    if (ids.length === 0) return { kind: 'none' as const }
    const set = new Set(ids.map(id => tasks.find(t => t.id === id)?.category || undefined))
    if (set.size > 1) return { kind: 'mixed' as const }
    const cat = Array.from(set)[0]
    return cat ? ({ kind: 'cat' as const, id: cat }) : ({ kind: 'none' as const })
  }, [ids, tasks])

  return (
    <DropdownMenu onOpenChange={open => open && void load()}>
      <DropdownMenuTrigger asChild>{children}</DropdownMenuTrigger>
      <DropdownMenuContent align={align} className="w-48">
        <DropdownMenuLabel className="text-muted-foreground text-[11px]">归入分类</DropdownMenuLabel>
        <DropdownMenuItem onSelect={() => void doAssign(null)}>
          <span className="flex-1">未分类</span>
          {current.kind === 'none' && <Check className="h-3.5 w-3.5" />}
        </DropdownMenuItem>
        {categories.map(cat => (
          <DropdownMenuItem key={cat.id} onSelect={() => void doAssign(cat.id)}>
            <span
              className="mr-2 inline-block h-2 w-2 shrink-0 rounded-full"
              style={{ background: cat.color }}
            />
            <span className="flex-1 truncate">{cat.name}</span>
            {current.kind === 'cat' && current.id === cat.id && (
              <Check className="h-3.5 w-3.5" />
            )}
          </DropdownMenuItem>
        ))}
        {categories.length === 0 && (
          <div className="text-muted-foreground px-2 py-1.5 text-[11px]">还没有分类</div>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem disabled={!onManage} onSelect={() => onManage?.()}>
          <Settings2 className="mr-2 h-3.5 w-3.5" /> 管理分类…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * 阅读页顶部的分类徽标（未分类时是虚线「分类」按钮，可点可归类）。
 */
export const NoteCategoryBadge = ({ taskId }: { taskId: string }) => {
  const task = useTaskStore(state => state.tasks.find(t => t.id === taskId))
  const categories = useCategoryStore(state => state.categories)
  const cat = task?.category ? categories.find(c => c.id === task.category) : undefined

  return (
    <NoteCategoryMenu taskIds={[taskId]}>
      <button
        type="button"
        className={cn(
          'inline-flex max-w-[45vw] items-center gap-1 truncate rounded-md border px-1.5 py-0.5 text-[11px] transition-colors sm:max-w-none',
          cat
            ? 'border-transparent'
            : 'border-border text-muted-foreground hover:text-foreground border-dashed',
        )}
        style={cat ? { background: `${cat.color}1f`, color: cat.color } : undefined}
      >
        <Tag className="h-3 w-3 shrink-0" />
        <span className="truncate">{cat ? cat.name : '分类'}</span>
      </button>
    </NoteCategoryMenu>
  )
}
