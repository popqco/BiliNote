"use client"

import { useTaskStore } from "@/store/taskStore"
import { useEffect, useState, useRef } from "react"
import { Play } from "lucide-react"
import { cn } from "@/lib/utils"
import {ScrollArea} from "@/components/ui/scroll-area.tsx";

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

  useEffect(() => {
    setTask(getCurrentTask())
  }, [currentTaskId, getCurrentTask])

  // 问答来源跳转：按时间定位到最接近的转录片段并高亮滚动
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
    // 等一帧让高亮先生效再滚动
    requestAnimationFrame(() => {
      segmentRefs.current[best]?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [focusTime, task])

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
            <ScrollArea className="w-full overflow-y-auto">

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
