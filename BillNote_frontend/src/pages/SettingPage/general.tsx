import toast from 'react-hot-toast'
import { useEffect, useRef, useState } from 'react'
import { Switch } from '@/components/ui/switch.tsx'
import { Input } from '@/components/ui/input.tsx'
import { Button } from '@/components/ui/button.tsx'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.tsx'
import { useSystemStore, type SmoothScrollTier } from '@/store/configStore'
import { get_task_config, save_task_config, type ScreenshotPlanner, type TaskConfig } from '@/services/taskConfig.ts'
import {
  get_screenshot_stats,
  slim_screenshots,
  type ScreenshotStats,
  type SlimResult,
} from '@/services/maintenance.ts'

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
 * 第四项：生成性能与预算 —— 同时生成笔记数、视频理解抽帧/拼图预算、分批总结
 * 并发。落盘后端 config/task_config.json（2026-10-11 起 UI 化，不再依赖环境变量），
 * 预算即时生效（每个任务启动时读取），并发数热切换不打断在途任务。
 */
// 每个数值字段的 UI 上下限（后端还有更宽的夹取兜底）
type NumericTaskKey =
  | 'max_workers'
  | 'frame_budget'
  | 'image_budget'
  | 'summarize_batch_concurrency'
  | 'screenshot_interval'
  | 'screenshot_max_count'

const TASK_CFG_LIMITS: Record<NumericTaskKey, [number, number]> = {
  max_workers: [1, 8],
  frame_budget: [100, 200000],
  image_budget: [10, 50000],
  summarize_batch_concurrency: [1, 4],
  screenshot_interval: [20, 900],
  screenshot_max_count: [3, 200],
}

