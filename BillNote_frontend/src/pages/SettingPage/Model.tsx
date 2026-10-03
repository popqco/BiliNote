import Provider from '@/components/Form/modelForm/Provider.tsx'
import { Outlet, useParams } from 'react-router-dom'
import { useIsMobile } from '@/hooks/useIsMobile.ts'
import { useRoutePath } from '@/hooks/useRoutePath.ts'
import { cn } from '@/lib/utils.ts'

const Model = () => {
  // 手机端供应商页同样两级：列表 ↔ 表单。是否选中用子路由 param 判断
  // （/settings/model=列表级，/settings/model/:id 或 /new=表单级）。
  const isMobile = useIsMobile()
  const params = useParams()
  // 路由路径两端统一（Tauri HashRouter 下 pathname 恒为 '/'，之前靠
  // window.location.pathname.endsWith('/new') 判断，桌面端 /new 表单永远进不去）。
  const routePath = useRoutePath()
  const onFormLevel =
    Boolean((params as any)?.id) || /\/settings\/model\/.+/.test(routePath)
  if (isMobile) {
    return (
      <div className="bg-background min-h-0">
        <div className={cn('min-h-0 p-2', onFormLevel && 'hidden')}>
          <Provider></Provider>
        </div>
        <div className={cn('min-h-0', !onFormLevel && 'hidden')}>
          <Outlet />
        </div>
      </div>
    )
  }
  return (
    <div className={'flex h-full min-h-0 bg-background'}>
      <div className={'flex-1/5 min-h-0 overflow-y-auto border-r border-border p-2'}>
        <Provider></Provider>
      </div>
      <div className={'flex-4/5 min-h-0 overflow-y-auto'}>
        <Outlet />
      </div>
    </div>
  )
}
export default Model
