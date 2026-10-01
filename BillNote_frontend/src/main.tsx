import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ThemeProvider } from 'next-themes'
import './index.css'
import App from './App.tsx'
import RootLayout from './layouts/RootLayout.tsx'
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* 夜间模式：默认跟随系统，用户可在「设置 → 外观」或首页快捷按钮切换；
        存储键与 index.html 的防白闪脚本保持一致 */}
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem storageKey="bilinote-theme">
      <RootLayout>
        <App />
      </RootLayout>
    </ThemeProvider>
  </StrictMode>
)
