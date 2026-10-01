// components/LazyImage.tsx
import { useInView } from 'react-intersection-observer'
import { FC, useState } from 'react'
import clsx from 'clsx'

interface LazyImageProps {
    src: string
    alt?: string
    className?: string
    placeholder?: string
}

const LazyImage: FC<LazyImageProps> = ({ src, alt, className, placeholder = '/placeholder.png' }) => {
    const { ref, inView } = useInView({ triggerOnce: true, threshold: 0.1 })
    const [loaded, setLoaded] = useState(false)
    const [failed, setFailed] = useState(false)
    // 加载失败（含封面代理 404）也退回占位图，不再留破图
    const finalSrc = failed || !src ? placeholder : src

    return (
        <div ref={ref} className={clsx('overflow-hidden', className)}>
            {inView ? (
                <img
                    src={finalSrc}
                    alt={alt}
                    loading="lazy"
                    onLoad={() => setLoaded(true)}
                    onError={() => {
                        if (!failed) setFailed(true)
                    }}
                    className={clsx('transition-opacity duration-300', loaded || failed ? 'opacity-100' : 'opacity-0') + ' h-10 w-14 rounded-md object-cover'}
                />
            ) : (
                <img src={placeholder} alt="loading" className="h-10 w-14 rounded-md object-cover opacity-30" />
            )}
        </div>
    )
}

export default LazyImage
