import { ConfigProvider, theme } from 'antd'
import { useTheme } from 'next-themes'
import { useEffect, useState, type ReactNode } from 'react'
// 【原型】亮色配色评审（2026-10-11）：从 ?variant= 读 antd token 覆盖；评审后随原型目录一起回退
import { getVariantFromUrl } from '@/prototype/lightMode/variants'

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
  // 【原型】亮色配色评审：跟随 ?variant= 给 antd 换底色 token
  const [, bump] = useState(0)
  useEffect(() => {
    const onChange = () => bump((n) => n + 1)
    window.addEventListener('lm-variant-change', onChange)
    return () => window.removeEventListener('lm-variant-change', onChange)
  }, [])
  const lmToken = !isDark ? getVariantFromUrl().antdToken : undefined
  return (
    <ConfigProvider
      theme={{
        algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: lmToken,
      }}
    >
      {children}
    </ConfigProvider>
  )
}
