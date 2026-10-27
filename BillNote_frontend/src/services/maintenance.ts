import request from '@/utils/request'

/** 存量笔记原片截图体检 / 瘦身（设置→通用「原片截图维护」）。 */

export interface ScreenshotOversized {
  task_id: string
  title: string
  images: number
  cap: number
  chars: number
}

export interface ScreenshotStats {
  notes: number
  notes_with_shots: number
  images: number
  over_limit: number
  interval: number
  max_count: number
  oversized: ScreenshotOversized[]
  estimated_drop: number
  estimated_keep_after: number
  disk: { files: number; bytes: number }
}

export interface SlimDetail {
  task_id: string
  title: string
  before: number
  after: number
  dropped: number
}

export interface SlimResult {
  dry_run: boolean
  interval: number
  max_count: number
  notes_scanned: number
  notes_changed: number
  images_before: number
  images_after: number
  skipped_active: number
  details: SlimDetail[]
}

export const get_screenshot_stats = async (): Promise<ScreenshotStats> =>
  await request.get('/maintenance/screenshot_stats', { timeout: 120000 })

export const slim_screenshots = async (payload: {
  dry_run: boolean
  min_images?: number
  task_ids?: string[]
  interval?: number
  max_count?: number
}): Promise<SlimResult> =>
  await request.post('/maintenance/screenshot_slim', payload, { timeout: 600000 })
