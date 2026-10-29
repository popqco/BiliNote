import request from '@/utils/request'

/** 自动化（稍后再看定期检查 + 通知）配置读取/保存与操作触发。 */

export const get_automation_config = async () => {
  return await request.get('/automation_config')
}

export const save_automation_config = async (config: any) => {
  return await request.post('/automation_config', { config })
}

/** 向已启用渠道发送测试通知，返回每渠道结果 [{channel, ok, detail}] */
export const test_notify = async () => {
  return await request.post('/automation/test_notify', {})
}

/** 手动触发一轮检查（后台执行，完成后发汇总通知） */
export const run_automation_now = async () => {
  return await request.post('/automation/run_now', {})
}

/** 当前状态：是否在跑 / 阶段 / 本轮提交与跳过明细 / 失败原因 */
export const get_automation_status = async () => {
  return await request.get('/automation/status')
}

/** 校验 B 站 Cookie（拉一次稍后再看）。失败时 reject，err.msg 即失败原因 */
export const check_automation_login = async () => {
  return await request.get('/automation/check_login', { suppressToast: true })
}

/** 已忽略的视频（被手动删除过、因而不再自动生成的视频） */
export const get_automation_ignore = async (): Promise<{
  items: Array<{
    key: string
    video_id: string
    platform: string
    title: string
    task_id: string
    ts: number
    reason: string
  }>
  count: number
}> => {
  return await request.get('/automation_ignore')
}

/** 从忽略名单移除：下一次检查轮会重新考虑该视频 */
export const remove_automation_ignore = async (video_id: string, platform = 'bilibili') => {
  return await request.post('/automation_ignore/remove', { video_id, platform })
}

export const clear_automation_ignore = async () => {
  return await request.post('/automation_ignore/clear', {})
}
