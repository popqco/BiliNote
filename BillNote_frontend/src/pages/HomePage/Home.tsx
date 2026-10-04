import { FC, useEffect, useRef, useState } from 'react'
import HomeLayout from '@/layouts/HomeLayout.tsx'
import MobileLayout from '@/layouts/MobileLayout.tsx'
import NoteForm from '@/pages/HomePage/components/NoteForm.tsx'
import MarkdownViewer from '@/pages/HomePage/components/MarkdownViewer.tsx'
import { useTaskStore } from '@/store/taskStore'
import { useIsMobile } from '@/hooks/useIsMobile.ts'
import History from '@/pages/HomePage/components/History.tsx'
import { get_task_status } from '@/services/note.ts'
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

  // 历史全量回填的任务只有概要（无正文）：点开后这里补拉一次结果。
  // 回填不批量拉正文（几十篇一次灌进 IndexedDB 太重），按需取这一篇。
  const lazyLoadingRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (!currentTask || currentTask.status !== 'SUCCESS') return
    const md: any = currentTask.markdown
    if (typeof md === 'string' ? md.trim() : md?.[0]?.content) return
    const id = currentTask.id
    if (lazyLoadingRef.current.has(id)) return
    lazyLoadingRef.current.add(id)
    ;(async () => {
      try {
        const res: any = await get_task_status(id)
        if (!res?.result) return
        const { markdown, transcript, audio_meta } = res.result
        const patch: any = {
          status: 'SUCCESS',
          markdown,
          transcript,
          audioMeta: audio_meta,
          message: undefined,
        }
        // 结果文件里的生成参数快照（model_name/provider_id/style）合进
        // formData 缺失的键：徽标与 NoteForm 回显的数据源
        const fd = useTaskStore.getState().tasks.find(t => t.id === id)?.formData || {}
        const merged: any = { ...fd }
        let touched = false
        for (const k of ['model_name', 'provider_id', 'style', 'quality', 'video_url', 'platform'] as const) {
          if (!merged[k] && res.result[k]) {
            merged[k] = res.result[k]
            touched = true
          }
        }
        if (touched) patch.formData = merged
        useTaskStore.getState().updateTaskContent(id, patch)
      } catch {
        // 概要仍在；重新点开这条笔记会再次尝试
      } finally {
        lazyLoadingRef.current.delete(id)
      }
    })()
  }, [currentTask])

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
