import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, Copy, RotateCcw } from 'lucide-react'
import { Button } from '@/components/ui/button.tsx'

/**
 * 前端异常留痕 + 错误边界（2026-10-17 用户反馈「页面时不时莫名全白」的修复层之一）。
 *
 * 白屏有两类根因，本文件负责其中「JS 崩溃」一类：
 *   1. React 渲染/生命周期里抛出未捕获异常 → 没有 boundary 时 React 会卸载
 *      整棵树，页面瞬间全白且无法操作。ErrorBoundary 接住并给出可操作的恢复 UI；
 *   2. window 级监听兜住事件处理器 / 异步 Promise 中的异常（这两类不触发
 *      boundary，但常常是崩溃的前兆）。
 * 另一类根因（WebView2 渲染进程被 OOM 杀掉）JS 侧救不了，由 Rust 侧
 * ProcessFailed 监听自动重载（见 src-tauri/src/lib.rs）。
 */

declare global {
  interface Window {
    __bilinoteErrors?: string[]
  }
}

const MAX_RECENT = 20

/** 记录一条前端异常（环形缓冲，最多 20 条，最新在前）。 */
export function recordFrontendError(kind: string, detail: string): void {
  const line = `[${new Date().toISOString()}] ${kind}: ${detail}`
  try {
    const list = (window.__bilinoteErrors ||= [])
    list.unshift(line)
    if (list.length > MAX_RECENT) list.length = MAX_RECENT
  } catch {
    /* 忽略 */
  }
  console.error(`[bilinote] ${kind}:`, detail)
}

export function installGlobalErrorCapture(): void {
  if (typeof window === 'undefined') return
  window.addEventListener('error', e => {
    const err = (e as ErrorEvent).error
    recordFrontendError('error', err?.stack || (e as ErrorEvent).message || String(e))
  })
  window.addEventListener('unhandledrejection', e => {
    const r = (e as PromiseRejectionEvent).reason
    recordFrontendError('unhandledrejection', r?.stack || r?.message || String(r))
  })
}

interface Props {
  children: ReactNode
  /** page：整页接管（根边界）；inline：只接管内容区，页面外壳仍可操作 */
  variant?: 'page' | 'inline'
  /** 变化时自动清除错误状态（配合路由 pathname：切走再切回不卡错误页） */
  resetKey?: string
}

interface State {
  error: Error | null
  componentStack: string
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: '' }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    recordFrontendError(
      'render',
      `${error?.stack || error?.message || String(error)}\n${info?.componentStack || ''}`,
    )
    this.setState({ componentStack: info?.componentStack || '' })
  }

  componentDidUpdate(prev: Props): void {
    // 路由 / 选中对象变化就把错误状态清掉，让用户能"换个笔记再看"
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null, componentStack: '' })
    }
  }

  private handleCopy = async (): Promise<void> => {
    const lines = [
      `message: ${this.state.error?.message || ''}`,
      `stack:\n${this.state.error?.stack || ''}`,
      `componentStack:\n${this.state.componentStack}`,
      ...(window.__bilinoteErrors || []).map(x => `recent: ${x}`),
    ]
    try {
      await navigator.clipboard.writeText(lines.join('\n'))
    } catch {
      /* 剪贴板不可用时静默 */
    }
  }

  render(): ReactNode {
    const { error, componentStack } = this.state
    if (!error) return this.props.children
    const inline = this.props.variant === 'inline'

    return (
      <div
        className={
          inline
            ? 'flex h-full w-full items-center justify-center p-6'
            : 'bg-background text-foreground flex min-h-screen w-full items-center justify-center p-6'
        }
      >
        <div className="border-border bg-card w-full max-w-xl rounded-xl border p-6 shadow-sm">
          <div className="mb-3 flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-amber-500" />
            <p className="text-base font-bold">界面出错了</p>
          </div>
          <p className="text-muted-foreground mb-4 text-sm leading-6">
            已捕获异常，界面不会整页空白。笔记与生成任务都存在后端，重新加载不会丢失；
            如果反复出现，请把下面的错误详情复制反馈。
          </p>
          <div className="mb-4 flex gap-2">
            <Button onClick={() => window.location.reload()}>
              <RotateCcw className="mr-2 h-4 w-4" />
              重新加载
            </Button>
            <Button variant="outline" onClick={this.handleCopy}>
              <Copy className="mr-2 h-4 w-4" />
              复制错误详情
            </Button>
          </div>
          <details className="text-muted-foreground text-xs">
            <summary className="cursor-pointer select-none">错误详情</summary>
            <pre className="bg-muted mt-2 max-h-56 overflow-auto rounded-md p-3 whitespace-pre-wrap break-all">
              {String(error?.message || error)}
              {error?.stack ? `\n\n${error.stack}` : ''}
              {componentStack ? `\n\n组件栈:${componentStack}` : ''}
            </pre>
          </details>
        </div>
      </div>
    )
  }
}
