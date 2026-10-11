import { ConfigProvider, theme } from 'antd'
import { useTheme } from 'next-themes'
import type { ReactNode } from 'react'

/** 亮色暖灰 token（2026-10-11 亮色配色定版，用户四方案评审选定「奶咖低饱和」）：
 *  antd / @ant-design/x 组件不读项目的 CSS 变量，必须在这里对齐底色与主色，
 *  否则 Bubble/Sender/Select 等仍是冷白底，与整体暖灰主题割裂。
 *  colorBgBase 会派生 antd 整套中性色阶（容器/悬浮/布局底），只覆盖这三个 + 主色即可。 */
const LIGHT_TOKENS = {
  colorBgBase: '#f6f4f0',
  colorBgLayout: '#efede8',
  colorBorderSecondary: '#e0ddd5',
  colorPrimary: '#476a9c',
  colorLink: '#476a9c',
}

/**
 * 让 antd / @ant-design/x 组件跟随项目的明暗主题。
 *
 * AI 问答用到的 Bubble / Sender（@ant-design/x）按 antd token 上色，
 * 默认永远是晴色算法——深色模式下出现大块白底浅字，与整体主题割裂
 * （2026-10-03 用户反馈）。antd 组件不读项目的 CSS 变量，唯一的正确
 * 姿势是在根上按 next-themes 的 resolvedTheme 切换 darkAlgorithm。
 */
export default function AntdThemeProvider({ children }: { children: ReactNode }) {
  const { resolvedTheme } = useTheme()
  const isDark = resolvedTheme === 'dark'
  return (
    <ConfigProvider
      theme={{
        algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: isDark ? undefined : LIGHT_TOKENS,
      }}
    >
      {children}
    </ConfigProvider>
  )
}
