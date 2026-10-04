import { useTheme } from 'next-themes'
import { Monitor, Moon, Sun } from 'lucide-react'
import { cn } from '@/lib/utils.ts'

const OPTIONS = [
  {
    key: 'light',
    label: '亮色',
    desc: '始终使用亮色主题',
    icon: <Sun className="h-5 w-5" />,
  },
  {
    key: 'dark',
    label: '暗色',
    desc: '始终使用暗色主题，夜间使用更护眼',
    icon: <Moon className="h-5 w-5" />,
  },
  {
    key: 'system',
    label: '跟随系统',
    desc: '跟随 Windows 的深色模式设置自动切换',
    icon: <Monitor className="h-5 w-5" />,
  },
]

/**
 * 外观设置：亮色 / 暗色 / 跟随系统 三态。
 * 偏好由 next-themes 持久化（localStorage: bilinote-theme），
 * 首页顶栏也有一个快捷切换按钮。
 */
const Appearance = () => {
  const { theme, setTheme } = useTheme()

  return (
    <div className="bg-card h-full overflow-auto p-4 sm:p-8">
      <div className="text-2xl font-medium">外观</div>
      <div className="text-muted-foreground mt-1 text-sm">
        选择主题外观。夜间使用暗色模式，屏幕不再刺眼。
      </div>

      {/* 手机上三列会被压成竖条，改单列上下排 */}
      <div className="mt-6 grid max-w-3xl grid-cols-1 gap-4 min-[420px]:grid-cols-3">
        {OPTIONS.map(o => (
          <button
            key={o.key}
            type="button"
            onClick={() => setTheme(o.key)}
            className={cn(
              'border-border hover:bg-accent flex flex-col items-start gap-2 rounded-lg border p-4 text-left transition-colors',
              theme === o.key && 'border-primary bg-primary-light ring-primary/40 ring-1',
            )}
          >
            <div className="flex items-center gap-2 font-medium">
              {o.icon}
              {o.label}
            </div>
            <div className="text-muted-foreground text-xs">{o.desc}</div>
          </button>
        ))}
      </div>

      <div className="text-muted-foreground mt-6 text-xs">
        提示：首页左上角也有一键切换按钮（太阳 / 月亮图标）。
      </div>
    </div>
  )
}

export default Appearance
