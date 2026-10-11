import type { ReactNode, FC } from 'react'
// import "@/global.css"
import { Toaster } from 'react-hot-toast'
// 【原型】亮色配色评审（2026-10-11）：底部浮条切换 ?variant=，生产构建不渲染，评审后删除
import LightModePrototype from '@/prototype/lightMode/LightModePrototype.tsx'

interface RootLayoutProps {
  children: ReactNode
}

export const metadata = {
  title: 'BiliNote - 视频笔记生成器',
  description: '通过视频链接结合大模型自动生成对应的笔记',
}

const RootLayout: FC<RootLayoutProps> = ({ children }) => {
  return (
    <div className="bg-background text-foreground min-h-screen font-sans">
      <LightModePrototype />
      <Toaster
        position="top-center" // 顶部居中显示
        toastOptions={{
          style: {
            borderRadius: '8px',
            background: '#333',
            color: '#fff',
          },
        }}
      />
      {children}
    </div>
  )
}

export default RootLayout
