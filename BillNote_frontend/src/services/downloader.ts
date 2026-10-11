import request from '@/utils/request.ts'

export const getDownloaderCookie = async id => {
  return await request.get('/get_downloader_cookie/' + id)
}

export const updateDownloaderCookie = async (data: { cookie: string; platform: any }) => {
  return await request.post('/update_downloader_cookie', data)
}

// ---- B 站扫码登录 / Cookie 自动续期（2026-10-11）----

export const getBilibiliAuthStatus = async () => {
  return await request.get('/bilibili/auth_status')
}

export const startBilibiliQrLogin = async () => {
  return await request.post('/bilibili/qr/start')
}

export const pollBilibiliQrLogin = async (qrcode_key: string) => {
  // suppressToast：轮询失败是常态（网络抖动/过期），不能让拦截器每 2 秒弹一个红 toast
  return await request.post('/bilibili/qr/poll', { qrcode_key }, { suppressToast: true })
}

export const refreshBilibiliCookie = async () => {
  return await request.post('/bilibili/cookie/refresh')
}
