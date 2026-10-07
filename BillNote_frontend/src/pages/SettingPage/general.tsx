import toast from 'react-hot-toast'
import { Switch } from '@/components/ui/switch.tsx'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.tsx'
import { useSystemStore, type SmoothScrollTier } from '@/store/configStore'

/**
 * 「通用」设置：放不属于模型/转写/下载/自动化/外观的全局开关。
 * 第一项：剪贴板识别轮询 —— 默认关（只在窗口回到前台时检查一次，
 * 零后台开销）；打开后每 N 秒经 Rust 侧读一次系统剪贴板（无需焦点），
 * 复制链接后切回窗口即弹窗。开关与间隔经 zustand persist 落盘
 *（localStorage: system-store），重启应用仍保留。
 * 第二项：应用外系统通知 —— 轮询在后台命中时经 OS 通知中心弹一条，
 * 应用不在前台也能看到；点击通知回到应用，应用内卡片（含封面+按钮）再弹出。
 * 第三项：阅读区平滑滚动 —— 滚轮惯性（Lenis），消除一格一顿的跳变；
 * 手机触摸端本来就是原生惯性，不受影响。手感三档即时切换、现场试选；
 * 拖动滚动键（左键 / Shift+左键）可按用户习惯互换。
 */
const General = () => {
  const pollEnabled = useSystemStore(s => s.clipboardPollEnabled)
  const setPollEnabled = useSystemStore(s => s.setClipboardPollEnabled)
  const pollIntervalSec = useSystemStore(s => s.clipboardPollIntervalSec)
  const setPollIntervalSec = useSystemStore(s => s.setClipboardPollIntervalSec)
  const osNotifyEnabled = useSystemStore(s => s.clipboardOsNotifyEnabled)
  const setOsNotifyEnabled = useSystemStore(s => s.setClipboardOsNotifyEnabled)
  const smoothEnabled = useSystemStore(s => s.smoothScrollEnabled)
  const setSmoothEnabled = useSystemStore(s => s.setSmoothScrollEnabled)
  const smoothTier = useSystemStore(s => s.smoothScrollTier)
  const setSmoothTier = useSystemStore(s => s.setSmoothScrollTier)
  const dragScroll = useSystemStore(s => s.dragScrollEnabled)
  const setDragScroll = useSystemStore(s => s.setDragScrollEnabled)

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
                // 桌面端走 Rust 原生通知，无需授权恒 granted。
                // 两步验证：request_permission 只过权限面，notify 才是真命令——
                // ACL 缺权限（如 2026-10-02 装机版 app.exe 缺 notification:default）
                // 时 request_permission 照样 granted、notify 却被拒（invoke 报
                // Command not allowed）。开启时发一条真实测试通知，命令不通当场
                // 报错回滚，不留到第一次后台命中时静默失败。
                const { ensureOsNotifyPermission, sendOsClipboardNotify } = await import(
                  '@/utils/osClipboardNotify.ts'
                )
                if (!(await ensureOsNotifyPermission())) {
                  toast.error('系统通知通道不可用，请重启应用后重试')
                  setOsNotifyEnabled(false)
                  return
                }
                const sent = await sendOsClipboardNotify('系统通知已开启，这是一条测试通知')
                if (!sent) {
                  toast.error('测试通知发送失败（notify 命令被拒），请更新应用后重试')
                  setOsNotifyEnabled(false)
                } else {
                  toast.success('测试通知已发出；若没看到横幅，请检查系统勿扰/全屏占用')
                }
              }
            }}
          />
        </div>
      </div>

      <div className="border-border mt-4 max-w-3xl rounded-lg border p-4">
        <div className="flex items-center justify-between gap-4">
          <div className="text-sm">
            阅读区平滑滚动
            <div className="text-muted-foreground text-xs">
              两种触摸式阅读手感：滚轮加连贯的缓动动画，消除一格一顿的刻度感；
              按住拖动 = 内容 1:1 跟随鼠标、松手带惯性滑行（像手机）。
              拖动用哪个键（左键还是 Shift+左键）可在下方「按住左键拖动滚动」对调。
              只影响桌面端的笔记阅读区与原文面板；
              手机触摸滑动本来就是原生惯性，不受影响。关闭后回到系统原生滚动。
            </div>
          </div>
          <Switch checked={smoothEnabled} onCheckedChange={setSmoothEnabled} />
        </div>
        <div className="mt-3 flex items-center justify-between gap-4">
          <div className="text-sm">
            手感档位
            <div className="text-muted-foreground text-xs">
              跟手：滑行短、停得快，接近原生但顺滑；适中：连贯不飘（推荐）；
              动量：接近手机松手后的滑行。滚轮与拖拽松手的惯性共用档位；
              切换后到阅读页滚几下、拖几下即可对比。
            </div>
          </div>
          <Select
            value={smoothTier}
            onValueChange={v => setSmoothTier(v as SmoothScrollTier)}
            disabled={!smoothEnabled}
          >
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="direct">跟手</SelectItem>
              <SelectItem value="medium">适中</SelectItem>
              <SelectItem value="momentum">动量</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="mt-3 flex items-center justify-between gap-4">
          <div className="text-sm">
            按住左键拖动滚动
            <div className="text-muted-foreground text-xs">
              开（默认）：按住左键拖动 = 内容跟随滚动，Shift+左键拖动 = 选择文字；
              关：左键拖动恢复为选择文字，改用 Shift+左键拖动滚动。
              习惯「左键就是选字」的用户关掉即可。
            </div>
          </div>
          <Switch
            checked={dragScroll}
            onCheckedChange={setDragScroll}
            disabled={!smoothEnabled}
          />
        </div>
      </div>
    </div>
  )
}

export default General
