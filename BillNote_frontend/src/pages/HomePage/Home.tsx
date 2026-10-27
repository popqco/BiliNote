import { FC, Suspense, lazy, useEffect, useState } from 'react'
import HomeLayout from '@/layouts/HomeLayout.tsx'
import MobileLayout from '@/layouts/MobileLayout.tsx'
import NoteForm from '@/pages/HomePage/components/NoteForm.tsx'
import { useTaskStore, ACTIVE_STATUSES } from '@/store/taskStore'
import { useIsMobile } from '@/hooks/useIsMobile.ts'
import History from '@/pages/HomePage/components/History.tsx'
import ErrorBoundary from '@/components/ErrorBoundary.tsx'

// 首屏不预载 markdown/markmap 两个大分包（gzip 合计约 600KB）：
// 远端手机先拿到表单/历史，真正打开笔记时再按需下载阅读器。
const MarkdownViewer = lazy(() => import('@/pages/HomePage/components/MarkdownViewer.tsx'))

type ViewStatus = 'idle' | 'loading' | 'success' | 'failed' | 'stopped'
export const HomePage: FC = () => {
  const tasks = useTaskStore(state => state.tasks)
  const currentTaskId = useTaskStore(state => state.currentTaskId)

  const currentTask = tasks.find(t => t.id === currentTaskId)

  const [status, setStatus] = useState<ViewStatus>('idle')
  const ensureTaskContent = useTaskStore(state => state.ensureTaskContent)

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

  // 历史概要卡只有标题/状态，没有正文：进入当前笔记时确保正文就位。
  // 有正文（含刷新后从其它路径写入的缓存）直接显示；没有才拉轻量视图。
  // contentStale 表示后端结果被外部重写过，本次打开时静默换成新内容。
  useEffect(() => {
    if (!currentTask) return
    const md: any = currentTask.markdown
    const hasContent = typeof md === 'string' ? !!md.trim() : !!md?.[0]?.content
    // 生成中也要尝试加载历史版本：后端在覆盖结果前保留旧 <task_id>.json，
    // /task_versions 能取到上一版；这样新版本跑着时仍可继续阅读旧笔记。
    if (ACTIVE_STATUSES.includes(currentTask.status)) {
      if (!hasContent && !currentTask.contentLoading) {
        void ensureTaskContent(currentTask.id)
      }
      return
    }
    // 失败/停止也可能留着上一版成功结果：打开时拉一次版本列表，
    // 让用户还能读历史版本（没有版本则保持原来的终态页）。
    if (currentTask.status !== 'SUCCESS') {
      if (!hasContent && !currentTask.contentLoading) {
        void ensureTaskContent(currentTask.id)
      }
      return
    }
    if (hasContent && !currentTask.contentStale) return
    void ensureTaskContent(currentTask.id)
  }, [currentTaskId, currentTask?.status, currentTask?.contentStale, ensureTaskContent])

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
      <Suspense
        fallback={
          <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
            正在加载笔记阅读器…
          </div>
        }
      >
        <MarkdownViewer status={status} />
      </Suspense>
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
