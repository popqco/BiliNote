import { useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import { Loader2, Play, Save, SendHorizonal } from 'lucide-react'

import { Button } from '@/components/ui/button.tsx'
import { Input } from '@/components/ui/input.tsx'
import { Checkbox } from '@/components/ui/checkbox.tsx'
import { Alert, AlertDescription } from '@/components/ui/alert.tsx'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.tsx'
import { noteStyles, noteFormats } from '@/constant/note.ts'
import { useModelStore } from '@/store/modelStore'
import {
  get_automation_config,
  save_automation_config,
  test_notify,
  run_automation_now,
  get_automation_status,
  check_automation_login,
} from '@/services/automation.ts'

/** 不可变地写入深层字段 */
const setIn = (obj: any, path: string[], value: any) => {
  const clone = JSON.parse(JSON.stringify(obj))
  let cur = clone
  for (let i = 0; i < path.length - 1; i++) cur = cur[path[i]]
  cur[path[path.length - 1]] = value
  return clone
}

const Section = ({ title, desc, children }: any) => (
  <div className="border-border mt-6 rounded-lg border p-4">
    <div className="font-medium">{title}</div>
    {desc && <div className="text-muted-foreground mt-1 text-xs">{desc}</div>}
    <div className="mt-3 flex flex-col gap-3">{children}</div>
  </div>
)

const Row = ({ label, desc, children }: any) => (
  <div className="flex items-center justify-between gap-4">
    <div className="text-sm">
      {label}
      {desc && <div className="text-muted-foreground text-xs">{desc}</div>}
    </div>
    <div className="flex items-center gap-2">{children}</div>
  </div>
)

/**
 * 「自动化」设置：定期检索 B 站「稍后再看」，为未总结/新增视频自动生成笔记，
 * 每轮结束后向微信（WxPusher）/邮箱（SMTP）发送汇总通知。
 * 配置存于后端 config/automation.json；Windows 计划任务模式见 automation_cli.py。
 */
const Automation = () => {
  const [cfg, setCfg] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<any>(null)
  const { loadEnabledModels, modelList } = useModelStore()

  useEffect(() => {
    loadEnabledModels()
    get_automation_config().then(c => setCfg(c)).catch(() => {})
    refreshStatus()
  }, [])

  const refreshStatus = () => get_automation_status().then(s => setStatus(s)).catch(() => {})

  // 一轮可能跑几分钟（下载+转写+生成）：跑的过程中让面板自己刷新，
  // 用户能看到阶段/提交数变化，而不是盯着一个静止的「进行中」。
  useEffect(() => {
    if (!status?.running) return
    const t = window.setInterval(() => {
      get_automation_status().then(setStatus).catch(() => {})
    }, 5000)
    return () => window.clearInterval(t)
  }, [status?.running])

  if (!cfg) {
    return (
      <div className="text-muted-foreground flex h-40 items-center justify-center gap-2 text-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> 加载自动化配置…
      </div>
    )
  }

  const upd = (path: string[], value: any) => setCfg((c: any) => setIn(c, path, value))

  const onSave = async () => {
    setBusy(true)
    try {
      const saved = await save_automation_config(cfg)
      setCfg(saved)
      toast.success('自动化配置已保存')
    } catch (e) {
      console.error(e)
    } finally {
      setBusy(false)
    }
  }

  const onTestNotify = async () => {
    setBusy(true)
    try {
      const res = await test_notify()
      const results = (res && (res as any).results) || []
      const lines = results.map((r: any) => `${r.ok ? '✅' : '❌'} ${r.channel}：${r.detail}`)
      toast(lines.join('\n') || '没有可用的通知渠道', { duration: 6000 })
    } catch (e) {
      console.error(e)
    } finally {
      setBusy(false)
    }
  }

  /**
   * 「立即运行一轮」：先校验 Cookie（失败当场给出原因，不再"点了没反应"），
   * 触发后轮询 /automation/status，把本轮提交/跳过明细或失败原因回显出来。
   */
  const onRunNow = async () => {
    setBusy(true)
    let poll: number | undefined
    try {
      const login: any = await check_automation_login()
      const n = Number(login?.count ?? 0)
      if (n === 0) {
        toast('「稍后再看」当前是空的，本轮不会有新任务', { icon: 'ℹ️', duration: 6000 })
      } else {
        toast.success(`登录有效，「稍后再看」共 ${n} 个视频，开始检查…`)
      }
      await run_automation_now()

      poll = window.setInterval(async () => {
        const st: any = await get_automation_status().catch(() => null)
        setStatus(st)
        if (!st) return
        if (st.last_error) {
          toast.error(st.last_error, { duration: 8000 })
          clearInterval(poll)
        } else if (st.progress?.submitted) {
          const sub = st.progress.submitted.length
          const skip = (st.progress.skipped || []).length
          toast.success(`本轮已提交 ${sub} 个任务，跳过 ${skip} 个（详见下方最近一轮）`, { duration: 6000 })
          clearInterval(poll)
        }
      }, 2000)
      window.setTimeout(() => poll && clearInterval(poll), 30000)
    } catch (e: any) {
      toast.error(e?.msg || '触发失败，请稍后再试', { duration: 8000 })
    } finally {
      setBusy(false)
    }
  }

  const gen = cfg.gen || {}
  const wx = cfg.notify?.wxpusher || {}
  const smtp = cfg.notify?.smtp || {}

  return (
    <div className="bg-card h-full overflow-auto p-8">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-2xl font-medium">自动化</div>
          <div className="text-muted-foreground mt-1 text-sm">
            定期检索「稍后再看」，为还没有笔记的视频自动生成总结，并按轮次推送汇总通知。
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={onRunNow} disabled={busy}>
            <Play className="mr-2 h-4 w-4" /> 立即运行一轮
          </Button>
          <Button onClick={onSave} disabled={busy}>
            {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
            保存
          </Button>
        </div>
      </div>

      {/* 最近一轮：点了「立即运行一轮」却看不到任何动静是最难查的问题，
          这里直接把后端的阶段/提交/跳过/失败原因摊开显示 */}
      {status && (status.running || status.last_result || status.last_error || status.progress) && (
        <div className="border-border bg-muted/40 mt-4 rounded-lg border p-4 text-sm">
          <div className="flex items-center gap-2 font-medium">
            {status.running ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : status.last_error ? (
              <span className="text-red-500">✕</span>
            ) : (
              <span className="text-green-500">✓</span>
            )}
            最近一轮：{status.phase || (status.running ? '进行中' : '未知')}
            {status.last_round_at && <span className="text-muted-foreground font-normal">（{status.last_round_at.replace('T', ' ').slice(0, 19)}）</span>}
          </div>
          {status.last_error && <div className="text-red-500 mt-2">失败原因：{status.last_error}</div>}
          {status.progress && (
            <div className="text-muted-foreground mt-2 flex flex-col gap-1">
              <div>
                稍后再看 {status.progress.total_in_list} 个 · 提交 {status.progress.submitted?.length ?? 0} 个 · 跳过{' '}
                {status.progress.skipped?.length ?? 0} 个
              </div>
              {(status.progress.submitted || []).map((s: any) => (
                <div key={s.task_id} className="truncate">▸ 已提交：{s.title}（{s.bvid}）</div>
              ))}
              {(status.progress.skipped || []).map((s: any) => (
                <div key={s.bvid} className="truncate">▸ 跳过 {s.bvid}：{s.reason}</div>
              ))}
            </div>
          )}
        </div>
      )}

      <Section title="基础设置" desc="检查轮只在 BiliNote 运行时由应用内调度执行；配置 Windows 计划任务后关掉应用也能跑（automation_cli.py）。">
        <Row label="启用自动化">
          <Checkbox checked={!!cfg.enabled} onCheckedChange={v => upd(['enabled'], !!v)} />
        </Row>
        <Row label="检查频率（分钟）">
          <Input
            type="number"
            className="w-28"
            value={cfg.interval_minutes}
            onChange={e => upd(['interval_minutes'], Number(e.target.value) || 120)}
          />
        </Row>
        <Row label="总结范围">
          <Select value={cfg.mode} onValueChange={v => upd(['mode'], v)}>
            <SelectTrigger className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部未总结的视频</SelectItem>
              <SelectItem value="window">仅最近新增</SelectItem>
            </SelectContent>
          </Select>
          {cfg.mode === 'window' && (
            <span className="text-muted-foreground flex items-center gap-1 text-xs">
              最近
              <Input
                type="number"
                className="h-8 w-16"
                value={cfg.window_days}
                onChange={e => upd(['window_days'], Number(e.target.value) || 7)}
              />
              天
            </span>
          )}
        </Row>
        <Row label="每轮最多新提交（防灌爆队列）">
          <Input
            type="number"
            className="w-28"
            value={cfg.max_per_round}
            onChange={e => upd(['max_per_round'], Number(e.target.value) || 5)}
          />
        </Row>
      </Section>

      <Section title="生成配置（自动化专用）" desc="无人值守时使用的模型与风格，独立于首页表单的当前选择。">
        <Row label="模型（供应商）">
          <Select
            value={gen.model_name || ''}
            onValueChange={v => {
              const m = modelList.find(m => m.model_name === v)
              setCfg((c: any) =>
                setIn(setIn(c, ['gen', 'model_name'], v), ['gen', 'provider_id'], m?.provider_id || ''),
              )
            }}
          >
            <SelectTrigger className="w-56">
              <SelectValue placeholder="选择模型" />
            </SelectTrigger>
            <SelectContent>
              {modelList.map(m => (
                <SelectItem key={m.id} value={m.model_name}>
                  {m.model_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Row>
        <Row label="笔记风格">
          <Select value={gen.style} onValueChange={v => upd(['gen', 'style'], v)}>
            <SelectTrigger className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {noteStyles.map(({ label, value }) => (
                <SelectItem key={value} value={value}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Row>
        <Row label="下载质量">
          <Select value={gen.quality} onValueChange={v => upd(['gen', 'quality'], v)}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="fast">fast</SelectItem>
              <SelectItem value="medium">medium</SelectItem>
              <SelectItem value="slow">slow</SelectItem>
            </SelectContent>
          </Select>
        </Row>
        <Row label="视频理解（抽帧辅助分析）">
          <Checkbox
            checked={!!gen.video_understanding}
            onCheckedChange={v => upd(['gen', 'video_understanding'], !!v)}
          />
        </Row>
        <div className="flex flex-wrap items-center gap-4">
          {noteFormats.map(({ label, value }) => (
            <label key={value} className="flex items-center space-x-2 text-sm">
              <Checkbox
                checked={(gen.format || []).includes(value)}
                onCheckedChange={checked =>
                  upd(
                    ['gen', 'format'],
                    checked
                      ? [...(gen.format || []), value]
                      : (gen.format || []).filter((x: string) => x !== value),
                  )
                }
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
      </Section>

      <Section title="通知渠道" desc="每轮有新任务时才发一条汇总（0 成功 0 失败的空轮不打扰）；两个渠道可独立启用、独立失败。填完点「发送测试通知」即可验证，收不到会直接显示原因。">
        <Row label="微信推送（WxPusher）" desc="免费，微信里直接收推送，推荐">
          <Checkbox checked={!!wx.enabled} onCheckedChange={v => upd(['notify', 'wxpusher', 'enabled'], !!v)} />
        </Row>
        <div className="grid grid-cols-2 gap-3">
          <Input
            placeholder="appToken（wxpusher.zjiecode.com 创建应用获取）"
            value={wx.app_token}
            onChange={e => upd(['notify', 'wxpusher', 'app_token'], e.target.value)}
          />
          <Input
            placeholder="UID（微信扫码关注公众号后获取，多个用逗号分隔）"
            value={wx.uids}
            onChange={e => upd(['notify', 'wxpusher', 'uids'], e.target.value)}
          />
        </div>
        <Row label="邮箱（SMTP）" desc="需要邮箱服务商给的「授权码」，不是登录密码">
          <Checkbox checked={!!smtp.enabled} onCheckedChange={v => upd(['notify', 'smtp', 'enabled'], !!v)} />
        </Row>
        <div className="grid grid-cols-2 gap-3">
          <Input placeholder="SMTP 服务器（如 smtp.qq.com）" value={smtp.host} onChange={e => upd(['notify', 'smtp', 'host'], e.target.value)} />
          <Input placeholder="端口（465 = SSL，587 = STARTTLS）" value={smtp.port} onChange={e => upd(['notify', 'smtp', 'port'], Number(e.target.value) || 465)} />
          <Input placeholder="邮箱账号" value={smtp.username} onChange={e => upd(['notify', 'smtp', 'username'], e.target.value)} />
          <Input
            placeholder="SMTP 授权码（不是登录密码）"
            type="password"
            value={smtp.password}
            onChange={e => upd(['notify', 'smtp', 'password'], e.target.value)}
          />
          <Input placeholder="收件人（多个用逗号分隔）" value={smtp.to} onChange={e => upd(['notify', 'smtp', 'to'], e.target.value)} />
        </div>
        <div>
          <Button variant="outline" onClick={onTestNotify} disabled={busy}>
            <SendHorizonal className="mr-2 h-4 w-4" /> 发送测试通知
          </Button>
        </div>
      </Section>

      <Alert variant="warning" className="mt-6 text-sm">
        <AlertDescription>
          <strong>提示：</strong>自动化读取你的 B 站 Cookie 获取「稍后再看」列表；Cookie 失效时会停止并在日志中提示，
          请到「下载配置」更新。Windows 计划任务模式需要手动注册一次（见仓库 automation_cli.py 顶部说明）。
          WxPusher / 邮箱的逐步配置教程见仓库 <code className="rounded bg-muted px-1">docs/automation.md</code> 与 README。
        </AlertDescription>
      </Alert>
    </div>
  )
}

export default Automation
