import { FC } from 'react'
import Lottie from 'lottie-react'
import loadingJson from '@/assets/Lottie/idle.json'

const Idle: FC<{ className?: string }> = ({ className }) => {
  // 手机窄屏上 350px 固定尺寸会撑出横向滚动条并挤掉下方文字：改用流式宽度，
  // 上限 350（桌面观感不变），不足则跟随容器收缩。
  return (
    <div className={`flex items-center justify-center ${className ?? ''}`}>
      <Lottie
        animationData={loadingJson}
        loop
        autoplay
        style={{ width: 'min(100%, 350px)', height: 'auto', aspectRatio: '1' }}
      />
    </div>
  )
}

export default Idle
