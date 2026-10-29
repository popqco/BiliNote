import { useEffect, useState } from 'react'
import { Pencil, Trash } from 'lucide-react'

import { Button } from '@/components/ui/button.tsx'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog.tsx'
import { Input } from '@/components/ui/input.tsx'
import { useCategoryStore } from '@/store/categoryStore'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * 分类管理（新建 / 重命名 / 删除）。
 *
 * 删除只断开关联：笔记本身不动，卡片变回「未分类」；确认文案写清楚，
 * 避免用户以为是删笔记。
 */
export const CategoryManageDialog = ({ open, onOpenChange }: Props) => {
  const categories = useCategoryStore(state => state.categories)
  const load = useCategoryStore(state => state.load)
  const create = useCategoryStore(state => state.create)
  const rename = useCategoryStore(state => state.rename)
  const remove = useCategoryStore(state => state.remove)

  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingName, setEditingName] = useState('')

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  const onCreate = async () => {
    if (busy || !name.trim()) return
    setBusy(true)
    const cat = await create(name)
    setBusy(false)
    if (cat) setName('')
  }

  const commitRename = async () => {
    if (!editingId || !editingName.trim()) {
      setEditingId(null)
      return
    }
    const ok = await rename(editingId, editingName)
    if (ok) setEditingId(null)
  }

  const onDelete = async (id: string, catName: string) => {
    const ok = window.confirm(
      `删除分类「${catName}」？\n\n笔记不会被删除，只会变回「未分类」。`,
    )
    if (!ok) return
    await remove(id)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>笔记分类</DialogTitle>
          <DialogDescription>
            一篇笔记归入一个分类。删除分类不会删除笔记，只会变回「未分类」。
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center gap-2">
          <Input
            value={name}
            maxLength={30}
            placeholder="新分类名称，例如：学习"
            onChange={e => setName(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') void onCreate()
            }}
          />
          <Button onClick={() => void onCreate()} disabled={busy || !name.trim()}>
            新建
          </Button>
        </div>

        <div className="border-border max-h-72 overflow-auto rounded-md border">
          {categories.length === 0 ? (
            <div className="text-muted-foreground px-3 py-6 text-center text-sm">
              还没有分类，先在上面新建一个
            </div>
          ) : (
            <div className="flex flex-col divide-y">
              {categories.map(cat => (
                <div key={cat.id} className="flex items-center gap-2 px-2 py-1.5">
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-full"
                    style={{ background: cat.color }}
                  />
                  {editingId === cat.id ? (
                    <>
                      <Input
                        autoFocus
                        value={editingName}
                        maxLength={30}
                        className="h-7 flex-1"
                        onChange={e => setEditingName(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') void commitRename()
                          if (e.key === 'Escape') setEditingId(null)
                        }}
                      />
                      <Button size="sm" onClick={() => void commitRename()}>
                        保存
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}>
                        取消
                      </Button>
                    </>
                  ) : (
                    <>
                      <span className="flex-1 truncate text-sm">{cat.name}</span>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label="重命名"
                        onClick={() => {
                          setEditingId(cat.id)
                          setEditingName(cat.name)
                        }}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label="删除分类"
                        onClick={() => void onDelete(cat.id, cat.name)}
                      >
                        <Trash className="text-muted-foreground h-3.5 w-3.5" />
                      </Button>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            完成
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