const TASK_CFG_META: Array<{
  key: NumericTaskKey
  label: string
  desc: string
  suffix: string
}> = [
  {
    key: 'max_workers',
    label: '同时生成笔记数',
    desc: '全应用同时处理几条笔记任务（下载/转写/总结可互相重叠）。批量导入与自动化也受此约束；调大同时吃更多带宽/显存，上游模型也可能限流。',
    suffix: '条',
  },
  {
    key: 'frame_budget',
    label: '抽帧预算',
    desc: '单个任务最多抽多少帧。超出时自动逐级升格（2×2→3×3→拉间隔）兜底。调高长视频保留更多细节，代价是截图占盘与处理时间。',
    suffix: '帧',
  },
  {
    key: 'image_budget',
    label: '拼图预算',
    desc: '单任务最多喂给模型多少张拼图。拼图按时间对齐分批发送（每批 ≤20 张），预算越大批次数越多、总结越慢越耗额度。',
    suffix: '张',
  },
  {
    key: 'summarize_batch_concurrency',
    label: '分批总结并发',
    desc: '长视频分批总结时同时发几批。调大提速明显，但上游并发限流时会多失败重试；1 = 旧的逐批串行。',
    suffix: '批',
  },
  {
    key: 'screenshot_interval',
    label: '截图预算间隔',
    desc: '自适应编排下只用来换算目标张数（≈ 时长 ÷ 间隔，再受单篇上限约束）：密集段多截、口播段少截，不再均匀切分。切到旧版固定间隔后，它恢复为「平均每多少秒最多 1 张」。',
    suffix: '秒',
  },
  {
    key: 'screenshot_max_count',
    label: '单篇截图上限',
    desc: '一篇笔记最多保留多少张原片截图（还会受视频时长约束）。默认 60；调小更清爽，调大保留更多画面细节。',
    suffix: '张',
  },
]
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

  const [taskCfg, setTaskCfg] = useState<TaskConfig | null>(null)
  // 原片截图维护（2026-10-27）：体检结果、预览、执行结果
  const [shotStats, setShotStats] = useState<ScreenshotStats | null>(null)
  const [shotStatsLoading, setShotStatsLoading] = useState(false)
  const [slimBusy, setSlimBusy] = useState(false)
  // 默认处理“全部超限笔记”：库内最大张数可能已经低于 100（2026-10-28 实测
  // 最大 99），默认 ≥100 会变成空操作，用户点完以为已清理干净。
  const [slimMin, setSlimMin] = useState('0')
  const [slimPreview, setSlimPreview] = useState<SlimResult | null>(null)
  const [slimConfirm, setSlimConfirm] = useState(false)
  const [slimDone, setSlimDone] = useState<SlimResult | null>(null)
  // 服务器确认过的基线。注意不能拿 taskCfg 当比较基准：onChange 打字过程
  // 已把输入值同步进 taskCfg，失焦时 v !== taskCfg[key] 恒为 false，POST 永远
  // 发不出去（2026-10-11 的"设置不保存"bug 根因）。savedRef 只被
  // 加载成功 / 保存成功两条路径改写。
  const savedRef = useRef<TaskConfig | null>(null)
  // 提交序号：慢响应回来时若已有更新的提交，只用它更新基线，不回写编辑态
  const commitSeqRef = useRef(0)
  useEffect(() => {
    get_task_config()
      .then(cfg => {
        savedRef.current = cfg
        setTaskCfg(cfg)
      })
      .catch(() => {
        savedRef.current = null
        setTaskCfg(null)
      })
  }, [])

  const commitTaskCfg = async (patch: Partial<TaskConfig>) => {
    const mySeq = ++commitSeqRef.current
    try {
      const saved = await save_task_config(patch)
      savedRef.current = saved
      if (mySeq === commitSeqRef.current) setTaskCfg(saved)
      toast.success('已保存，后续任务即时生效')
    } catch (e: any) {
      toast.error(e?.msg || '保存失败')
      if (mySeq === commitSeqRef.current) {
        setTaskCfg(savedRef.current ? { ...savedRef.current } : null)
      }
    }
  }

  // 数字输入失焦时提交：与「服务器基线」比较，范围内且真的变了才发请求，
  // 非法值回滚到基线（而不是把脏本地值原样写回）
  const commitTaskCfgNumber = (key: NumericTaskKey, raw: string) => {
    const base = savedRef.current
    if (!base) return
    const [lo, hi] = TASK_CFG_LIMITS[key]
    if (raw.trim() === '') {
      // 清空后失焦：静默回滚到基线
      setTaskCfg({ ...base })
      return
    }
    const n = Number(raw)
    if (!Number.isFinite(n) || n < lo || n > hi) {
      toast.error(`请输入 ${lo}–${hi} 之间的整数`)
      setTaskCfg({ ...base })
      return
    }
    const v = Math.round(n)
    if (v !== base[key]) {
      commitTaskCfg({ [key]: v } as Partial<TaskConfig>)
    } else {
      setTaskCfg({ ...base })
    }
  }

  const handleIntervalChange = (v: string) => {
    const n = Number(v)
    if (!Number.isFinite(n) || n < 1 || n > 60) {
      toast.error('轮询间隔需在 1–60 秒之间')
      return
    }
    setPollIntervalSec(n)
  }

  /* ---------------- 原片截图维护 ---------------- */

  const loadShotStats = async () => {
    setShotStatsLoading(true)
    try {
      setShotStats(await get_screenshot_stats())
    } catch (e: any) {
      toast.error(e?.msg || '截图体检失败（后端可能未更新）')
    } finally {
      setShotStatsLoading(false)
    }
  }

  useEffect(() => {
    void loadShotStats()
  }, [])

  const runSlim = async (dryRun: boolean) => {
    setSlimBusy(true)
    try {
      const result = await slim_screenshots({
        dry_run: dryRun,
        min_images: Number(slimMin) || 0,
      })
      if (dryRun) {
        setSlimPreview(result)
        setSlimConfirm(false)
      } else {
        setSlimDone(result)
        setSlimPreview(null)
        setSlimConfirm(false)
        toast.success(
          `已瘦身 ${result.notes_changed} 篇：${result.images_before} → ${result.images_after} 张`,
        )
        void loadShotStats()
      }
    } catch (e: any) {
      toast.error(e?.msg || (dryRun ? '预览失败' : '瘦身失败'))
    } finally {
      setSlimBusy(false)
    }
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

      <div className="border-border mt-4 max-w-3xl rounded-lg border p-4">
        <div className="text-sm font-medium">生成性能与预算</div>
        <div className="text-muted-foreground mt-1 text-xs">
          全局生效（手动生成、批量导入、自动化共用），失焦自动保存。预算默认是
          2026-10-10 定版的激进档（4800 帧 / 1000 张拼图），免费额度紧张时可调回旧档
          （如 2400/500）。
        </div>
        <div className="mt-3 flex items-center justify-between gap-4">
          <div className="text-sm">
            截图编排
            <div className="text-muted-foreground text-xs">
              自适应：按画面变化、停留时长、清晰度与转写密度打分，把预算分给信息密集的
              段落（口播/过场段自然少截），5 分钟粒度保留覆盖下限。影子对比：仍按旧版
              出图，只在日志里记录两种方案差异，适合先观察一段时间。旧版固定间隔：完全
              沿用均匀抽稀。
            </div>
          </div>
          <Select
            value={taskCfg?.screenshot_planner ?? 'adaptive'}
            onValueChange={v => {
              void commitTaskCfg({ screenshot_planner: v as ScreenshotPlanner })
            }}
            disabled={!taskCfg}
          >
            <SelectTrigger className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="adaptive">自适应（推荐）</SelectItem>
              <SelectItem value="shadow">影子对比（只记日志）</SelectItem>
              <SelectItem value="legacy">旧版固定间隔</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {TASK_CFG_META.map(item => (
          <div key={item.key} className="mt-3 flex items-center justify-between gap-4">
            <div className="text-sm">
              {item.label}
              <div className="text-muted-foreground text-xs">{item.desc}</div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <Input
                type="number"
                className="w-24"
                disabled={!taskCfg}
                value={taskCfg ? String(taskCfg[item.key]) : ''}
                min={TASK_CFG_LIMITS[item.key][0]}
                max={TASK_CFG_LIMITS[item.key][1]}
                onChange={e => {
                  if (!taskCfg) return
                  // 打字过程只更新本地态，失焦才提交
                  const n = Number(e.target.value)
                  if (Number.isFinite(n)) setTaskCfg({ ...taskCfg, [item.key]: n })
                }}
                onBlur={e => commitTaskCfgNumber(item.key, e.target.value)}
              />
              <span className="text-muted-foreground w-4 text-xs">{item.suffix}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="border-border mt-4 max-w-3xl rounded-lg border p-4">
        <div className="text-sm font-medium">原片截图维护</div>
        <div className="text-muted-foreground mt-1 text-xs">
          检查现有笔记的截图数量，并按每篇生成时的密度（旧笔记回退到当前全局：
          每 {shotStats?.interval ?? 90} 秒最多 1 张、单篇上限 {shotStats?.max_count ?? 60} 张）
          一次性瘦身，并顺带清理上一轮瘦身留下的空 bullet / 冒号残行（不动图片）。
          执行前会把旧正文自动归档为一个版本，可在笔记右上角的版本下拉里切回；
          只改正文引用，不删除图片文件。
        </div>

        {shotStats ? (
          <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-6">
            <div>笔记 {shotStats.notes}</div>
            <div>带图 {shotStats.notes_with_shots}</div>
            <div>截图 {shotStats.images.toLocaleString()}</div>
            <div>超限 {shotStats.over_limit}</div>
            <div className={shotStats.notes_with_residue > 0 ? 'text-amber-600' : undefined}>
              残渣 {shotStats.notes_with_residue}
            </div>
            <div>磁盘 {(shotStats.disk.bytes / 1024 ** 3).toFixed(1)} GB</div>
          </div>
        ) : (
          <div className="text-muted-foreground mt-3 text-xs">
            {shotStatsLoading ? '正在体检…' : '暂无数据'}
          </div>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void loadShotStats()}
            disabled={shotStatsLoading || slimBusy}
          >
            {shotStatsLoading ? '体检中…' : '重新体检'}
          </Button>
          <Select value={slimMin} onValueChange={setSlimMin} disabled={slimBusy}>
            <SelectTrigger className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="0">全部超限笔记（推荐）</SelectItem>
              <SelectItem value="50">仅 ≥50 张</SelectItem>
              <SelectItem value="100">仅 ≥100 张</SelectItem>
              <SelectItem value="200">仅 ≥200 张</SelectItem>
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void runSlim(true)}
            disabled={slimBusy}
          >
            {slimBusy ? '处理中…' : '预览瘦身'}
          </Button>
          {slimConfirm ? (
            <Button
              variant="destructive"
              size="sm"
              onClick={() => void runSlim(false)}
              disabled={slimBusy}
            >
              {slimBusy ? '执行中…' : '确认执行（旧版本自动归档）'}
            </Button>
          ) : (
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setSlimConfirm(true)}
              disabled={slimBusy}
            >
              执行瘦身
            </Button>
          )}
        </div>

        {slimPreview && (
          <div className="text-muted-foreground border-border mt-3 rounded-md border p-2 text-xs">
            预览：影响 {slimPreview.notes_scanned} 篇中的 {slimPreview.notes_changed} 篇，
            {slimPreview.images_before.toLocaleString()} →{' '}
            {slimPreview.images_after.toLocaleString()} 张
            {slimPreview.residue_cleaned > 0 &&
              `；另清理 ${slimPreview.residue_cleaned} 篇历史瘦身残渣（空 bullet/冒号行，不动图片）`}
            {slimPreview.skipped_active > 0 &&
              `（${slimPreview.skipped_active} 篇生成中已跳过）`}
            {slimPreview.skipped_archive > 0 &&
              `（${slimPreview.skipped_archive} 篇归档失败已跳过）`}
            {slimPreview.details.slice(0, 5).map(item => (
              <div key={item.task_id} className="mt-1 truncate">
                · {item.title || item.task_id.slice(0, 8)}：{item.before} → {item.after}
              </div>
            ))}
          </div>
        )}
        {slimDone && (
          <div className="text-muted-foreground mt-3 text-xs">
            已完成：{slimDone.notes_changed} 篇，
            {slimDone.images_before.toLocaleString()} →{' '}
            {slimDone.images_after.toLocaleString()} 张
            {slimDone.residue_cleaned > 0 &&
              `；另清理 ${slimDone.residue_cleaned} 篇历史残渣`}
            {slimDone.skipped_archive > 0 &&
              `；${slimDone.skipped_archive} 篇归档失败已跳过`}。
          </div>
        )}
      </div>
    </div>
  )
}

export default General
