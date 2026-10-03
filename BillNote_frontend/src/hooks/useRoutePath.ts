import { useLocation } from 'react-router-dom'

/**
 * 统一路由路径：Tauri 桌面端走 HashRouter，location.pathname 永远是 '/'，
 * 真实路由藏在 window.location.hash（#/settings/…）里；Web 端（BrowserRouter）
 * pathname 即真实路由。之前各处直接读 location.pathname / window.location.pathname，
 * 在桌面端全部失效（设置菜单总览出不来、/model/new 表单进不去、菜单无高亮）。
 * 两端统一走这个 hook。
 */
export function useRoutePath(): string {
  const location = useLocation()
  if (location.pathname && location.pathname !== '/') {
    return location.pathname.replace(/\/+$/, '') || '/'
  }
  if (typeof window !== 'undefined' && window.location.hash.startsWith('#/')) {
    const hashPath = window.location.hash.replace(/^#/, '').split('?')[0]
    return hashPath.replace(/\/+$/, '') || '/'
  }
  return location.pathname || '/'
}
