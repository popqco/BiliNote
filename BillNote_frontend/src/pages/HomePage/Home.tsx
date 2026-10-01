import { FC, useCallback, useEffect, useState } from 'react'
import HomeLayout from '@/layouts/HomeLayout.tsx'
import NoteForm from '@/pages/HomePage/components/NoteForm.tsx'
import MarkdownViewer from '@/pages/HomePage/components/MarkdownViewer.tsx'
import { useTaskStore } from '@/store/taskStore'
import History from '@/pages/HomePage/components/History.tsx'
import { useClipboardWatcher, notifyClipboardVideo } from '@/hooks/useClipboardWatcher.tsx'
type ViewStatus = 'idle' | 'loading' | 'success' | 'failed'
export const HomePage: FC = () => {
  const tasks = useTaskStore(state => state.tasks)
  const currentTaskId = useTaskStore(state => state.currentTaskId)

  const currentTask = tasks.find(t => t.id === currentTaskId)

  const [status, setStatus] = useState<ViewStatus>('idle')

  const content = currentTask?.markdown || ''

  // 剪贴板视频链接识别：发现新链接 → 右下角弹窗（含标题封面）→ 点「生成笔记」
  // 派发事件给 NoteForm 填入表单（只填不自动提交，模型/风格由用户确认）。
  const handleClipboardCandidate = useCallback((info: { url: string; platform: string }) => {
    window.dispatchEvent(new CustomEvent('bilinote:clipboard-video', { detail: info }))
  }, [])
  useClipboardWatcher(useCallback((info: {
    url: string
    platform: string
    title?: string
    cover_url?: string
  }) => {
    notifyClipboardVideo(info, handleClipboardCandidate)
  }, [handleClipboardCandidate]))

  useEffect(() => {
    if (!currentTask) {
      setStatus('idle')
    } else if (currentTask.status === 'SUCCESS') {
      setStatus('success')
    } else if (currentTask.status === 'FAILED') {
      setStatus('failed')
    } else {
      // PENDING、PARSING、DOWNLOADING、TRANSCRIBING、SUMMARIZING 等所有进行中状态
      setStatus('loading')
    }
  }, [currentTask, currentTask?.status])

  // useEffect( () => {
  //     get_task_status('d4e87938-c066-48a0-bbd5-9bec40d53354').then(res=>{
  //         console.log('res1',res)
  //         setContent(res.data.result.markdown)
  //     })
  // }, [tasks]);
  return (
    <HomeLayout
      NoteForm={<NoteForm />}
      Preview={<MarkdownViewer status={status} />}
      History={<History />}
    />
  )
}
