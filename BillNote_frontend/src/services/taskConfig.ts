import request from '@/utils/request'

/** 生成性能与预算配置（设置→通用「生成性能与预算」）。 */

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
