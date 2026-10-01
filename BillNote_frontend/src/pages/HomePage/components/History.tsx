import NoteHistory from '@/pages/HomePage/components/NoteHistory.tsx'
import { useTaskStore } from '@/store/taskStore'
import { Clock } from 'lucide-react'
const History = () => {
  const currentTaskId = useTaskStore(state => state.currentTaskId)
  const setCurrentTask = useTaskStore(state => state.setCurrentTask)
  return (
    <>
      <div className={'flex h-full w-full flex-col gap-4 px-2.5 py-1.5'}>
        {/*生成历史    */}
        <div className="my-4 flex h-[40px] shrink-0 items-center gap-2">
          <Clock className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-base font-medium text-foreground">生成历史</h2>
        </div>
        {/* 滚动只用 HomeLayout 里那一层 ScrollArea：之前这里还套了一层写死高度的
            ScrollArea（sm:480px/md:720px），两层 Radix Viewport 叠在一起，
            滚轮事件在两层之间链式触发——滚历史列表会带着正文一起动，
            体感就是「笔记之间的滚动条互相干扰」。删掉内层，只留一层滚动。 */}
        <div className="min-h-0 flex-1">
          <NoteHistory onSelect={setCurrentTask} selectedId={currentTaskId} />
        </div>
      </div>
    </>
  )
}

export default History
