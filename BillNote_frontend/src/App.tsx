import './App.css'
import { lazy, Suspense, useEffect } from 'react'
import { BrowserRouter, HashRouter, Navigate, Routes, Route, useLocation } from 'react-router-dom'
import { useTaskPolling } from '@/hooks/useTaskPolling.ts'
import { useTasksSync } from '@/hooks/useTasksSync.ts'
import { useCheckBackend } from '@/hooks/useCheckBackend.ts'
import { systemCheck } from '@/services/system.ts'
import BackendInitDialog from '@/components/BackendInitDialog'
import StartupBanner from '@/components/SystemDiagnostic/StartupBanner'
import BackendHealthIndicator from '@/components/BackendHealth/BackendHealthIndicator'
import ErrorBoundary from '@/components/ErrorBoundary.tsx'
import { handleCrashBoot } from '@/utils/crashBoot.ts'
import Index from '@/pages/Index.tsx'
import { HomePage } from './pages/HomePage/Home.tsx'

// 非首屏页面使用 React.lazy 按需加载
const Onboarding = lazy(() => import('@/pages/Onboarding'))
const SettingPage = lazy(() => import('./pages/SettingPage/index.tsx'))

// 桌面端首启引导守卫：未完成 onboarding 时强制跳到 /onboarding
function OnboardingGuard({ children }: { children: React.ReactNode }) {
  const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  // 仅在 Tauri 桌面端拦截；纯 web 端不打扰用户
  if (!isTauri) return <>{children}</>
  if (localStorage.getItem('bilinote-onboarded') !== '1') return <Navigate to="/onboarding" replace />
  return <>{children}</>
}
const Model = lazy(() => import('@/pages/SettingPage/Model.tsx'))
const ProviderForm = lazy(() => import('@/components/Form/modelForm/Form.tsx'))
const AboutPage = lazy(() => import('@/pages/SettingPage/about.tsx'))
const Monitor = lazy(() => import('@/pages/SettingPage/Monitor.tsx'))
const Downloader = lazy(() => import('@/pages/SettingPage/Downloader.tsx'))
const DownloaderForm = lazy(() => import('@/components/Form/DownloaderForm/Form.tsx'))
const TranscriberPage = lazy(() => import('@/pages/SettingPage/transcriber.tsx'))
const AppearancePage = lazy(() => import('@/pages/SettingPage/appearance.tsx'))
const AutomationPage = lazy(() => import('@/pages/SettingPage/automation.tsx'))
const GeneralPage = lazy(() => import('@/pages/SettingPage/general.tsx'))
const ConnectionPage = lazy(() => import('@/pages/SettingPage/connection.tsx'))
const NotFoundPage = lazy(() => import('@/pages/NotFoundPage'))
const GlobalClipboardWatcher = lazy(() => import('@/components/GlobalClipboardWatcher.tsx'))

// 内容区错误边界（2026-10-17 白屏修复）：某篇笔记/某个设置页渲染崩溃时
// 只接管内容区并给出「重新加载」入口，页面外壳（历史列表、侧边栏）仍在，
// 用户可以直接切到别的笔记；resetKey=pathname 让路由切换自动清错误状态。
function RouteErrorBoundary({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  return (
    <ErrorBoundary variant="inline" resetKey={location.pathname}>
      {children}
    </ErrorBoundary>
  )
}

function App() {
  const isDesktopApp = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  const { loading, initialized, failed, lastError, retry } = useCheckBackend()
  // 未就绪前不发任何任务请求：旧实现会在 /sys_check 还没通过时同时发
  // /tasks/recent?limit=300 和 ?limit=120，并顺序补拉最多 120 篇正文，
  // 和首屏 JS/健康探测抢同源连接，正是「刷很多次才连上」的主因之一。
  // 手机轮询放宽到 5s（Tailscale + 省电），桌面端保持 3s。
  useTaskPolling(isDesktopApp ? 3000 : 5000, initialized)
  // 与后端任务列表增量同步：桌面端 5 秒一次——外部（API / 自动化 / MCP 工具）
  // 触发的新任务、重跑与结果更新必须立刻反映到界面，不能等用户手动刷新；
  // 浏览器/手机 Viewer（经 Tailscale）保持 30 秒，省流量与电量。
  useTasksSync(isDesktopApp ? 5000 : 30000, initialized)

  // 渲染进程崩溃自愈重载的落地（Rust 侧重载时带 ?bn_crash=1）：不自动打开
  // 上一篇笔记，停在列表页——避免「重载→又落在重笔记→再崩」的死循环
  // （2026-10-18 白屏根治配套，见 utils/crashBoot.ts）
  useEffect(() => {
    handleCrashBoot()
  }, [])

  // 在后端初始化完成后执行系统检查
  useEffect(() => {
    if (initialized) {
      systemCheck()
    }
  }, [initialized])

  // 如果后端还未初始化，显示初始化对话框（loading 或 failed 都展示，由 dialog 内部决定渲染哪一态）
  if (!initialized) {
    return (
      <>
        <StartupBanner />
        <BackendInitDialog
          open={loading}
          failed={failed}
          lastError={lastError}
          onRetry={retry}
        />
      </>
    )
  }

  // 桌面端使用 HashRouter 避免刷新 404；Web 端继续使用 BrowserRouter
  const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  const Router = isTauri ? HashRouter : BrowserRouter

  // 后端已初始化，渲染主应用
  return (
    <>
      <StartupBanner />
      <BackendHealthIndicator />
      <Router>
        <RouteErrorBoundary>
          <Suspense fallback={<div className="flex h-screen items-center justify-center">加载中…</div>}>
            {/* 全局剪贴板监听：首页/设置页都存活，轮询不因切页中断 */}
            <GlobalClipboardWatcher />
            <Routes>
              <Route path="/onboarding" element={<Onboarding />} />
              <Route path="/" element={<OnboardingGuard><Index /></OnboardingGuard>}>
                <Route index element={<HomePage />} />
                <Route path="settings" element={<SettingPage />}>
                  {/* /settings 本体即菜单总览（SettingLayout 两级导航依赖它）：
                      index 绝不能 <Navigate> 到任何详情页——之前先跳 model 后改跳
                      connection，都导致手机端「连接 → 设置 → 连接 → …」死循环，
                      菜单总览永远停留不住。index 只放占位（手机端隐藏，桌面端提示选左侧）。 */}
                  <Route index element={<div className="text-muted-foreground p-8 text-sm">请从左侧选择设置项</div>} />
                  <Route path="model" element={<Model />}>
                    <Route path="new" element={<ProviderForm isCreate />} />
                    <Route path=":id" element={<ProviderForm />} />
                  </Route>
                  <Route path="download" element={<Downloader />}>
                    <Route path=":id" element={<DownloaderForm />} />
                  </Route>
                  <Route path="transcriber" element={<TranscriberPage />} />
                  <Route path="appearance" element={<AppearancePage />} />
                  <Route path="automation" element={<AutomationPage />} />
                  <Route path="general" element={<GeneralPage />} />
                  <Route path="connection" element={<ConnectionPage />} />
                  <Route path="monitor" element={<Monitor />}></Route>
                  <Route path="about" element={<AboutPage />}></Route>
                  <Route path="*" element={<NotFoundPage />} />
                </Route>
                <Route path="*" element={<NotFoundPage />} />
              </Route>
            </Routes>
          </Suspense>
        </RouteErrorBoundary>
      </Router>
    </>
  )
}

export default App
