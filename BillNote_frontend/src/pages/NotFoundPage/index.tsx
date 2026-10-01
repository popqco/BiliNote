// src/pages/NotFoundPage.tsx
import NotFound from '@/components/Lottie/404.tsx'
import { Button } from '@/components/ui/button.tsx'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

/**
 * 桌面端用的是哈希路由，只要路由串被写坏（外部工具改了 location.hash、
 * 粘贴进来的深链接带了非法片段等），用户就会一直卡在这个 404 页上——
 * 表现就是「我什么都没干，界面自己变成这个画面了」。所以除了按钮，这里再
 * 加一个倒计时自动回首页，任何情况下都能自己走出来。
 */
const AUTO_BACK_SECONDS = 5

const NotFoundPage = () => {
  const navigate = useNavigate()
  const [left, setLeft] = useState(AUTO_BACK_SECONDS)

  useEffect(() => {
    const timer = setInterval(() => {
      setLeft(prev => (prev <= 1 ? 0 : prev - 1))
    }, 1000)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    if (left > 0) return
    navigate('/', { replace: true })
  }, [left, navigate])

  return (
    <div className="flex min-h-screen w-full flex-col items-center justify-center text-muted-foreground">
      <div className="text-center">
        <h1 className="mb-4 text-4xl font-bold">你好像走丢了哦！～～</h1>
        <p className="mb-4 text-lg">请检查你的网址是否正确，或者点击下面的按钮返回首页。</p>
        <Button onClick={() => navigate('/', { replace: true })} className="hover:underline">
          返回首页
        </Button>
        <p className="mt-3 text-sm">
          {left > 0 ? `${left} 秒后自动返回首页…` : '正在返回首页…'}
        </p>
      </div>
      <div>
        <NotFound />
      </div>
    </div>
  )
}

export default NotFoundPage
