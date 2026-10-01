import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useClipboardWatcher, notifyClipboardVideo } from '@/hooks/useClipboardWatcher.tsx'

/**
 * 全局剪贴板监听挂载点：放在 Router 内部、Routes 之外，
 * 首页 / 设置页 / 关于页都存活，轮询不会因切到设置页而中断。
 *
 * 点「生成笔记」时若当前不在首页，先 navigate('/') 再派发事件给 NoteForm
 *（NoteForm 只在首页挂载，直接派发会丢）。延迟 250ms 等首页挂载，
 * NoteForm 内部还有 rAF 规避清空 effect 覆盖。
 */
export default function GlobalClipboardWatcher() {
  const navigate = useNavigate()

  const handleAccept = useCallback((info: { url: string; platform: string }) => {
    if (window.location.hash.includes('#/settings') || window.location.pathname.startsWith('/settings')) {
      navigate('/')
      window.setTimeout(() => {
        window.dispatchEvent(new CustomEvent('bilinote:clipboard-video', { detail: info }))
      }, 250)
    } else {
      window.dispatchEvent(new CustomEvent('bilinote:clipboard-video', { detail: info }))
    }
  }, [navigate])

  useClipboardWatcher(useCallback((info: {
    url: string
    platform: string
    title?: string
    cover_url?: string
  }) => {
    notifyClipboardVideo(info, handleAccept)
  }, [handleAccept]))

  return null
}
