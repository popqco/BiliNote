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
