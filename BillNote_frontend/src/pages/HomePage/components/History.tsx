import NoteHistory from '@/pages/HomePage/components/NoteHistory.tsx'
import { useTaskStore } from '@/store/taskStore'
const History = () => {
  const currentTaskId = useTaskStore(state => state.currentTaskId)
  const setCurrentTask = useTaskStore(state => state.setCurrentTask)
  return (
    <>
      {/* 标题只用 HomeLayout 中间栏 header 那一个：这里曾经自带一个
          “🕐 生成历史”标题，和外层 header 上下重复（2026-10-05 用户实拍）。
          只保留搜索框上方的紧凑间距。 */}
      <div className={'flex w-full flex-col gap-4 px-2.5 py-1.5'}>
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
