import request from '@/utils/request'
import toast from 'react-hot-toast'

export const generateNote = async (data: {
  video_url: string
  platform: string
  quality: string
  model_name: string
  provider_id: string
  task_id?: string
  format: Array<string>
  style: string
  extras?: string
  video_understand?: boolean
  video_interval?: number
  grid_size: Array<number>
}) => {
  try {
    console.log('generateNote', data)
    const response = await request.post('/generate_note', data)

    if (!response) {
      if (response.data.msg) {
        toast.error(response.data.msg)
      }
      return null
    }
    toast.success('笔记生成任务已提交！')

    console.log('res', response)
    // 成功提示

    return response
  } catch (e: any) {
    console.error('❌ 请求出错', e)

    // 错误提示
    // toast.error('笔记生成失败，请稍后重试')

    throw e // 抛出错误以便调用方处理
  }
}

export const delete_task = async ({ video_id, platform }) => {
  try {
    const data = {
      video_id,
      platform,
    }
    const res = await request.post('/delete_task', data)


      toast.success('任务已成功删除')
      return res
  } catch (e) {
    toast.error('请求异常，删除任务失败')
    console.error('❌ 删除任务失败:', e)
    throw e
  }
}

export const get_task_status = async (task_id: string) => {
  // 轮询专用：抑制全局 toast，失败语义由轮询层统一消化
  // （区分「后端明确失败（有错误信息）」与「网络抖动（保留状态继续轮询）」）
  return await request.get('/task_status/' + task_id, { suppressToast: true })
}

// 快速取视频标题/封面（不下载）：提交后立刻调用，让任务卡片第一时间可辨识，
// 不必等任务成功/失败。失败静默返回 null（不影响主流程）。
export const get_video_meta = async (url: string, platform = 'bilibili') => {
  try {
    return await request.get('/video_meta', {
      params: { url, platform },
      suppressToast: true,
      timeout: 15000,
    })
  } catch {
    return null
  }
}

// 后端最近任务列表：生成历史增量同步（自动化任务可见性 / 重启后状态恢复）
export const get_recent_tasks = async (limit = 80) => {
  try {
    return await request.get('/tasks/recent', {
      params: { limit },
      suppressToast: true,
    })
  } catch {
    return null
  }
}
