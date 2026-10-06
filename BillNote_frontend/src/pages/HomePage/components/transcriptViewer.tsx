"use client"

import { useTaskStore } from "@/store/taskStore"
import { useEffect, useState, useRef, useCallback } from "react"
import { Play } from "lucide-react"
import { cn } from "@/lib/utils"
import {ScrollArea} from "@/components/ui/scroll-area.tsx";
import { useSmoothScroll } from "@/hooks/useSmoothScroll"

interface Segment {
  start: number
  end: number
  text: string

}

interface Task {
  transcript?: {
    segments?: Segment[]
  }
}

const TranscriptViewer = ({ focusTime }: { focusTime?: number | null }) => {
  const getCurrentTask = useTaskStore((state) => state.getCurrentTask)
  const currentTaskId = useTaskStore((state) => state.currentTaskId)
  const [task, setTask] = useState<Task | null>(null)
  const [activeSegment, setActiveSegment] = useState<number | null>(null)
  const segmentRefs = useRef<(HTMLDivElement | null)[]>([])
  // 原文面板与笔记正文同为长文阅读场景，滚轮惯性平滑与正文同款（设置页开关+档位）
  const { attach: attachSmoothScroll, scrollTo: smoothScrollTo } = useSmoothScroll()
  const attachViewport = useCallback(
    (node: HTMLDivElement | null) => attachSmoothScroll(node),
    [attachSmoothScroll],
  )

  useEffect(() => {
    setTask(getCurrentTask())
  }, [currentTaskId, getCurrentTask])

  // 问答来源跳转：按时间定位到最接近的转录片段并高亮滚动。
  // 面板刚挂载时布局未稳（外层列宽/滚动容器高度在变），一次
  // scrollIntoView 经常落空——轮询重试直到目标片段真正滚进视野。
  useEffect(() => {
    if (focusTime == null) return
    const segments = task?.transcript?.segments
    if (!segments?.length) return
    let best = 0
    for (let i = 1; i < segments.length; i++) {
      if (Math.abs(segments[i].start - focusTime) < Math.abs(segments[best].start - focusTime)) {
        best = i
      }
    }
    setActiveSegment(best)
    let tries = 0
    const timer = window.setInterval(() => {
      tries += 1
      const el = segmentRefs.current[best]
      const vp = el?.closest('[data-slot="scroll-area-viewport"]') as HTMLElement | null
      if (el && vp) {
        const er = el.getBoundingClientRect()
        const vr = vp.getBoundingClientRect()
        const inside = er.top >= vr.top && er.bottom <= vr.bottom
        if (inside) {
          window.clearInterval(timer)
          return
        }
        // 平滑滚动约 300ms，校验间隔只有 150ms：每轮都 smooth 会不停
        // 重启动画、位置永远到不了目标（与章节跳转同款坑）。前两次
        // smooth，之后一律瞬时补滚（Lenis 模式下 lerp 指数逼近，重设
        // 目标不重启动画，同样的节奏即可）。
        const targetTop = vp.scrollTop + (er.top - vr.top) - vr.height / 2 + er.height / 2
        smoothScrollTo(vp, targetTop, { immediate: tries >= 3 })
      }
      if (tries >= 15) window.clearInterval(timer)
    }, 150)
    return () => window.clearInterval(timer)
  }, [focusTime, task, smoothScrollTo])

  const formatTime = (seconds: number): string => {
    const mins = Math.floor(seconds / 60)
    const secs = Math.floor(seconds % 60)
    return `${mins}:${secs.toString().padStart(2, "0")}`
  }

  const handleSegmentClick = (index: number) => {
    setActiveSegment(index)
    // Here you could add functionality to play the audio from this segment
  }

  const scrollToSegment = (index: number) => {
    segmentRefs.current[index]?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    })
  }

  return (
      <div className="transcript-viewer flex h-full w-full flex-col  rounded-md border bg-card p-4 shadow-sm">
        <h2 className="mb-4 text-lg font-medium">转写结果</h2>
        {!task?.transcript?.segments?.length ? (
            <div className="flex h-full items-center justify-center text-muted-foreground">暂无转写内容</div>
        ) : (
            <>


            <div className="mb-3 grid grid-cols-[80px_1fr] gap-2 border-b pb-2 text-xs font-medium text-muted-foreground">
                <div>时间</div>
                <div>内容</div>
              </div>
            {/* min-h-0 + flex-1：外层是 flex-col，不限高的话片段列表会把
                面板撑到整篇转写的高度，scrollIntoView 找不到自身滚动容器
                就去滚笔记主视口——点时间徽章时整个阅读区被拽走（2026-10-03
                用户反馈“时间戳跳转不直观”的元凶之一）。 */}
            <ScrollArea viewportRef={attachViewport} className="min-h-0 w-full flex-1">

              <div className="space-y-1">
                {task.transcript.segments.map((segment, index) => (
                    <div
                        key={index}
                        ref={(el) => (segmentRefs.current[index] = el)}
                        className={cn(
                            "group grid grid-cols-[80px_1fr] gap-2 rounded-md p-2 transition-colors hover:bg-accent",
                            activeSegment === index && "bg-accent",
                        )}
                        onClick={() => handleSegmentClick(index)}
                    >
                      <div className="flex items-center gap-1 text-xs text-muted-foreground">
                        <button
                            className="invisible rounded-full p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground group-hover:visible"
                            onClick={(e) => {
                              e.stopPropagation()
                              // Add play functionality here
                            }}
                        >
                          {/*<Play className="h-3 w-3" />*/}
                        </button>
                        <span>{formatTime(segment.start)}</span>
                      </div>

                      <div className="text-sm leading-relaxed text-foreground">
                        {segment.speaker && (
                            <span className="mr-2 rounded bg-secondary px-1.5 py-0.5 text-xs font-medium text-foreground">
                      {segment.speaker}
                    </span>
                        )}
                        {segment.text}
                      </div>
                    </div>
                ))}
              </div>
            </ScrollArea>

            </>
        )}


        {task?.transcript?.segments?.length > 0 && (
            <div className="mt-4 flex justify-between border-t pt-3 text-xs text-muted-foreground">
              <span>共 {task.transcript.segments.length} 条片段</span>
              <span>总时长: {formatTime(task.transcript.segments[task.transcript.segments.length - 1]?.end || 0)}</span>
            </div>
        )}
      </div>
  )
}

export default TranscriptViewer
