import { ConfigProvider, theme } from 'antd'
import { useTheme } from 'next-themes'
import type { ReactNode } from 'react'

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
      }}
    >
      {children}
    </ConfigProvider>
  )
}
