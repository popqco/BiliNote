import { Check, ChevronDown, ListFilter, Settings2 } from 'lucide-react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu.tsx'
import { useCategoryStore } from '@/store/categoryStore'
import { cn } from '@/lib/utils.ts'

/** 分类筛选值：all=全部 / none=未分类 / 其余为分类 id */
export type CategoryFilter = 'all' | 'none' | string

interface Props {
  value: CategoryFilter
  /** 全部笔记数（含筛选前） */
  total: number
  counts: { none: number; per: Map<string, number> }
  onChange: (value: CategoryFilter) => void
  onManage: () => void
}

/**
 * 搜索框右侧的「分类 ▾」筛选入口（2026-10-30 用户选定的方案）。
 *
 * 把原来横跨一行的分类胶囊收进下拉：列表多出一行高度，分类再多也不会挤爆窄面板。
 * 菜单里带每类计数，「管理分类…」入口也放在这里（原来的齿轮按钮一并省掉）。
 */
export const NoteCategoryFilterMenu = ({ value, total, counts, onChange, onManage }: Props) => {
  const categories = useCategoryStore(state => state.categories)
  const load = useCategoryStore(state => state.load)

  const active = value !== 'all' && value !== 'none' ? categories.find(c => c.id === value) : undefined
  const noneActive = value === 'none'
  const label = value === 'all' ? '分类' : noneActive ? '未分类' : active?.name || '分类'
  const filtered = value !== 'all'

  return (
    <DropdownMenu onOpenChange={open => open && void load()}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="按分类筛选"
          className={cn(
            'border-border text-muted-foreground hover:text-foreground flex h-8 max-w-[112px] shrink-0 items-center gap-1 rounded border px-2 text-[11px] transition-colors',
            filtered && 'border-primary bg-primary/10 text-primary dark:text-primary-soft',
          )}
        >
          <ListFilter className="h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 truncate">{label}</span>
          {active && (
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ background: active.color }}
            />
          )}
          <ChevronDown className="h-3 w-3 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuLabel className="text-muted-foreground text-[11px]">只看</DropdownMenuLabel>
        <DropdownMenuItem onSelect={() => onChange('all')}>
          <span className="flex-1">全部笔记</span>
          <span className="text-muted-foreground tabular-nums text-[10px]">{total}</span>
          {value === 'all' && <Check className="ml-1.5 h-3.5 w-3.5" />}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onChange('none')}>
          <span className="flex-1">未分类</span>
          <span className="text-muted-foreground tabular-nums text-[10px]">{counts.none}</span>
          {noneActive && <Check className="ml-1.5 h-3.5 w-3.5" />}
        </DropdownMenuItem>
        {categories.map(cat => {
          const catActive = value === cat.id
          return (
            <DropdownMenuItem key={cat.id} onSelect={() => onChange(cat.id)}>
              <span
                className="mr-2 inline-block h-2 w-2 shrink-0 rounded-full"
                style={{ background: cat.color }}
              />
              <span className="flex-1 truncate">{cat.name}</span>
              <span className="text-muted-foreground tabular-nums text-[10px]">
                {counts.per.get(cat.id) || 0}
              </span>
              {catActive && <Check className="ml-1.5 h-3.5 w-3.5" />}
            </DropdownMenuItem>
          )
        })}
        {categories.length === 0 && (
          <div className="text-muted-foreground px-2 py-1.5 text-[11px]">还没有分类</div>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={onManage}>
          <Settings2 className="mr-2 h-3.5 w-3.5" /> 管理分类…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
