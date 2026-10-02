import toast from 'react-hot-toast'
import { Switch } from '@/components/ui/switch.tsx'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.tsx'
import { useSystemStore } from '@/store/configStore'

/**
 * 「通用」设置：放不属于模型/转写/下载/自动化/外观的全局开关。
 * 第一项：剪贴板识别轮询 —— 默认关（只在窗口回到前台时检查一次，
 * 零后台开销）；打开后每 N 秒经 Rust 侧读一次系统剪贴板（无需焦点），
 * 复制链接后切回窗口即弹窗。开关与间隔经 zustand persist 落盘
 *（localStorage: system-store），重启应用仍保留。
 * 第二项：应用外系统通知 —— 轮询在后台命中时经 OS 通知中心弹一条，
 * 应用不在前台也能看到；点击通知回到应用，应用内卡片（含封面+按钮）再弹出。
 */
const General = () => {
  const pollEnabled = useSystemStore(s => s.clipboardPollEnabled)
  const setPollEnabled = useSystemStore(s => s.setClipboardPollEnabled)
  const pollIntervalSec = useSystemStore(s => s.clipboardPollIntervalSec)
  const setPollIntervalSec = useSystemStore(s => s.setClipboardPollIntervalSec)
  const osNotifyEnabled = useSystemStore(s => s.clipboardOsNotifyEnabled)
  const setOsNotifyEnabled = useSystemStore(s => s.setClipboardOsNotifyEnabled)

  const handleIntervalChange = (v: string) => {
    const n = Number(v)
    if (!Number.isFinite(n) || n < 1 || n > 60) {
      toast.error('轮询间隔需在 1–60 秒之间')
      return
    }
    setPollIntervalSec(n)
  }

  return (
    <div className="bg-card h-full overflow-auto p-8">
      <div className="text-2xl font-medium">通用</div>
      <div className="text-muted-foreground mt-1 text-sm">
        全局行为开关，修改即时生效、自动保存。
      </div>

      <div className="border-border mt-6 max-w-3xl rounded-lg border p-4">
        <div className="flex items-center justify-between gap-4">
          <div className="text-sm">
            剪贴板轮询识别
            <div className="text-muted-foreground text-xs">
              关闭时只在窗口回到前台时检查一次剪贴板（零后台开销、零打扰）。
              打开后每隔所选间隔在后台读一次剪贴板，复制视频链接后切回窗口即弹窗提示。
            </div>
          </div>
          <Switch checked={pollEnabled} onCheckedChange={setPollEnabled} />
        </div>
        <div className="mt-3 flex items-center justify-between gap-4">
          <div className="text-sm">
            轮询间隔
            <div className="text-muted-foreground text-xs">
              单次读取只是一次本地 IPC，开销可忽略；3 秒是延迟与打扰的平衡点。
              读取失败会自动退避（最长 60 秒），成功后复位。
            </div>
          </div>
          <Select
            value={String(pollIntervalSec)}
            onValueChange={handleIntervalChange}
            disabled={!pollEnabled}
          >
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="2">每 2 秒</SelectItem>
              <SelectItem value="3">每 3 秒</SelectItem>
              <SelectItem value="5">每 5 秒</SelectItem>
              <SelectItem value="10">每 10 秒</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="mt-3 flex items-center justify-between gap-4">
          <div className="text-sm">
            应用外系统通知
            <div className="text-muted-foreground text-xs">
              轮询在后台命中视频链接时，经系统通知中心弹一条提醒——
              不用切回 BiliNote 也能看到。点击通知回到应用，完整卡片再弹出。
              关掉后后台命中只攒着、等回到前台才提示。
            </div>
          </div>
          <Switch
            checked={osNotifyEnabled}
            onCheckedChange={async v => {
              setOsNotifyEnabled(v)
              if (v) {
                // 桌面端走 Rust 原生通知，无需授权恒 granted；
                // 若 notify 命令不通（如旧构建），回滚并提示。
                const { ensureOsNotifyPermission } = await import('@/utils/osClipboardNotify.ts')
                const ok = await ensureOsNotifyPermission()
                if (!ok) {
                  toast.error('系统通知通道不可用，请重启应用后重试')
                  setOsNotifyEnabled(false)
                }
              }
            }}
          />
        </div>
      </div>
    </div>
  )
}

export default General
