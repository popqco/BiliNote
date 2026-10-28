/* -------------------- 常量 -------------------- */
import {
  BiliBiliLogo,
  DouyinLogo,
  KuaishouLogo,
  LocalLogo,
  WechatArticleLogo,
  WechatChannelsLogo,
  XiaohongshuLogo,
  YoutubeLogo,
} from '@/components/Icons/platform.tsx'

export const noteFormats = [
  { label: '目录', value: 'toc' },
  { label: '原片跳转', value: 'link' },
  { label: '原片截图', value: 'screenshot' },
  { label: 'AI总结', value: 'summary' },
] as const

/** 原片截图密度：数值是「平均每多少秒最多 1 张」，与后端 screenshot_interval 对应。
 *  global = 跟随「设置 → 通用」的截图平均间隔（表单默认，避免默认 normal 覆盖全局值）。 */
export const screenshotDensities = [
  { label: '跟随全局设置（设置→通用）', short: '跟随全局', value: 'global', seconds: 0 },
  { label: '精简（约 3 分钟 1 张）', short: '精简', value: 'sparse', seconds: 180 },
  { label: '标准（约 1.5 分钟 1 张）', short: '标准', value: 'normal', seconds: 90 },
  { label: '丰富（约 45 秒 1 张）', short: '丰富', value: 'rich', seconds: 45 },
] as const

export const noteStyles = [
  { label: '精简', value: 'minimal' },
  { label: '详细', value: 'detailed' },
  { label: '教程', value: 'tutorial' },
  { label: '学术', value: 'academic' },
  { label: '小红书', value: 'xiaohongshu' },
  { label: '生活向', value: 'life_journal' },
  { label: '任务导向', value: 'task_oriented' },
  { label: '商业风格', value: 'business' },
  { label: '会议纪要', value: 'meeting_minutes' },
] as const

export const videoPlatforms = [
  { label: '哔哩哔哩', value: 'bilibili', logo: BiliBiliLogo },
  { label: 'YouTube', value: 'youtube', logo: YoutubeLogo },
  { label: '抖音', value: 'douyin', logo: DouyinLogo },
  { label: '快手', value: 'kuaishou', logo: KuaishouLogo },
  { label: '小红书', value: 'xiaohongshu', logo: XiaohongshuLogo },
  { label: '公众号', value: 'wechat_article', logo: WechatArticleLogo },
  { label: '视频号', value: 'wechat_channels', logo: WechatChannelsLogo },
  { label: '本地视频', value: 'local', logo: LocalLogo },
] as const
