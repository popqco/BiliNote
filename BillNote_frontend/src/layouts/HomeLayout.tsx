import React, { FC, useRef, useState } from 'react'
import { SlidersHorizontal, PanelLeftClose, PanelLeftOpen, History as HistoryIcon, Sun, Moon } from 'lucide-react'
import { useTheme } from 'next-themes'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'

import { Link } from 'react-router-dom'
import { ResizablePanel, ResizablePanelGroup, ResizableHandle } from '@/components/ui/resizable'
import { ScrollArea } from "@/components/ui/scroll-area.tsx"
import type { ImperativePanelHandle } from 'react-resizable-panels'
import logo from '@/assets/icon.svg'

interface IProps {
  NoteForm: React.ReactNode
  Preview: React.ReactNode
  History: React.ReactNode
}

/**
 * 面板布局持久化（localStorage）：
 * 之前 PanelGroup 没有 autoSaveId，尺寸只存在内存里——进一次「设置」页
 * HomeLayout 被卸载，回来时三个面板全部回到 defaultSize，用户自己拖好的
 * 比例被重置（用户实拍反馈）。
 */
const HOME_LAYOUT_STORAGE_KEY = 'bilinote-home-layout'

/** 折叠状态单独存一份布尔值：autoSaveId 存的是尺寸（折叠=0），恢复时不会再触发 onCollapse */
function readCollapsed(side: 'left' | 'middle'): boolean {
  try {
    return localStorage.getItem(`bilinote-home-${side}-collapsed`) === '1'
  } catch {
    return false
  }
}

function persistCollapsed(side: 'left' | 'middle', collapsed: boolean): void {
  try {
    localStorage.setItem(`bilinote-home-${side}-collapsed`, collapsed ? '1' : '0')
  } catch {
    /* 隐私模式等场景写不了 localStorage，忽略即可 */
  }
}

const HomeLayout: FC<IProps> = ({ NoteForm, Preview, History }) => {
  const { resolvedTheme, setTheme } = useTheme()
  const [, setShowSettings] = useState(false)
  const leftPanelRef = useRef<ImperativePanelHandle>(null)
  const middlePanelRef = useRef<ImperativePanelHandle>(null)
  // 折叠状态同样要跟着面板尺寸一起恢复：autoSaveId 会把折叠面板恢复成 0 宽度，
  // 但 onCollapse 不一定触发，展开按钮就没了。
  const [isLeftCollapsed, setIsLeftCollapsed] = useState(() => readCollapsed('left'))
  const [isMiddleCollapsed, setIsMiddleCollapsed] = useState(() => readCollapsed('middle'))

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <ResizablePanelGroup
        direction="horizontal"
        className="h-full w-full"
        autoSaveId={HOME_LAYOUT_STORAGE_KEY}
      >
        {/* 左边表单 */}
        <ResizablePanel
          ref={leftPanelRef}
          defaultSize={23}
          minSize={10}
          maxSize={35}
          collapsible
          collapsedSize={0}
          onCollapse={() => {
            setIsLeftCollapsed(true)
            persistCollapsed('left', true)
          }}
          onExpand={() => {
            setIsLeftCollapsed(false)
            persistCollapsed('left', false)
          }}
        >
          <aside className="border-border bg-card flex h-full flex-col overflow-hidden border-r">
            <header className="flex h-16 items-center justify-between px-6">
              <div className="flex items-center gap-2">
                <div className="flex h-10 w-10 items-center justify-center overflow-hidden rounded-2xl">
                  <img src={logo} alt="logo" className="h-full w-full object-contain" />
                </div>
                <div className="text-foreground text-2xl font-bold">BiliNote</div>
              </div>
              <div className="flex items-center gap-1">
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        onClick={() => leftPanelRef.current?.collapse()}
                        className="text-muted-foreground hover:text-primary hover:bg-accent cursor-pointer rounded p-1"
                      >
                        <PanelLeftClose className="h-5 w-5" />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>
                      <span>收起工作区</span>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
                        className="text-muted-foreground hover:text-primary hover:bg-accent cursor-pointer rounded p-1"
                      >
                        {resolvedTheme === 'dark' ? (
                          <Sun className="h-5 w-5" />
                        ) : (
                          <Moon className="h-5 w-5" />
                        )}
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>
                      <span>{resolvedTheme === 'dark' ? '切换到亮色模式' : '切换到暗色模式'}</span>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger onClick={() => setShowSettings(true)}>
                      <Link to={'/settings'}>
                        <SlidersHorizontal className="text-muted-foreground hover:text-primary cursor-pointer" />
                      </Link>
                    </TooltipTrigger>
                    <TooltipContent>
                      <span>全局配置</span>
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </div>
            </header>
            <ScrollArea className="flex-1 overflow-auto">
              <div className="p-4">{NoteForm}</div>
            </ScrollArea>
          </aside>
        </ResizablePanel>

        <ResizableHandle />

        {/* 左面板折叠时的展开按钮 */}
        {isLeftCollapsed && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={() => leftPanelRef.current?.expand()}
                  className="border-border bg-card hover:bg-accent flex h-full w-8 shrink-0 items-center justify-center border-r"
                >
                  <PanelLeftOpen className="h-4 w-4 text-muted-foreground" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="right">
                <span>展开工作区</span>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}

        {/* 中间历史 */}
        <ResizablePanel
          ref={middlePanelRef}
          defaultSize={16}
          minSize={10}
          maxSize={30}
          collapsible
          collapsedSize={0}
          onCollapse={() => {
            setIsMiddleCollapsed(true)
            persistCollapsed('middle', true)
          }}
          onExpand={() => {
            setIsMiddleCollapsed(false)
            persistCollapsed('middle', false)
          }}
        >
          <aside className="border-border bg-card flex h-full flex-col overflow-hidden border-r">
            <header className="border-border flex h-10 shrink-0 items-center justify-between border-b px-3">
              <span className="text-muted-foreground text-sm font-medium">生成历史</span>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      onClick={() => middlePanelRef.current?.collapse()}
                      className="text-muted-foreground hover:text-primary hover:bg-accent cursor-pointer rounded p-1"
                    >
                      <PanelLeftClose className="h-4 w-4" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>
                    <span>收起历史</span>
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </header>
            <ScrollArea className="flex-1 overflow-auto">
              <div>{History}</div>
            </ScrollArea>
          </aside>
        </ResizablePanel>

        <ResizableHandle />

        {/* 中间面板折叠时的展开按钮 */}
        {isMiddleCollapsed && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={() => middlePanelRef.current?.expand()}
                  className="border-border bg-card hover:bg-accent flex h-full w-8 shrink-0 items-center justify-center border-r"
                >
                  <HistoryIcon className="h-4 w-4 text-muted-foreground" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="right">
                <span>展开历史</span>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}

        {/* 右边预览 */}
        <ResizablePanel defaultSize={61} minSize={30}>
          <main className="bg-card flex h-full flex-col overflow-hidden p-6">{Preview}</main>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  )
}

export default HomeLayout
