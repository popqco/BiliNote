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

  const [loading, setLoading] = useState(true)
  const [authStatus, setAuthStatus] = useState<any>(null)
  const [qr, setQr] = useState<{ qr_url: string; qrcode_key: string } | null>(null)
  const [qrStatus, setQrStatus] = useState('')

  const loadAuthStatus = async () => {
    if (!isBilibili) return
    try {
      setAuthStatus(await getBilibiliAuthStatus())
    } catch {
      // 静默：登录态查询失败不影响表单使用
    }
  }

  useEffect(() => {
    const loadCookie = async () => {
      setLoading(true) // 🔁 切换平台时显示 loading
      try {
        const res = await getDownloaderCookie(id)
        const cookie = res?.cookie || ''
        form.reset({ cookie }) // ✅ 正确重置表单值
      } catch (e) {
        toast.error('加载 Cookie 失败: ' + e)
        form.reset({ cookie: '' }) // ❗失败时也要清空旧值
      } finally {
        setLoading(false)
      }
    }

    if (id) {
      loadCookie()
      loadAuthStatus()
      setQr(null)
      setQrStatus('')
    }
  }, [id]) // 🔁 每当 id 变化时触发

  // 扫码状态轮询：confirmed / expired 时停止
  useEffect(() => {
    if (!qr || qrStatus === 'confirmed' || qrStatus === 'expired') return
    const timer = setInterval(async () => {
      try {
        const res = await pollBilibiliQrLogin(qr.qrcode_key)
        const st = res?.status || 'waiting'
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
      const res = await refreshBilibiliCookie()
      toast.success(res?.msg || '检查完成')
      loadAuthStatus()
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
    } catch (e) {
      toast.error('保存失败')
    }
  }

  if (loading) return <div className="p-4">加载中...</div>

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

          <FormField
            control={form.control}
            name="cookie"
            render={({ field }) => (
              <FormItem className="flex flex-col gap-2">
                <FormLabel>Cookie</FormLabel>
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
