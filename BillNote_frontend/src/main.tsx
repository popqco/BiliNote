import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ThemeProvider } from 'next-themes'
import AntdThemeProvider from '@/components/AntdThemeProvider.tsx'
import './index.css'
import App from './App.tsx'
import RootLayout from './layouts/RootLayout.tsx'
import ErrorBoundary, { installGlobalErrorCapture } from '@/components/ErrorBoundary.tsx'

// 全局异常留痕：事件处理器 / 异步 Promise 里的异常不会触发 ErrorBoundary，
// 但往往是崩溃前兆（2026-10-17 白屏排查需要）。渲染期崩溃由下面的边界兜。
installGlobalErrorCapture()

// React 接管前移除 index.html 里的启动占位（避免和首屏 UI 重叠）
document.getElementById('bn-boot')?.remove()

createRoot(document.getElementById('root')!).render(
  <ErrorBoundary>
    <StrictMode>
      {/* 夜间模式：默认跟随系统，用户可在「设置 → 外观」或首页快捷按钮切换；
          存储键与 index.html 的防白闪脚本保持一致。
          AntdThemeProvider：antd/@ant-design/x 组件（AI 问答的 Bubble/Sender）
          不读 CSS 变量，必须用 antd darkAlgorithm 才能跟随明暗主题 */}
      <ThemeProvider attribute="class" defaultTheme="system" enableSystem storageKey="bilinote-theme">
        <AntdThemeProvider>
          <RootLayout>
            <App />
          </RootLayout>
        </AntdThemeProvider>
      </ThemeProvider>
    </StrictMode>
  </ErrorBoundary>
)
