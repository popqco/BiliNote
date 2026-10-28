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
  video_understanding?: boolean
  video_interval?: number
  grid_size: Array<number>
  /** 原片截图密度：global 跟随全局 / sparse 精简 / normal 标准 / rich 丰富 */
  screenshot_density?: 'global' | 'sparse' | 'normal' | 'rich'
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

/**
 * 删除任务（后端真删：状态文件 + 缓存 + 导出笔记 + 向量索引）。
 *
 * 只传 task_id 就够；video_id/platform 是给老版本后端兜底的。
 * 失败时不再自己弹 toast——由调用方（store.removeTask）决定文案并把卡片放回去，
 * 避免出现「请求异常，删除任务失败」+「服务器错误，请稍后再试」两条叠加。
 */
export const delete_task = async (payload: {
  task_id?: string
  video_id?: string
  platform?: string
  force?: boolean
}) => {
  return await request.post('/delete_task', payload, { suppressToast: true })
}

export const get_task_status = async (
  task_id: string,
  options?: { view?: 'full' | 'note'; signal?: AbortSignal },
) => {
  // 轮询/阅读专用：抑制全局 toast，失败语义由调用层统一消化
  // （区分「后端明确失败（有错误信息）」与「网络抖动（保留状态继续轮询）」）。
  // view=note 只取正文+轻量元信息，不含 transcript / audio_meta.raw_info；
  // 默认 full 保持 MCP/导出/旧客户端行为。
  return await request.get('/task_status/' + task_id, {
    params: options?.view ? { view: options.view } : undefined,
    signal: options?.signal,
    suppressToast: true,
  })
}

/** 打开「原文」面板时按需取字幕：阅读首屏不再携带 transcript。 */
export const get_task_transcript = async (task_id: string) => {
  return await request.get('/task_transcript/' + task_id, { suppressToast: true })
}

/** 同一视频的历史笔记版本（生成中也能取到上一版）。 */
export const get_task_versions = async (task_id: string) => {
  return await request.get('/task_versions/' + task_id, { suppressToast: true })
}

/**
 * 停止正在生成（或排队中）的任务。
 *
 * 模型/参数设置错了时不必干等整条流程跑完（含转写重试、总结降级阶梯这类
 * 「一直不停地尝试」）：停止后状态落 CANCELLED（已停止），改完设置可直接
 * 重新生成。suppressToast：提示文案由调用方（store.cancelTask）决定。
 */
export const cancel_task = async (task_id: string) => {
  return await request.post('/cancel_task', { task_id }, { suppressToast: true })
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
// version 是后端状态文件集合的指纹；带上一次的值，未变化时后端只回
// { unchanged: true }（几百字节），不再每 30s 传整包列表。
let recentVersion: string | null = null

export const get_recent_tasks = async (
  limit = 80,
  options?: { force?: boolean },
) => {
  try {
    const res: any = await request.get('/tasks/recent', {
      params: {
        limit,
        ...(options?.force || !recentVersion ? {} : { since: recentVersion }),
      },
      suppressToast: true,
    })
    if (res?.version) recentVersion = res.version
    return res
  } catch {
    return null
  }
}

/** 重新配对/切 Worker 后指纹失效，下一次同步强制拉整包。 */
export const resetRecentVersion = () => {
  recentVersion = null
}
