import { useEffect, useState } from 'react'
import { useTaskStore, STOP_ARM_MS, isTaskStopArmed } from '@/store/taskStore'

/**
 * 「停止」按钮的武装（可用）状态（2026-10-18 误触修复）。
 *
 * 背景：点「重新生成/提交」后，同一个按钮位与同一块面板会在毫秒级变成
 * 「停止生成」；手还停在原地的第二下点击（双击，或觉得没反应又点一次）
 * 会把刚起来的新 run 直接停掉——用户实拍日志里两次 stop 都紧跟 retry
 * 到达（+3s/+8s），而用户以为只按了「重新生成」。
 *
 * 这里给出「启动后 STOP_ARM_MS 内未武装」的响应式布尔：所有停止入口在
 * 未武装时不可用（按钮禁用/不渲染 + store 层二次拦截），窗口过去自动
 * 恢复；没有 startedAt（自动化任务、老数据）直接视为可用。
 */
export function useStopArmed(taskId?: string | null): boolean {
  const startedAt = useTaskStore(s =>
    taskId ? s.tasks.find(t => t.id === taskId)?.startedAt : undefined,
  )
  const [, forceTick] = useState(0)
  const armed = isTaskStopArmed({ startedAt })

  useEffect(() => {
    if (armed || !startedAt) return
    // 武装时刻 + 一帧余量后再渲染一次，让按钮自动「解禁」
    const left = Math.max(30, startedAt + STOP_ARM_MS - Date.now() + 30)
    const t = window.setTimeout(() => forceTick(n => n + 1), left)
    return () => window.clearTimeout(t)
  }, [armed, startedAt])

  return armed
}

export default useStopArmed
