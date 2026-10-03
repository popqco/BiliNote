import { Outlet, useParams } from 'react-router-dom'
import { useIsMobile } from '@/hooks/useIsMobile.ts'
import { cn } from '@/lib/utils.ts'
import Options from '@/components/Form/DownloaderForm/Options.tsx'
import ProxyConfig from '@/components/Form/DownloaderForm/ProxyConfig.tsx'
const Downloader = () => {
  // 手机端同样两级：/settings/download=配置列表级，/settings/download/:id=表单级。
  // 之前这里桌面/手机都左右分栏，手机上被挤成一条缝且返回键直达菜单回不来。
  const isMobile = useIsMobile()
  const params = useParams()
  const onFormLevel = Boolean((params as any)?.id)
  if (isMobile) {
    return (
      <div className="bg-background min-h-0">
        <div className={cn('min-h-0 p-2', onFormLevel && 'hidden')}>
          <div className="flex flex-col gap-3">
            <ProxyConfig />
            <Options />
          </div>
        </div>
        <div className={cn('min-h-0', !onFormLevel && 'hidden')}>
          <Outlet />
        </div>
      </div>
    )
  }
  return (
    <div className={'flex h-full bg-background'}>
      <div className={'flex flex-1/5 flex-col gap-3 overflow-y-auto border-border border-r p-2'}>
        <ProxyConfig />
        <Options></Options>
      </div>
      <div className={'flex-4/5'}>
        <Outlet />
      </div>
    </div>
  )
}
export default Downloader
