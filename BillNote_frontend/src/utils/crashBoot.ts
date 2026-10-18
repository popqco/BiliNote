import toast from 'react-hot-toast'
import { useTaskStore } from '@/store/taskStore'

/**
 * 渲染进程崩溃自愈重载的落地点（2026-10-18 白屏根治配套）。
 *
 * Rust 侧 ProcessFailed 监听（src-tauri/src/lib.rs setup_webview_crash_recovery）
 * 在自动重载时会带上 `?bn_crash=1` 标记。前端启动读到标记后**不自动打开上次
 * 的笔记**——几百张 4K 截图的重量级笔记正是把渲染进程反复推爆的元凶，
 * 崩溃后重载如果又落在同一篇笔记上，就是「崩溃→重载→再崩」的死循环；
 * 停在列表页让用户自己决定再打开哪一篇（打开后内存已被 NoteImage 的
 * 离屏卸载压住，不会再滚爆）。
 *
 * 手动 Ctrl+R / 正常刷新不带这个标记，行为不变。
 */
export function handleCrashBoot() {
  if (typeof window === 'undefined') return
  let params: URLSearchParams
  try {
    params = new URLSearchParams(window.location.search)
  } catch {
    return
  }
  if (params.get('bn_crash') !== '1') return

  // 清掉标记：本次启动内路由跳转/再次刷新不再重复触发
  params.delete('bn_crash')
  try {
    const qs = params.toString()
    history.replaceState(
      null,
      '',
      window.location.pathname + (qs ? `?${qs}` : '') + window.location.hash,
    )
  } catch {
    /* 忽略 */
  }

  const apply = () => {
    useTaskStore.setState({ currentTaskId: null })
    window.setTimeout(() => {
      toast('页面上次异常重载，已返回列表以避免再次卡住', { icon: '⚠️', duration: 5000 })
    }, 900)
  }

  // persist 是异步水合（IndexedDB）：必须等水合完成再清，否则会被
  // 持久化的 currentTaskId 覆盖回去
  if (useTaskStore.persist.hasHydrated()) {
    apply()
  } else {
    const unsub = useTaskStore.persist.onFinishHydration(() => {
      unsub()
      apply()
    })
  }
}
