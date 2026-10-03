import { FC, ReactNode, useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { FilePlus2, History, BookOpenText, Settings } from 'lucide-react'
import { cn } from '@/lib/utils.ts'
import { useTaskStore } from '@/store/taskStore'
import { loadWorkerConnection } from '@/utils/workerConnection.ts'

export type MobileTab = 'new' | 'history' | 'note' | 'settings'

/** localStorage 持久化当前 Tab：手机上切到设置页再回来仍停在原 Tab。 */
const MOBILE_TAB_KEY = 'bilinote-mobile-tab'
function readTab(fallback: MobileTab): MobileTab {
  try {
    const v = localStorage.getItem(MOBILE_TAB_KEY)
    if (v === 'new' || v === 'history' || v === 'note') return v
  } catch { /* 忽略 */ }
  return fallback
}

interface IProps {
  NewForm: ReactNode
  HistoryList: ReactNode
  NoteView: ReactNode
  defaultTab?: MobileTab
}

const TABS: Array<{ id: MobileTab; label: string; icon: ReactNode }> = [
  { id: 'new', label: '新建', icon: <FilePlus2 className="h-5 w-5" /> },
  { id: 'history', label: '历史', icon: <History className="h-5 w-5" /> },
  { id: 'note', label: '笔记', icon: <BookOpenText className="h-5 w-5" /> },
  { id: 'settings', label: '设置', icon: <Settings className="h-5 w-5" /> },
]

/**
 * 手机端单列 Tab 布局：新建 / 历史 / 笔记 / 设置四页一次只放一页，
 * 替代桌面三栏可拖拽布局（手机上三栏挤成一条缝，见用户实拍）。
 * 桌面端不受影响（Home.tsx 里按 useIsMobile 二选一）。
 */
const MobileLayout: FC<IProps> = ({ NewForm, HistoryList, NoteView, defaultTab = 'new' }) => {
  const [active, setActive] = useState<MobileTab>(() => readTab(defaultTab))
  // 配对状态要响应变化（配对页保存后回首页横幅应消失）：用 state 存，
  // 配对/断开事件里刷新，而非每次渲染重读 localStorage。
  const [hasUnpaired, setHasUnpaired] = useState(() => !loadWorkerConnection())
  const pendingCount = useTaskStore(s => s.tasks.filter(t => !['SUCCESS', 'FAILED', 'FAILD'].includes(t.status)).length)

  const switchTab = useCallback((t: MobileTab) => {
    setActive(t)
    try {
      localStorage.setItem(MOBILE_TAB_KEY, t)
    } catch { /* 忽略 */ }
  }, [])

  // 历史里点任务 → 自动切到笔记页；提交新任务 → 自动切到笔记页看进度
  const currentTaskId = useTaskStore(s => s.currentTaskId)
  useEffect(() => {
    if (currentTaskId) switchTab('note')
  }, [currentTaskId, switchTab])

  // 配对成功/断开（连接页广播事件）→ 刷新横幅
  useEffect(() => {
    const refresh = () => setHasUnpaired(!loadWorkerConnection())
    window.addEventListener('bilinote:worker-paired', refresh)
    window.addEventListener('bilinote:worker-unpaired', refresh)
    return () => {
      window.removeEventListener('bilinote:worker-paired', refresh)
      window.removeEventListener('bilinote:worker-unpaired', refresh)
    }
  }, [])

  return (
    <div className="bg-background flex h-dvh flex-col overflow-hidden">
      {/* 未配对引导横幅：点直达配对页。替代原来满屏刷的 401 toast。 */}
      {hasUnpaired && (
        <Link
          to="/settings/connection"
          className="bg-amber-500/15 text-amber-700 dark:text-amber-300 shrink-0 px-4 py-2 text-center text-sm font-medium"
        >
          未连接 Worker，点此去配对 →
        </Link>
      )}

      {/* 三个 Tab 常驻保活、只显隐：切 Tab 不卸载表单，填一半的链接/选项不丢。
          之前条件渲染每次切走就 unmount，NoteForm 的 useEffect(no currentTask→清空)
          把刚填的链接清掉——即"每次切回来就清空"。 */}
      <main className="min-h-0 flex-1 overflow-y-auto">
        <div className={active === 'new' ? 'p-4 pb-8' : 'hidden'}>{NewForm}</div>
        <div className={active === 'history' ? 'p-4 pb-8' : 'hidden'}>{HistoryList}</div>
        <div className={active === 'note' ? 'p-4 pb-8' : 'hidden'}>{NoteView}</div>
      </main>

      <nav className="border-border bg-card grid shrink-0 grid-cols-4 border-t pb-[env(safe-area-inset-bottom)]">
        {TABS.map(t => {
          const selected = active === t.id
          // 设置是独立路由页，其余三个是首页内 Tab 切换（不跳路由、不重挂载）。
          if (t.id === 'settings') {
            return (
              <Link
                key={t.id}
                to="/settings/connection"
                className="text-muted-foreground flex flex-col items-center gap-0.5 py-2 text-xs"
              >
                {t.icon}
                {t.label}
              </Link>
            )
          }
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => switchTab(t.id)}
              className={cn(
                'relative flex flex-col items-center gap-0.5 py-2 text-xs',
                selected ? 'text-primary font-medium' : 'text-muted-foreground',
              )}
            >
              {t.icon}
              {t.label}
              {/* 历史页右上角挂进行中任务数 */}
              {t.id === 'history' && pendingCount > 0 && (
                <span className="bg-primary text-primary-foreground absolute top-1 right-1/2 translate-x-5 rounded-full px-1.5 text-[10px] leading-4">
                  {pendingCount}
                </span>
              )}
            </button>
          )
        })}
      </nav>
    </div>
  )
}

export default MobileLayout
