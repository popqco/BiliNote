import { FC, useEffect, useState } from 'react'
import HomeLayout from '@/layouts/HomeLayout.tsx'
import MobileLayout from '@/layouts/MobileLayout.tsx'
import NoteForm from '@/pages/HomePage/components/NoteForm.tsx'
import MarkdownViewer from '@/pages/HomePage/components/MarkdownViewer.tsx'
import { useTaskStore } from '@/store/taskStore'
import { useIsMobile } from '@/hooks/useIsMobile.ts'
import History from '@/pages/HomePage/components/History.tsx'
type ViewStatus = 'idle' | 'loading' | 'success' | 'failed'
export const HomePage: FC = () => {
  const tasks = useTaskStore(state => state.tasks)
  const currentTaskId = useTaskStore(state => state.currentTaskId)

  const currentTask = tasks.find(t => t.id === currentTaskId)

  const [status, setStatus] = useState<ViewStatus>('idle')

  const content = currentTask?.markdown || ''

  // 剪贴板监听已移到全局 <GlobalClipboardWatcher />（App 内 Router 下挂载），
  // 切到设置页时轮询不中断；这里不再重复挂载，避免双跑弹两次。

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
  // 手机窄视口走单列 Tab 布局（新建/历史/笔记/设置），桌面端保持三栏可拖拽。
  // 注意 MarkdownViewer 在 master 上只认 status（内容自己从 store 取），
  // 不要传 mobile-split 旧版的 content prop（类型已删，传了 tsc 报错）。
  const isMobile = useIsMobile()
  if (isMobile) {
    return (
      <MobileLayout
        NewForm={<NoteForm />}
        HistoryList={<History />}
        NoteView={<MarkdownViewer status={status} />}
      />
    )
  }
  return (
    <HomeLayout
      NoteForm={<NoteForm />}
      Preview={<MarkdownViewer status={status} />}
      History={<History />}
    />
  )
}
