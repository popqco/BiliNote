import { useState, useEffect, useCallback, useMemo } from 'react'
import { Bubble, Sender } from '@ant-design/x'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import { normalizeMathDelimiters } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Loader2, Trash2, ChevronDown, ChevronUp, BookOpen, UserRound, Bot, Maximize2, Minimize2 } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { chatKey, useChatJumpStore, useChatStore } from '@/store/chatStore'
import { useTaskStore } from '@/store/taskStore'
import { useModelStore } from '@/store/modelStore'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.tsx'
import {
  askQuestion,
  backfillGlobalIndex,
  getChatStatus,
  getIndexCoverage,
  indexTask,
  type ChatScope,
  type ChatSource,
  type IndexStatus,
} from '@/services/chat'

type ChatMode = 'half' | 'full'

interface ChatPanelProps {
  taskId: string
  mode: ChatMode
  onModeChange: (mode: ChatMode) => void
}

function SourceBadges({
  sources,
  /** 锚定“提问时”的笔记：标签的本篇/跨篇判定用它，不随浏览位置漂移 */
  anchorTaskId,
  /** 实时当前笔记：点击跳转时判断要不要先切笔记用它 */
  currentTaskId,
}: {
  sources: ChatSource[]
  anchorTaskId: string
  currentTaskId: string
}) {
  const [expanded, setExpanded] = useState(false)
  const setCurrentTask = useTaskStore(state => state.setCurrentTask)
  const requestJump = useChatJumpStore(state => state.requestJump)

  if (!sources || sources.length === 0) return null

  const detailOf = (s: ChatSource) =>
    s.source_type === 'markdown'
      ? s.section_title || '笔记'
      : s.source_type === 'meta'
        ? '视频信息'
        : `${(s.start_time ?? 0).toFixed(0)}s ~ ${(s.end_time ?? 0).toFixed(0)}s`

  const labelOf = (s: ChatSource) => {
    const detail = detailOf(s)
    // 跨笔记来源带《标题》前缀；提问笔记本篇的来源保持原样
    if (s.note_title && s.task_id && s.task_id !== anchorTaskId) {
      return `《${s.note_title}》 ${detail}`
    }
    return detail
  }

  const jumpTitleOf = (s: ChatSource) => {
    if (!s.task_id) return undefined
    if (s.task_id !== currentTaskId) return '点击跳转到该笔记对应位置'
    // 本篇来源：定位到笔记内对应章节（转录来源按时间映射到章节）
    if (s.source_type === 'markdown' && s.section_title) return '点击定位到笔记该章节'
    if (s.source_type === 'transcript' && s.start_time != null)
      return '点击定位到笔记该章节'
    return undefined
  }

  const handleJump = (s: ChatSource) => {
    if (!s.task_id) return
    // 用实时当前笔记判断是否需要切换（锚点只管标签显示）
    if (s.task_id !== currentTaskId) setCurrentTask(s.task_id)
    requestJump({
      task_id: s.task_id,
      section_title: s.section_title,
      start_time: s.start_time,
    })
  }

  return (
    <div className="mt-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
      >
        <BookOpen className="h-3 w-3" />
        <span>引用来源 ({sources.length})</span>
        {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>
      {expanded && (
        <div className="mt-1 flex flex-wrap gap-1">
          {sources.map((s, i) => {
            const jumpTitle = jumpTitleOf(s)
            return (
              <Badge
                key={i}
                variant="outline"
                className="text-xs font-normal"
                style={jumpTitle ? { cursor: 'pointer' } : undefined}
                title={jumpTitle}
                onClick={jumpTitle ? () => handleJump(s) : undefined}
              >
                {labelOf(s)}
              </Badge>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default function ChatPanel({ taskId, mode, onModeChange }: ChatPanelProps) {
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null)
  // 覆盖率：已索引数 / 笔记总数。之前只拿 /chat/indexed 默认 limit=50 的
  // 列表长度当"已覆盖 N 篇"，笔记一多数字就对不上（用户实拍：只显示 9 篇）。
  // 现在走 /chat/coverage 的真实统计，补建按钮按缺失数提示。
  const [coverage, setCoverage] = useState<{ indexed: number; total: number } | null>(null)
  const [backfilling, setBackfilling] = useState(false)

  const scope = useChatStore(state => state.scope)
  const setScope = useChatStore(state => state.setScope)
  const chatModelName = useChatStore(state => state.chatModelName)
  const setChatModelName = useChatStore(state => state.setChatModelName)
  const key = chatKey(taskId, scope)
  const messages = useChatStore(state => state.chatHistory[key]) ?? []
  const addMessage = useChatStore(state => state.addMessage)
  const clearChat = useChatStore(state => state.clearChat)

  const currentTaskId = useTaskStore(state => state.currentTaskId)
  const tasks = useTaskStore(state => state.tasks)
  const currentTask = useMemo(
    () => tasks.find(t => t.id === currentTaskId) ?? null,
    [tasks, currentTaskId],
  )

  // 问答模型列表：复用生成笔记的可用模型（modelList），选过即持久化；
  // 校验有效性（模型被删后回落），为空时首次自动选中第一个。
  const modelList = useModelStore(state => state.modelList)
  const loadEnabledModels = useModelStore(state => state.loadEnabledModels)
  useEffect(() => {
    loadEnabledModels()
  }, [loadEnabledModels])
  useEffect(() => {
    if (!modelList.length) return
    if (!chatModelName || !modelList.some(m => m.model_name === chatModelName)) {
      setChatModelName(modelList[0].model_name)
    }
  }, [modelList, chatModelName, setChatModelName])

  // 检查索引状态，未索引时自动触发，indexing 时轮询。
  // 全部笔记模式：覆盖率走 /chat/coverage 真实统计；若当前笔记已索引但
  // 全局缺口大（缺失>0 且已索引<=1），自动补建一次历史索引。
  // 注意轮询只跟当前 taskId 走：切笔记会重跑本 effect，先查新笔记的
  // 单篇状态（idle→自动索引→转圈），这是切笔记后转圈的来源，属正常；
  // 覆盖率与补建只在 scope==='all' 时跑，不阻塞单篇问答。
  useEffect(() => {
    if (!taskId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      try {
        const [res, cov] = await Promise.all([
          getChatStatus(taskId),
          scope === 'all'
            ? getIndexCoverage().catch(() => null)
            : Promise.resolve(null),
        ])
        if (cancelled) return
        setIndexStatus(res.status)
        if (cov) setCoverage({ indexed: cov.indexed, total: cov.total_notes })

        if (res.status === 'idle') {
          // 未索引，触发后台索引
          await indexTask(taskId)
          if (!cancelled) setIndexStatus('indexing')
        }

        // 当前笔记已索引、但全局几乎是空的：历史笔记缺全局索引，自动补建一次
        if (scope === 'all' && res.status === 'indexed' && cov && cov.missing.length > 0 && cov.indexed <= 1) {
          try {
            setBackfilling(true)
            await backfillGlobalIndex()
          } catch {
            // 补索引失败不阻塞问答，单篇索引仍可用
          } finally {
            if (!cancelled) {
              setBackfilling(false)
              getIndexCoverage()
                .then(next => {
                  if (!cancelled && next) setCoverage({ indexed: next.indexed, total: next.total_notes })
                })
                .catch(() => {})
            }
          }
        }

        // indexing 状态持续轮询
        if (res.status === 'indexing' || res.status === 'idle') {
          timer = setTimeout(poll, 2000)
        }
      } catch {
        if (!cancelled) setIndexStatus('failed')
      }
    }

    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [taskId, scope])

  const handleSend = useCallback(
    async (value: string) => {
      const question = value.trim()
      if (!question || loading) return

      // 问答模型：优先用面板上用户自选的（与左侧生成表单解耦），
      // 其次回落任务卡片自带配置；provider 由 modelList 反查。
      const modelName = chatModelName || currentTask?.formData?.model_name
      const providerId =
        modelList.find(m => m.model_name === modelName)?.provider_id ||
        currentTask?.formData?.provider_id
      if (!providerId || !modelName) {
        toast.error('无法获取模型配置，请先去设置页添加模型')
        return
      }

      addMessage(key, { role: 'user', content: question })
      setInput('')
      setLoading(true)

      try {
        const history = messages.map(m => ({ role: m.role, content: m.content }))
        const res = await askQuestion({
          task_id: taskId,
          question,
          history,
          provider_id: providerId,
          model_name: modelName,
          scope,
        })
      addMessage(key, {
        role: 'assistant',
        content: res.answer,
        sources: res.sources,
        // 锚定提问时的笔记：徽章标签的本篇/跨篇判定不再随浏览漂移
        ask_task_id: taskId,
      })
      } catch (e: any) {
        // 后端 R.error 透出的 msg（含模型/供应商/上游原话）优先展示，
        // 否则用户看到的永远是“问答请求失败”，无法定位是哪一环坏了。
        toast.error(e?.msg || e?.message || '问答请求失败')
      } finally {
        setLoading(false)
      }
    },
    [loading, taskId, key, scope, chatModelName, modelList, currentTask, messages, addMessage],
  )

  // 转换为 Bubble.List 的数据格式
  const bubbleItems = useMemo(() => {
    const items = messages.map((msg, i) => ({
      key: `msg-${i}`,
      role: msg.role === 'user' ? ('user' as const) : ('ai' as const),
      content: msg.content,
      footer:
        msg.role === 'assistant' && msg.sources ? (
          <SourceBadges
            sources={msg.sources}
            anchorTaskId={msg.ask_task_id ?? taskId}
            currentTaskId={taskId}
          />
        ) : undefined,
    }))

    if (loading) {
      items.push({
        key: 'loading',
        role: 'ai' as const,
        content: '思考中...',
        loading: true,
      } as any)
    }

    return items
    // taskId 必须进依赖：SourceBadges 靠它判断“本篇/跨笔记”来源并决定
    // 点击时是否先切笔记。漏掉会导致切换笔记后徽章仍按旧笔记计算跳转
    // 目标（browser-use 实测：切到香水后点猛玛来源，setCurrentTask 不触发）。
  }, [messages, loading, taskId])

  // Bubble 角色配置
  const roles = useMemo(
    () => ({
      user: {
        placement: 'end' as const,
        avatar: (
          <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-500 text-white">
            <UserRound className="h-4 w-4" />
          </div>
        ),
        variant: 'filled' as const,
        styles: { content: { background: '#3b82f6', color: '#fff' } },
      },
      ai: {
        placement: 'start' as const,
        avatar: (
          <div className="flex h-7 w-7 items-center justify-center rounded-full bg-neutral-500 text-white">
            <Bot className="h-4 w-4" />
          </div>
        ),
        variant: 'outlined' as const,
        contentRender: (content: any) => (
          <div className="markdown-body prose prose-sm max-w-none prose-p:my-1 prose-li:my-0.5 prose-headings:my-2">
            {/* 问答回答里也可能带公式：和主阅读器一样挂 remark-math/rehype-katex，
                之前只挂了 remarkGfm，公式只能显示原文 */}
            <ReactMarkdown
              remarkPlugins={[remarkGfm, remarkMath]}
              rehypePlugins={[rehypeKatex]}
            >
              {normalizeMathDelimiters(
                typeof content === 'string' ? content : String(content),
              )}
            </ReactMarkdown>
          </div>
        ),
      },
    }),
    [],
  )

  if (indexStatus === null || indexStatus === 'indexing' || indexStatus === 'idle') {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground">
        <Loader2 className="h-6 w-6 animate-spin" />
        <div className="text-center">
          <p className="text-sm font-medium">正在索引笔记内容...</p>
          <p className="mt-1 text-xs">首次使用需下载 Embedding 模型（约 80MB），请耐心等待</p>
        </div>
      </div>
    )
  }

  if (indexStatus === 'failed') {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground">
        <span className="text-sm">索引失败，请重试</span>
        <Button
          size="sm"
          variant="outline"
          onClick={async () => {
            setIndexStatus('indexing')
            try {
              await indexTask(taskId)
            } catch {
              toast.error('索引请求失败')
              setIndexStatus('failed')
            }
          }}
        >
          重新索引
        </Button>
      </div>
    )
  }

  const handleScopeChange = (next: ChatScope) => {
    if (next !== scope) setScope(next)
  }

  // 手动补建：后端是后台逐个跑（几十篇笔记要几分钟），之前只调一次接口
  // 就收尾，按钮"闪一下就没下文"（用户实拍）。这里发完后每 3s 轮询一次
  // /chat/coverage，直到缺口补满或 5 分钟超时；数字实时涨，跑完给 toast。
  // 注意：这里不能用 useCallback——它在下面两个 early return 之后，
  // status 从 indexing 切到 indexed 时 hook 数量变化会整页白屏（2026-10-04
  // 手机实拍）。用普通函数，身份变化不影响 onClick 使用。
  const handleBackfill = async () => {
    if (backfilling) return
    setBackfilling(true)
    toast.success('已开始补建索引：后台逐个处理，数字会慢慢涨，不影响提问')
    try {
      await backfillGlobalIndex()
    } catch {
      toast.error('补建请求失败，请稍后重试')
      setBackfilling(false)
      return
    }
    for (let round = 0; round < 100; round += 1) {
      await new Promise(r => setTimeout(r, 3000))
      try {
        const next = await getIndexCoverage()
        setCoverage({ indexed: next.indexed, total: next.total_notes })
        if (next.missing.length === 0 || next.indexed >= next.total_notes) {
          toast.success(`补建完成：已覆盖全部 ${next.total_notes} 篇笔记`)
          break
        }
        if (round === 99) {
          toast('补建仍在后台继续，稍后数字会继续涨', { duration: 5000 })
        }
      } catch {
        /* 轮询失败继续下一轮 */
      }
    }
    setBackfilling(false)
  }

  return (
    <div className="flex h-full flex-col sm:border-l">
      {/* 头部：窄屏允许换行，范围切换按钮不再把标题挤掉 */}
      <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5 border-b px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="shrink-0 text-sm font-medium">AI 问答</span>
          {/* 范围切换：全部笔记（默认）/ 当前笔记 */}
          <div className="flex items-center rounded-md bg-muted p-0.5 text-xs">
            <button
              className={`rounded px-2 py-0.5 whitespace-nowrap ${scope === 'all' ? 'bg-background font-medium shadow-sm' : 'text-muted-foreground'}`}
              onClick={() => handleScopeChange('all')}
              title="跨全部历史笔记检索"
            >
              全部笔记
            </button>
            <button
              className={`rounded px-2 py-0.5 whitespace-nowrap ${scope === 'current' ? 'bg-background font-medium shadow-sm' : 'text-muted-foreground'}`}
              onClick={() => handleScopeChange('current')}
              title="只检索当前笔记"
            >
              当前笔记
            </button>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-muted-foreground hover:text-foreground"
            onClick={() => onModeChange(mode === 'half' ? 'full' : 'half')}
            title={mode === 'half' ? '全屏' : '半屏'}
          >
            {mode === 'half' ? (
              <Maximize2 className="h-3.5 w-3.5" />
            ) : (
              <Minimize2 className="h-3.5 w-3.5" />
            )}
          </Button>
          {messages.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-muted-foreground hover:text-red-500"
              onClick={() => clearChat(key)}
              title="清空当前范围的问答记录"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>
      {/* 范围提示条：覆盖率来自 /chat/coverage 真实统计（已索引/笔记总数）。
          数字对不上时的三种正常原因：① 刚装好/刚升级，历史笔记还没建过索引，
          点"补建索引"即可；② 笔记很多时补建是后台逐个跑，数字会慢慢涨；
          ③ 已删除笔记的索引会被清理。补建不阻塞提问，单篇索引仍可用。 */}
      {scope === 'all' && (
        <div className="border-b px-3 py-1 text-xs text-muted-foreground">
          {backfilling
            ? (
              <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <Loader2 className="h-3 w-3 animate-spin" />
                {coverage
                  ? `正在补建索引…已索引 ${coverage.indexed} / 共 ${coverage.total} 篇（后台逐个跑，不影响提问）`
                  : '正在补建索引…（后台逐个跑，不影响提问）'}
              </span>
            )
            : coverage === null
              ? '正在统计已索引笔记…'
              : coverage.total === 0
                ? '暂无笔记可索引'
                : coverage.indexed >= coverage.total
                  ? `已覆盖全部 ${coverage.total} 篇笔记`
                  : (
                    <span className="flex flex-wrap items-center gap-x-1 gap-y-0.5">
                      <span>{`已索引 ${coverage.indexed} / 共 ${coverage.total} 篇笔记`}</span>
                      <button
                        className="ml-1 shrink-0 rounded border px-1.5 py-0.5 underline hover:text-foreground"
                        onClick={handleBackfill}
                      >
                        补建索引
                      </button>
                    </span>
                  )}
        </div>
      )}

      {/* 消息列表 */}
      <div className="flex-1 overflow-hidden">
        {messages.length === 0 && !loading ? (
          <div className="flex h-full items-center justify-center text-center text-sm text-muted-foreground">
            <div>
              <p>{scope === 'all' ? '可跨全部历史笔记提问' : '针对当前笔记内容提问'}</p>
              <p className="mt-1 text-xs">例如：这个视频的核心观点是什么？</p>
            </div>
          </div>
        ) : (
          <Bubble.List
            items={bubbleItems}
            role={roles}
            style={{ height: '100%' }}
          />
        )}
      </div>

      {/* 输入区域 */}
      <div className="border-t px-3 py-2">
        {/* 问答模型选择：与左侧生成表单解耦，选过即记住（随 chat store 持久化） */}
        <div className="mb-2 flex items-center gap-2">
          <span className="shrink-0 text-xs text-muted-foreground">问答模型</span>
          <Select
            value={chatModelName}
            onValueChange={setChatModelName}
            onOpenChange={open => {
              if (open) loadEnabledModels()
            }}
          >
            <SelectTrigger className="h-7 min-w-0 flex-1 truncate text-xs">
              <SelectValue placeholder="选择问答模型" />
            </SelectTrigger>
            <SelectContent>
              {modelList.map(m => (
                <SelectItem key={m.id} value={m.model_name}>
                  {m.model_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Sender
          value={input}
          onChange={setInput}
          onSubmit={handleSend}
          loading={loading}
          placeholder="输入你的问题..."
        />
      </div>
    </div>
  )
}
