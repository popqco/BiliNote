// components/LazyImage.tsx
import { useInView } from 'react-intersection-observer'
import { FC, useCallback, useEffect, useRef, useState } from 'react'
import clsx from 'clsx'

interface LazyImageProps {
    src: string
    alt?: string
    className?: string
    placeholder?: string
}

/**
 * 封面懒加载图（历史列表）。
 *
 * 2026-10-17 重写（用户反馈「老笔记封面有时候刷新不出来」）：
 *  - 加载期间始终显示占位图。旧版在 inView 后把整块换成 opacity-0 的真图，
 *    而一张封面经后端代理要 3~4s——滚动列表时看到的就是一片空白，像"刷不出来"；
 *  - 已加载完成的图（缓存命中、重挂载、同 src）直接显示，不再依赖 onLoad。
 *    旧版只在 onLoad 里置 loaded，占位图分支切过来时 src 没变、事件不再触发，
 *    卡在 opacity-0 永远不可见；
 *  - 失败自动重试一次（加 cache-buster 参数绕开缓存），仍失败才回退占位图。
 *    旧版一次失败即永久失败（failed 锁死），直到整页重载。
 */
const LazyImage: FC<LazyImageProps> = ({ src, alt, className, placeholder = '/placeholder.png' }) => {
    const { ref, inView } = useInView({ triggerOnce: true, threshold: 0.1 })
    const [loaded, setLoaded] = useState(false)
    const [failed, setFailed] = useState(false)
    // 重试计数：非 0 时在 URL 上带 _retry 参数绕开代理/浏览器缓存
    const [retryTick, setRetryTick] = useState(0)
    const retriedRef = useRef(false)
    const imgRef = useRef<HTMLImageElement | null>(null)

    const hasCover = !!src && src !== placeholder
    const finalSrc = hasCover
        ? retryTick
            ? `${src}${src.includes('?') ? '&' : '?'}_retry=${retryTick}`
            : src
        : placeholder

    // 切换封面源（外部刷新了 audioMeta）时重置加载状态，避免沿用上一张的 loaded
    useEffect(() => {
        setLoaded(false)
        setFailed(false)
        setRetryTick(0)
        retriedRef.current = false
    }, [src])

    // 图片可能已经加载完（缓存命中时 onLoad 可能在监听挂上前就触发）：
    // 渲染后检查 complete，命中就直接显示
    const syncCompleteState = useCallback(() => {
        const img = imgRef.current
        if (img && img.complete && img.naturalWidth > 0) setLoaded(true)
    }, [])

    useEffect(() => {
        if (inView) syncCompleteState()
    }, [inView, finalSrc, syncCompleteState])

    const handleError = () => {
        if (!hasCover) {
            // 占位图本身失败：没有可回退的了，不再重试
            setFailed(true)
            return
        }
        if (!retriedRef.current) {
            retriedRef.current = true
            window.setTimeout(() => setRetryTick(t => t + 1), 600)
            return
        }
        setFailed(true)
    }

    return (
        <div ref={ref} className={clsx('relative overflow-hidden', className)}>
            {/* 占位底层：始终渲染（撑出 h-10 w-14 的尺寸），真封面加载完成后被盖住 */}
            <img
                src={placeholder}
                alt=""
                aria-hidden
                className="h-10 w-14 rounded-md object-cover opacity-40"
            />
            {inView && !failed && (
                <img
                    ref={node => {
                        imgRef.current = node
                        if (node) syncCompleteState()
                    }}
                    src={finalSrc}
                    alt={alt ?? '封面'}
                    onLoad={() => setLoaded(true)}
                    onError={handleError}
                    className={clsx(
                        'absolute inset-0 h-full w-full rounded-md object-cover transition-opacity duration-300',
                        loaded ? 'opacity-100' : 'opacity-0',
                    )}
                />
            )}
        </div>
    )
}

export default LazyImage
