import request from '@/utils/request'

/** 生成性能与预算配置（设置→通用「生成性能与预算」）。
 *
 * 原片截图已完全由后端自适应编排决定（2026-10-28 定版），不再有间隔/上限/
 * 模式配置项。
 */

export interface TaskConfig {
  max_workers: number
  frame_budget: number
  image_budget: number
  summarize_batch_concurrency: number
}

export const get_task_config = async (): Promise<TaskConfig> => {
  return await request.get('/task_config')
}

export const save_task_config = async (config: Partial<TaskConfig>): Promise<TaskConfig> => {
  return await request.post('/task_config', { config })
}
