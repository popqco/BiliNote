import { FC, useEffect, useRef, useState } from 'react'
import HomeLayout from '@/layouts/HomeLayout.tsx'
import MobileLayout from '@/layouts/MobileLayout.tsx'
import NoteForm from '@/pages/HomePage/components/NoteForm.tsx'
import MarkdownViewer from '@/pages/HomePage/components/MarkdownViewer.tsx'
import { useTaskStore, mergeAudioMeta } from '@/store/taskStore'
import { useIsMobile } from '@/hooks/useIsMobile.ts'
import History from '@/pages/HomePage/components/History.tsx'
import ErrorBoundary from '@/components/ErrorBoundary.tsx'
import { get_task_status } from '@/services/note.ts'
type ViewStatus = 'idle' | 'loading' | 'success' | 'failed' | 'stopped'
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
    } else if (currentTask.status === 'FAILED' || currentTask.status === 'FAILD') {
      setStatus('failed')
    } else if (currentTask.status === 'CANCELLED') {
      // 用户主动停止：终态但不是失败——独立视图（「已停止」+ 重试），
      // 不能落 loading，否则停止后界面一直转圈
      setStatus('stopped')
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
        const cur = useTaskStore.getState().tasks.find(t => t.id === id)
        const patch: any = {
          status: 'SUCCESS',
          markdown,
          transcript,
          // 空字段不覆盖本地（老结果文件缺封面/标题，整包替换会把卡片元数据清掉）
          audioMeta: mergeAudioMeta(cur?.audioMeta, audio_meta),
          message: undefined,
        }
        // 结果文件里的生成参数快照（model_name/provider_id/style）合进
        // formData 缺失的键：徽标与 NoteForm 回显的数据源
        const fd = cur?.formData || {}
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
  // 笔记预览区错误边界（2026-10-17 白屏修复）：单篇笔记/某个版本渲染崩溃
  // 时只接管预览区，历史列表/新建表单仍在，用户可直接换一篇继续用；
  // resetKey=当前笔记 id，换笔记自动清错误状态。
  const previewPane = (
    <ErrorBoundary variant="inline" resetKey={currentTaskId || ''}>
      <MarkdownViewer status={status} />
    </ErrorBoundary>
  )
  if (isMobile) {
    return (
      <MobileLayout
        NewForm={<NoteForm />}
        HistoryList={<History />}
        NoteView={previewPane}
      />
    )
  }
  return (
    <HomeLayout
      NoteForm={<NoteForm />}
      Preview={previewPane}
      History={<History />}
    />
  )
}
