// 下载器 Cookie 设置表单（最简化版）
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { zodResolver } from '@hookform/resolvers/zod'
import {
  Form,
  FormField,
  FormItem,
  FormLabel,
  FormControl,
  FormMessage,
} from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import {
  getDownloaderCookie,
  updateDownloaderCookie,
  getBilibiliAuthStatus,
  startBilibiliQrLogin,
  pollBilibiliQrLogin,
  refreshBilibiliCookie,
  getYuanbaoLoginStatus,
  startYuanbaoLogin,
  cancelYuanbaoLogin,
  clearYuanbaoCookie,
} from '@/services/downloader' // 你自定义的请求
import { useParams } from 'react-router-dom'
import { videoPlatforms } from '@/constant/note.ts'
import { QRCodeSVG } from 'qrcode.react'

const CookieSchema = z.object({
  cookie: z.string().min(10, '请填写有效 Cookie'),
})

const formatTs = ts => (ts ? new Date(ts * 1000).toLocaleString() : '从未')

const DownloaderForm = () => {
  const form = useForm({
    resolver: zodResolver(CookieSchema),
    defaultValues: { cookie: '' },
  })
  const { id } = useParams()
  const isBilibili = id === 'bilibili'
  const isYoutube = id === 'youtube'
  const isWechatChannels = id === 'wechat_channels'
  const isWechatArticle = id === 'wechat_article'

  const [loading, setLoading] = useState(true)
  const [authStatus, setAuthStatus] = useState<any>(null)
  const [qr, setQr] = useState<{ qr_url: string; qrcode_key: string } | null>(null)
  const [qrStatus, setQrStatus] = useState('')
  const [yb, setYb] = useState<any>({ status: 'idle', message: '', has_cookie: false })

  const loadCookie = async () => {
    setLoading(true) // 🔁 切换平台时显示 loading
    try {
      const res = await getDownloaderCookie(id)
      form.reset({ cookie: res?.cookie || '' }) // ✅ 正确重置表单值
    } catch (e) {
      toast.error('加载 Cookie 失败: ' + e)
      form.reset({ cookie: '' }) // ❗失败时也要清空旧值
    } finally {
      setLoading(false)
    }
  }

  const loadAuthStatus = async () => {
    if (!isBilibili) return
    try {
      setAuthStatus(await getBilibiliAuthStatus())
    } catch {
      // 静默：登录态查询失败不影响表单使用
    }
  }

  const loadYuanbaoStatus = async () => {
    if (!isWechatChannels) return
    try {
      setYb(await getYuanbaoLoginStatus())
    } catch {
      // 静默：状态查询失败不影响手动粘贴 Cookie
    }
  }

  useEffect(() => {
    if (id) {
      loadCookie()
      loadAuthStatus()
      loadYuanbaoStatus()
      setQr(null)
      setQrStatus('')
    }
  }, [id]) // 🔁 每当 id 变化时触发

  // B 站扫码状态轮询：confirmed / expired 时停止
  useEffect(() => {
    if (!qr || qrStatus === 'confirmed' || qrStatus === 'expired') return
    const timer = setInterval(async () => {
      try {
        const res: any = await pollBilibiliQrLogin(qr.qrcode_key)
        const st: any = res?.status || 'waiting'
        setQrStatus(st)
        if (st === 'confirmed') {
          toast.success('扫码登录成功，该账号的 Cookie 此后会自动续期')
          if (res?.cookie) form.reset({ cookie: res.cookie })
          setQr(null)
          loadAuthStatus()
        }
      } catch {
        // 轮询失败静默，下个周期重试
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [qr, qrStatus])

  // 元宝扫码状态轮询：后端窗口关闭/成功/失败即停止
  useEffect(() => {
    if (!isWechatChannels || yb?.status !== 'running') return
    const timer = setInterval(async () => {
      try {
        const st: any = await getYuanbaoLoginStatus()
        setYb(st)
        if (st?.status === 'success') {
          toast.success('元宝登录成功，视频号解析已可用')
          loadCookie()
        }
        if (['success', 'failed', 'cancelled'].includes(st?.status)) {
          clearInterval(timer)
        }
      } catch {
        // 静默：下个周期重试
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [isWechatChannels, yb?.status])

  const onStartQr = async () => {
    try {
      const res = await startBilibiliQrLogin()
      setQr({ qr_url: res?.qr_url, qrcode_key: res?.qrcode_key })
      setQrStatus('waiting')
    } catch {
      // 拦截器已 toast 具体原因
    }
  }

  const onRefreshCookie = async () => {
    try {
      await refreshBilibiliCookie()
      toast.success('检查完成')
      loadAuthStatus()
    } catch {
      // 拦截器已 toast 具体原因
    }
  }

  const onStartYuanbao = async () => {
    try {
      const res = await startYuanbaoLogin()
      setYb(res || { status: 'running' })
      toast.success('已打开登录窗口：请用微信扫码登录腾讯元宝')
    } catch {
      // 拦截器已 toast 具体原因
    }
  }

  const onCancelYuanbao = async () => {
    try {
      const res = await cancelYuanbaoLogin()
      setYb(res || { ...yb, status: 'cancelled' })
      toast('已取消扫码登录')
    } catch {
      // 拦截器已 toast 具体原因
    }
  }

  const onClearYuanbao = async () => {
    try {
      const res = await clearYuanbaoCookie()
      setYb(res || { status: 'idle', message: '', has_cookie: false })
      form.reset({ cookie: '' })
      toast.success('已清除元宝登录态')
    } catch {
      // 拦截器已 toast 具体原因
    }
  }

  const onSubmit = async values => {
    try {
      await updateDownloaderCookie({
        platform: id,
        cookie: String(values.cookie),
      })
      toast.success('保存成功')
      loadAuthStatus()
      loadYuanbaoStatus()
    } catch (e) {
      toast.error('保存失败')
    }
  }

  if (loading) return <div className="p-4">加载中...</div>

  // 公众号视频不需要登录态，也不该让用户误以为要填 Cookie
  if (isWechatArticle) {
    return (
      <div className="flex max-w-xl flex-col gap-3 p-4">
        <div className="text-lg font-bold">微信公众号</div>
        <p className="text-sm text-muted-foreground">
          公众号文章视频使用本机 Microsoft Edge / Google Chrome 的真实浏览器会话抓取，无需 Cookie。
        </p>
        <p className="text-sm text-muted-foreground">
          生成笔记时会自动打开一次无头浏览器（约多花 10 秒）；请在 Worker 本机安装 Edge 或 Chrome。
        </p>
        <p className="text-sm text-muted-foreground">
          如果文章里嵌入的是视频号内容，请改用「视频号」平台并粘贴视频号分享链接。
        </p>
      </div>
    )
  }

  return (
    <div className="max-w-xl p-4">
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col gap-4">
          <div className="text-lg font-bold">
            设置{videoPlatforms.find(item => item.value === id)?.label}下载器 Cookie
          </div>

          {isBilibili && (
            <div className="flex flex-col gap-2 rounded-md border p-3">
              <div className="text-sm font-semibold">B 站扫码登录（推荐）</div>
              <div className="text-xs text-muted-foreground">
                用哔哩哔哩 App 扫码登录后，应用会保存完整凭据并在 Cookie 失效前自动续期，
                不再需要手动复制 SESSDATA。
                {authStatus?.username ? ` 当前登录：${authStatus.username}。` : ''}
                {authStatus
                  ? authStatus.can_auto_refresh
                    ? ' ✅ 已具备自动续期凭据。'
                    : ' ⚠️ 当前 Cookie 无法自动续期。'
                  : ''}
                {authStatus?.refreshed_at ? ` 上次续期：${formatTs(authStatus.refreshed_at)}。` : ''}
              </div>
              <div className="flex gap-2">
                <Button type="button" variant="outline" onClick={onStartQr}>
                  {qr ? '重新获取二维码' : '扫码登录'}
                </Button>
                <Button type="button" variant="outline" onClick={onRefreshCookie}>
                  检查并续期
                </Button>
              </div>
              {qr && qrStatus !== 'confirmed' && (
                <div className="flex flex-col items-center gap-2 self-start rounded-md border p-3">
                  <QRCodeSVG value={qr.qr_url} size={180} />
                  <div className="text-xs text-muted-foreground">
                    {qrStatus === 'scanned'
                      ? '已扫码，请在手机上确认登录'
                      : qrStatus === 'expired'
                        ? '二维码已过期，请点击「重新获取二维码」'
                        : '请用哔哩哔哩 App 扫码'}
                  </div>
                </div>
              )}
            </div>
          )}

          {isWechatChannels && (
            <div className="flex flex-col gap-2 rounded-md border p-3">
              <div className="text-sm font-semibold">腾讯元宝扫码登录（视频号解析必需）</div>
              <div className="text-xs text-muted-foreground">
                微信视频号不提供匿名播放地址，官方开源方案都依赖腾讯元宝的登录态。
                点击下方按钮会在 Worker 本机打开一个浏览器窗口，用微信扫码登录元宝即可；
                登录态只保存在本机 config/downloader.json，不会上传。
                <br />
                当前状态：
                <span className={yb?.has_cookie ? 'text-green-600' : 'text-amber-600'}>
                  {yb?.has_cookie ? '已登录元宝' : '未登录'}
                </span>
                {yb?.message ? `（${yb.message}）` : ''}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={onStartYuanbao}
                  disabled={yb?.status === 'running'}
                >
                  {yb?.status === 'running' ? '等待扫码…' : yb?.has_cookie ? '重新扫码登录' : '扫码登录元宝'}
                </Button>
                {yb?.status === 'running' && (
                  <Button type="button" variant="outline" onClick={onCancelYuanbao}>
                    取消
                  </Button>
                )}
                {yb?.has_cookie && (
                  <Button type="button" variant="outline" onClick={onClearYuanbao}>
                    清除登录态
                  </Button>
                )}
              </div>
              <div className="text-xs text-muted-foreground">
                扫码登录需要在 Worker 本机操作；远端手机端可让本机先登录，之后直接解析。
              </div>
            </div>
          )}

          <FormField
            control={form.control}
            name="cookie"
            render={({ field }) => (
              <FormItem className="flex flex-col gap-2">
                <FormLabel>
                  Cookie{isWechatChannels ? '（扫码失败时可手动粘贴元宝 Cookie）' : ''}
                </FormLabel>
                <FormControl>
                  {isYoutube ? (
                    <Textarea
                      {...field}
                      rows={8}
                      className="font-mono text-xs"
                      placeholder={
                        '粘贴 youtube.com 的登录 Cookie。\n' +
                        '推荐：浏览器装 “Get cookies.txt LOCALLY” 扩展导出 cookies.txt，整段粘贴到此处；' +
                        '也可以仅粘贴一行 Cookie 串（k=v; k=v; …）。\n' +
                        '年龄限制视频（提示 Sign in to confirm your age）必须填写。'
                      }
                    />
                  ) : isWechatChannels ? (
                    <Textarea
                      {...field}
                      rows={6}
                      className="font-mono text-xs"
                      placeholder="粘贴 yuanbao.tencent.com 的完整 Cookie（需包含 hy_token）"
                    />
                  ) : (
                    <Input {...field} placeholder="输入 Cookie（也可粘贴完整 Cookie 串）" />
                  )}
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          <Button type="submit">保存</Button>
        </form>
      </Form>
    </div>
  )
}

export default DownloaderForm
