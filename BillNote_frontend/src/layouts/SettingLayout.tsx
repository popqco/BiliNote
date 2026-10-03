import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'
import { Link, Outlet } from 'react-router-dom'
import { ChevronLeft, SlidersHorizontal } from 'lucide-react'
import React from 'react'
import { useIsMobile } from '@/hooks/useIsMobile.ts'
import { useRoutePath } from '@/hooks/useRoutePath.ts'
import { cn } from '@/lib/utils.ts'
import logo from '@/assets/icon.svg'

interface ISettingLayoutProps {
  Menu: React.ReactNode
}
const SettingLayout = ({ Menu }: ISettingLayoutProps) => {
  // 手机端设置页改为「菜单 → 详情」两级：/settings 本体=菜单级，其余=详情级（左上角返回）。
  // 桌面端保持左右分栏不受影响。
  const isMobile = useIsMobile()
  // 路由路径两端统一（Tauri HashRouter 下 pathname 恒为 '/'，见 hook 注释）。
  const path = useRoutePath()
  const onMenuLevel = path === '/settings'
  // 返回目标：三级页面退回所属列表，二级详情退回设置菜单。
  // 之前除模型表单外全部退回 /settings，而 /settings index 又 <Navigate> 到
  // connection，导致「详情 → 设置 → 秒跳 connection」，菜单总览去哪都到不了。
  // 现在 index 不再跳转，下面的映射才有意义。
  const modelFormMatch = /^\/settings\/model\/.+/.test(path)
  const downloaderFormMatch = /^\/settings\/download\/.+/.test(path)
  const detailBackTo = modelFormMatch
    ? '/settings/model'
    : downloaderFormMatch
      ? '/settings/download'
      : '/settings'
  if (isMobile) {
    return (
      <div className="bg-background flex h-dvh flex-col overflow-hidden">
        <header className="border-border bg-card flex h-14 shrink-0 items-center gap-2 border-b px-4">
          {!onMenuLevel ? (
            <Link to={detailBackTo} className="text-primary flex items-center gap-1 text-sm font-medium">
              <ChevronLeft className="h-5 w-5" />
              {modelFormMatch ? '模型列表' : downloaderFormMatch ? '下载配置' : '设置'}
            </Link>
          ) : (
            <span className="text-base font-semibold">设置</span>
          )}
          <div className="flex-1" />
          <Link to="/" className="text-muted-foreground text-sm">
            首页
          </Link>
        </header>
        <div className={cn('min-h-0 flex-1 overflow-y-auto p-4', !onMenuLevel && 'hidden')}>
          {Menu}
        </div>
        <div className={cn('min-h-0 flex-1 overflow-y-auto', onMenuLevel && 'hidden')}>
          <Outlet />
        </div>
      </div>
    )
  }
  return (
    <div
      className="h-full w-full"
      style={{
        backgroundColor: 'var(--color-muted)',
      }}
    >
      <div className="flex flex-1">
        {/* 左侧部分：Header + 表单 */}
        <aside className="border-border bg-card flex w-[300px] flex-col border-r">
          {/* Header */}
          <header className="flex h-16 items-center justify-between px-6">
            <div className="flex items-center gap-2">
              <div className="flex h-10 w-10 items-center justify-center overflow-hidden rounded-2xl">
                <img src={logo} alt="logo" className="h-full w-full object-contain" />
              </div>
              <div className="text-foreground text-2xl font-bold">BiliNote</div>
            </div>
            <div>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger>
                    <Link to={'/'}>
                      <SlidersHorizontal className="text-muted-foreground hover:text-primary cursor-pointer" />
                    </Link>
                  </TooltipTrigger>
                  <TooltipContent>
                    <span>返回首页</span>
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </div>
          </header>

          {/* 表单内容 */}
          <div className="flex-1 overflow-auto p-4">
            {/*<NoteForm />*/}
            {Menu}
          </div>
        </aside>

        {/* 右侧预览区域 */}
        <main className="h-screen flex-1 overflow-hidden">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
export default SettingLayout
