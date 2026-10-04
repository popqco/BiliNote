import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import toast from 'react-hot-toast'
import { Button } from '@/components/ui/button.tsx'
import { Input } from '@/components/ui/input.tsx'
import {
  clearWorkerConnection,
  loadWorkerConnection,
  resolveApiBaseUrl,
  saveWorkerConnection,
} from '@/utils/workerConnection.ts'

/**
 * 「连接 Worker」设置页：Viewer 直连 Worker 的配对入口（票 3）。
 *
 * - 本机 All-in-One 默认未配对：所有请求走同源/构建期地址，行为与原来一致。
 * - 填入 Worker 地址 + 配对 token → 先调免鉴的 /pairing_verify 校验 → 通过才保存。
 * - 连接信息存本机 localStorage，axios 请求拦截器每次请求自动带 token。
 * - 断开连接 = 清除本机保存，回到本机模式（不涉及 Worker 侧任何变更）。
 */
export default function ConnectionPage() {
  const navigate = useNavigate()
  const [baseUrl, setBaseUrl] = useState('')
  const [token, setToken] = useState('')
  const [connected, setConnected] = useState(false)
  const [checking, setChecking] = useState(false)

  // 进页回显本机已保存的连接（地址回显、token 不回显，由用户重输）
  useEffect(() => {
    const saved = loadWorkerConnection()
    if (saved) {
      setBaseUrl(saved.baseUrl)
      setConnected(true)
    }
    // 一键配对链接：支持 #pair=<token> 自动填充 token（地址栏随即清除，
    // token 不留痕不进服务器日志）。手机上手动抄 43 位 token 极易错，
    // 从 Worker 本机页面复制完整链接在手机打开即可。
    const m = window.location.hash.match(/pair=([A-Za-z0-9_-]+)/)
    if (m) {
      setToken(m[1])
      window.history.replaceState(null, '', window.location.pathname + window.location.search)
      toast.success('已从链接填入配对 token，点击「配对并连接/更新配对」完成配对')
    }
  }, [])

  // 地址规整：只输 IP[:端口] 也能连——自动补 http:// 前缀、省略端口补默认
  // 8483、去掉尾部斜杠与误贴的 /api 后缀（2026-10-04 用户反馈：不想手输前缀）。
  // 注意 new URL("http://host").pathname 恒为 "/"（WHATWG 规定），所以不能用
  // !u.pathname 判断“裸主机”——之前这个分支永远走不到，“省略端口默认 8483”
  // 只是文案上说说而已（2026-10-04 端口健壮度复查发现）。
  const normalizeBase = (raw: string) => {
    let v = raw.trim()
    if (!v) return ''
    if (!/^https?:\/\//i.test(v)) v = `http://${v}`
    v = v.replace(/\/+$/, '').replace(/\/api$/, '')
    try {
      const u = new URL(v)
      // 纯主机名（pathname 只剩 "/"）且没写端口 → 补默认 8483
      if (u.protocol === 'http:' && !u.port && (u.pathname === '' || u.pathname === '/')) {
        v = `${v}:8483`
      }
    } catch {
      // 解析不了就原样返回，由 sys_check 校验报连接失败
    }
    return v
  }

  const handleVerifyAndSave = async () => {
    const base = normalizeBase(baseUrl)
    const t = token.trim()
    if (!base) {
      toast.error('请填写 Worker 地址，例如 100.64.0.5:8483')
      return
    }
    if (!t) {
      toast.error('请填写配对 token（在 Worker 本机查看）')
      return
    }
    setChecking(true)
    try {
      // 先验目标端口在 Worker 本机是否被占用（免鉴 /port_check，打 Worker 自己）：
      // 端口被别的程序占了，sys_check 会连到一个“能通但不是 Worker”的服务，
      // pairing_verify 再报 token 不对——用户会误以为 token 抄错了。
      // 先把“端口对不对”这件事单独说清楚。
      try {
        const u = new URL(base)
        if (u.port) {
          const pc = await fetch(`${base}/api/port_check?port=${u.port}`)
          const pj = await pc.json().catch(() => null)
          if (pc.ok && pj?.code === 0 && pj?.data?.taken === false) {
            throw new Error(`Worker 本机上端口 ${u.port} 没有程序在监听：地址或端口写错了？`)
          }
        }
      } catch (e: any) {
        // 上面主动抛出的“端口空闲”要继续往外抛；纯网络异常才吞掉走正常校验
        if (typeof e?.message === 'string' && e.message.includes('没有程序在监听')) throw e
      }
      // 免鉴接口：先验连通性（/sys_check），再验 token（/pairing_verify）
      const sysRes = await fetch(`${base}/api/sys_check`)
      if (!sysRes.ok) throw new Error('Worker 无响应')
      const verifyRes = await fetch(`${base}/api/pairing_verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: t }),
      })
      const verifyJson = await verifyRes.json().catch(() => null)
      if (!verifyRes.ok || verifyJson?.code !== 0 || verifyJson?.data?.ok !== true) {
        toast.error('配对 token 不正确，请核对后重试')
        return
      }
      saveWorkerConnection({ baseUrl: base, token: t })
      setConnected(true)
      setToken('')
      // 通知首页横幅消失 + 直接回首页（请求拦截器每次重算 baseURL，无需手动刷新）
      window.dispatchEvent(new Event('bilinote:worker-paired'))
      toast.success('已配对，正在前往首页…')
      navigate('/')
    }
    catch (e: any) {
      toast.error(typeof e?.message === 'string' ? `连接失败：${e.message}` : '连接失败，请检查地址与网络')
    }
    finally {
      setChecking(false)
    }
  }

  const handleDisconnect = () => {
    clearWorkerConnection()
    setConnected(false)
    setToken('')
    window.dispatchEvent(new Event('bilinote:worker-unpaired'))
    toast.success('已断开，回到本机模式。')
  }

  return (
    <div className="bg-card h-full overflow-auto p-4 sm:p-8">
      <div className="text-xl font-medium sm:text-2xl">连接 Worker</div>
      <div className="text-muted-foreground mt-1 text-sm">
        Viewer 直连远端 Worker：一对一配对，不经过中继服务器。未配对时即本机 All-in-One 模式。
      </div>

      <div className="border-border mt-6 max-w-3xl rounded-lg border p-4">
        {/* 手机窄屏上状态与按钮上下排：之前左右排把长地址挤成几字一行的竖条。 */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <div className="min-w-0 text-sm">
            当前状态
            <div className="text-muted-foreground mt-0.5 text-xs break-all">
              {connected
                ? (<>
                    <div>已配对：{loadWorkerConnection()?.baseUrl ?? ''}</div>
                    <div>请求地址：{resolveApiBaseUrl()}</div>
                  </>)
                : '未配对：本机模式（All-in-One）'}
            </div>
          </div>
          {connected && (
            <Button size="sm" variant="outline" onClick={handleDisconnect} className="shrink-0 self-start sm:self-auto">
              断开连接
            </Button>
          )}
        </div>

        <div className="mt-4 space-y-3">
          <div>
            <div className="mb-1 text-sm">Worker 地址</div>
            <Input
              placeholder="例如 100.64.0.5:8483（可省略 http:// 和端口）"
              value={baseUrl}
              onChange={e => setBaseUrl(e.target.value)}
            />
            {baseUrl.trim() && (
              <div className="text-muted-foreground mt-1 text-xs">
                保存时将连接：<span className="font-mono">{normalizeBase(baseUrl)}</span>
              </div>
            )}
            <div className="text-muted-foreground mt-1 text-xs">
              可不带 http:// 前缀与 /api 后缀；省略端口时默认 8483。
            </div>
          </div>
          <div>
            <div className="mb-1 text-sm">配对 token</div>
            <Input
              type="password"
              placeholder={connected ? '已保存（修改需重输完整 token）' : '在 Worker 本机查看配对 token 后填入'}
              value={token}
              onChange={e => setToken(e.target.value)}
            />
            <div className="text-muted-foreground mt-1 text-xs">
              已保存的 token 不回显；改动必须重输完整 token，空着保存则保持原值。
            </div>
          </div>
          <Button size="sm" onClick={handleVerifyAndSave} disabled={checking}>
            {checking ? '校验中…' : connected ? '更新配对' : '配对并连接'}
          </Button>
        </div>
      </div>

      <WorkerTokenPanel />

      <WorkerAddressPanel />

      <RemoteControlPanel />

      <div className="text-muted-foreground mt-4 max-w-3xl text-xs">
        跨公网请先用 Tailscale / ZeroTier 组网打通两台设备，再填组网地址配对。
        账号体系不在 MVP 范围：能连上组网、拿对 token 即视为本人。
      </div>
    </div>
  )
}

/**
 * raw fetch 不走 axios 拦截器，配对 token 要手动带上——否则远端 Viewer
 * （明明已配对）调这些接口也会 401，「远端配置总开关」就永远卡在「读取中…」。
 */
const pairingHeaders = (): Record<string, string> => {
  const token = loadWorkerConnection()?.token
  return token ? { 'X-Pairing-Token': token } : {}
}

/**
 * Worker 本机 token 面板：调用需鉴权的 /pairing_token 查看完整 token、
 * /pairing_regenerate 重生成。远端未配对调不通（401），本机回环免鉴可看。
 * token 显示后提供复制按钮；重生成后旧 Viewer 全部失效，需重新配对。
 */
function WorkerTokenPanel() {
  const [masked, setMasked] = useState<string | null>(null)
  const [revealed, setRevealed] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [regenLoading, setRegenLoading] = useState(false)

  const fetchToken = async () => {
    setLoading(true)
    try {
      const res = await fetch(`${resolveApiBaseUrl()}/pairing_token`)
      if (res.status === 401) {
        toast.error('远端未配对无权查看：请在本机 All-in-One 上打开此页')
        return
      }
      if (!res.ok) throw new Error('请求失败')
      const json = await res.json().catch(() => null)
      if (json?.code !== 0) throw new Error(json?.msg || '请求失败')
      setMasked(json.data.masked)
      setRevealed(json.data.token)
    }
    catch (e: any) {
      toast.error(typeof e?.message === 'string' ? `查看失败：${e.message}` : '查看失败')
    }
    finally {
      setLoading(false)
    }
  }

  const handleRegenerate = async () => {
    if (!window.confirm('重生成后已配对的 Viewer 全部失效，需重新配对。确定继续？')) return
    setRegenLoading(true)
    try {
      const res = await fetch(`${resolveApiBaseUrl()}/pairing_regenerate`, { method: 'POST' })
      if (res.status === 401) {
        toast.error('远端未配对无权操作：请在本机 All-in-One 上打开此页')
        return
      }
      const json = await res.json().catch(() => null)
      if (!res.ok || json?.code !== 0) throw new Error(json?.msg || '请求失败')
      setMasked(null)
      setRevealed(json.data.token)
      toast.success('已重生成：旧 Viewer 需用新 token 重新配对')
    }
    catch (e: any) {
      toast.error(typeof e?.message === 'string' ? `重生成失败：${e.message}` : '重生成失败')
    }
    finally {
      setRegenLoading(false)
    }
  }

  const handleCopy = async () => {
    if (!revealed) return
    try {
      await navigator.clipboard.writeText(revealed)
      toast.success('已复制')
    }
    catch {
      toast.error('复制失败，请手动选择复制')
    }
  }

  return (
    <div className="border-border mt-6 max-w-3xl rounded-lg border p-4">
      <div className="text-sm">
        本机 Worker 配对 token
        <div className="text-muted-foreground text-xs">
          在 Worker 本机打开此页查看完整 token，抄到 Viewer 上配对。远端未配对无权查看。
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={fetchToken} disabled={loading}>
          {loading ? '读取中…' : revealed ? '重新读取' : '查看本机 token'}
        </Button>
        <Button size="sm" variant="outline" onClick={handleRegenerate} disabled={regenLoading}>
          {regenLoading ? '生成中…' : '重新生成'}
        </Button>
        {revealed && (
          <Button size="sm" variant="outline" onClick={handleCopy}>
            复制
          </Button>
        )}
      </div>
      {masked && (
        <div className="text-muted-foreground mt-2 text-xs">
          脱敏：{masked}
        </div>
      )}
      {revealed && (
        <pre className="mt-2 max-w-full overflow-x-auto rounded bg-zinc-900 px-2 py-1.5 font-mono text-[11px] leading-snug text-green-200">
          {revealed}
        </pre>
      )}
    </div>
  )
}

/**
 * 本机 Worker 地址面板：直接告诉用户"手机/其他设备上该填什么地址"——
 * 端口与可达 IP 由后端 /worker_info 返回（仅本机回环可查；远端 Viewer
 * 打开此页时 401 → 整块自动隐藏）。token 可读时附一键配对链接
 * （#pair=，手机浏览器打开自动填 token，见页面顶部 hash 填充逻辑）。
 */
function WorkerAddressPanel() {
  const [info, setInfo] = useState<{
    port: number
    addresses: Array<{ ip: string; url: string; label: string }>
  } | null>(null)
  const [hidden, setHidden] = useState(false)
  const [token, setToken] = useState<string | null>(null)
  // 端口自检：本机 8483（或当前端口）是否真的是我们的后端在监听。
  const [portProbe, setPortProbe] = useState<{
    state: 'ok' | 'foreign' | 'idle'
    detail: string
  }>({ state: 'idle', detail: '' })

  useEffect(() => {
    // 仅本机可见：worker_info 对远端 401，整块隐藏，token 也不必再取
    fetch(`${resolveApiBaseUrl()}/worker_info`)
      .then(async r => {
        if (!r.ok) throw new Error(String(r.status))
        const j = await r.json().catch(() => null)
        if (j?.code !== 0) throw new Error('bad payload')
        setInfo(j.data)
        // 端口自检：/worker_info 能通说明本端口就是后端自己，直接标正常；
        // 通不过（旧后端 404）才需要 port_check 区分“空闲/被别人占”。
        setPortProbe({
          state: 'ok',
          detail: `端口 ${j.data.port} 由本 Worker 后端监听，一切正常。`,
        })
        return fetch(`${resolveApiBaseUrl()}/pairing_token`)
      })
      .then(async r => {
        if (!r || !r.ok) return
        const j = await r.json().catch(() => null)
        if (j?.code === 0 && j.data?.token) setToken(j.data.token)
      })
      .catch(() => {
        // 兜底：部署的后端还没有 /worker_info（404）时，本机打开此页也能
        // 从地址栏拿到端口——前端就是后端 serve 的，location.port 即端口。
        if (/^(localhost|127\.0\.0\.1)$/.test(window.location.hostname) && window.location.port) {
          setInfo({
            port: Number(window.location.port),
            addresses: [
              {
                ip: window.location.hostname,
                url: window.location.origin,
                label: '本机自用',
              },
            ],
          })
        } else {
          setHidden(true)
        }
      })
  }, [])

  if (hidden || !info) return null

  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast.success(`已复制${what}`)
    } catch {
      toast.error('复制失败，请手动选择复制')
    }
  }

  return (
    <div className="border-border mt-6 max-w-3xl rounded-lg border p-4">
      <div className="text-sm">
        本机 Worker 地址
        <div className="text-muted-foreground text-xs">
          其他设备（手机 Viewer）配对时填写的地址；端口由 Worker 的 BACKEND_PORT
          配置决定，当前 <span className="font-mono">{info.port}</span>。
        </div>
        {/* 端口占用自检状态行 */}
        {portProbe.state !== 'idle' && (
          <div
            className={
              portProbe.state === 'ok'
                ? 'mt-1 text-xs text-green-600 dark:text-green-400'
                : 'mt-1 text-xs text-red-500'
            }
          >
            {portProbe.state === 'ok' ? '● ' : '● '}
            {portProbe.detail}
          </div>
        )}
      </div>
      <div className="mt-3 space-y-3">
        {info.addresses.map(a => (
          <div key={a.ip} className="border-border rounded-md border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="bg-primary-light text-primary rounded px-1.5 py-0.5 text-[10px]">
                {a.label}
              </span>
              <code className="min-w-0 flex-1 break-all font-mono text-xs">{a.url}</code>
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => copy(a.url, '地址')}
              >
                复制地址
              </Button>
              {token && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    copy(`${a.url}/settings/connection#pair=${token}`, '一键配对链接')
                  }
                >
                  复制一键配对链接
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>
      {token && (
        <div className="text-muted-foreground mt-2 text-xs">
          一键配对链接 = 地址 + 连接页 + 已填好的 token；发到手机浏览器打开，
          token 自动填好，点「配对并连接」即完成。
        </div>
      )}
    </div>
  )
}

/**
 * 远控总开关（票 5）：Worker 本机可一键禁止 Viewer 改全局配置。
 * 关闭后远端 Viewer 只能提交/查看任务（任务白名单），改配置类接口 403；
 * 本机回环不受影响。关闭操作仅允许本机发起（防远端误锁），开启可远端发起。
 */
function RemoteControlPanel() {
  const [allowRemote, setAllowRemote] = useState<boolean | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [saving, setSaving] = useState(false)

  const loadRemoteConfig = async () => {
    setLoadError(false)
    try {
      const r = await fetch(`${resolveApiBaseUrl()}/remote_config`, { headers: pairingHeaders() })
      if (r.status === 401) throw new Error('未配对')
      const j = await r.json().catch(() => null)
      if (j?.code !== 0) throw new Error(j?.msg || '读取失败')
      setAllowRemote(!!j.data.allow_remote)
    } catch {
      // 不许永远停在「读取中…」：给出可重试的失败态
      setAllowRemote(null)
      setLoadError(true)
    }
  }

  useEffect(() => {
    loadRemoteConfig()
  }, [])

  const handleToggle = async (next: boolean) => {
    setSaving(true)
    try {
      const res = await fetch(`${resolveApiBaseUrl()}/remote_config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...pairingHeaders() },
        body: JSON.stringify({ allow_remote: next }),
      })
      const json = await res.json().catch(() => null)
      if (!res.ok || json?.code !== 0) {
        toast.error(
          json?.msg ||
            (res.status === 401
              ? '未配对：请先完成配对'
              : res.status === 403
                ? '关闭远控请到 Worker 本机操作'
                : '保存失败'),
        )
        return
      }
      setAllowRemote(!!json.data.allow_remote)
      toast.success(next ? '已允许远端配置' : '已禁止远端配置：Viewer 仅可提交与查看任务')
    } catch {
      toast.error('保存失败，请检查网络')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="border-border mt-6 max-w-3xl rounded-lg border p-4">
      <div className="text-sm">
        远端配置总开关
        <div className="text-muted-foreground text-xs">
          关闭后，已配对的 Viewer 只能提交与查看任务，改全局配置会被拒绝（403）。
          本机 All-in-One 不受影响。关闭操作只能在本机执行，开启可在远端执行。
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant={allowRemote === false ? 'default' : 'outline'}
          disabled={saving}
          onClick={() => (allowRemote === null ? loadRemoteConfig() : handleToggle(!allowRemote))}
        >
          {saving
            ? '保存中…'
            : allowRemote === null
              ? loadError
                ? '读取失败，点击重试'
                : '读取中…'
              : allowRemote
                ? '禁止远端配置'
                : '允许远端配置'}
        </Button>
        {allowRemote !== null && (
          <span className="text-muted-foreground text-xs">
            当前：{allowRemote ? '允许远端配置' : '仅任务（提交/查看），配置已锁'}
          </span>
        )}
      </div>
    </div>
  )
}
