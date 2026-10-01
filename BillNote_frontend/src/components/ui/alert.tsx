import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'

import { cn } from '@/lib/utils'

const alertVariants = cva(
  'relative w-full rounded-lg border px-4 py-3 text-sm grid has-[>svg]:grid-cols-[calc(var(--spacing)*4)_1fr] grid-cols-[0_1fr] has-[>svg]:gap-x-3 gap-y-0.5 items-start [&>svg]:size-4 [&>svg]:translate-y-0.5 [&>svg]:text-current',
  {
    variants: {
      variant: {
        default: 'bg-card text-card-foreground',
        destructive:
          'text-destructive bg-card [&>svg]:text-current *:data-[slot=alert-description]:text-destructive/90',
        success:
          'text-success bg-card [&>svg]:text-current *:data-[slot=alert-description]:text-success/90',
        warning:
          'text-foreground bg-red-50 dark:bg-red-500/10 [&>svg]:text-current *:data-[slot=alert-description]:text-warning/90',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  }
)

function Alert({
  className,
  variant,
  ...props
}: React.ComponentProps<'div'> & VariantProps<typeof alertVariants>) {
  return (
    <div
      data-slot="alert"
      role="alert"
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  )
}

function AlertTitle({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="alert-title"
      className={cn('col-start-2 line-clamp-1 min-h-4 font-medium tracking-tight', className)}
      {...props}
    />
  )
}

function AlertDescription({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="alert-description"
      // 这里原本是 grid + gap-1：grid 会把每个子元素（连纯文本节点也算）各占一行，
      // 于是「<strong>提示：</strong>后面一句话」这种写法被拆成两行，含 <code> 的
      // 说明更是每个片段一行。改成块级流式排版，文字按容器宽度自然折行。
      className={cn(
        'text-muted-foreground col-start-2 text-sm [&_p]:leading-relaxed',
        className
      )}
      {...props}
    />
  )
}

export { Alert, AlertTitle, AlertDescription }
