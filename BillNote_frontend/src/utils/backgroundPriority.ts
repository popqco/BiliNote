/**
 * 前台任务优先级闸：用户点开笔记期间暂停后台同步/轮询。
 *
 * 远端手机经 Tailscale 回到 Worker 时，浏览器同源只有 ~6 个 HTTP/1.1
 * 连接。后台 3s/30s 轮询、历史封面、其它任务的结果拉取会和「当前这篇
 * 笔记」抢连接，表现就是点开半天不出正文。打开笔记的请求开始前置位，
 * 拿完正文（或失败）后放行。
 */
let busyDepth = 0

export function beginForegroundTask(): void {
  busyDepth += 1
}

export function endForegroundTask(): void {
  busyDepth = Math.max(0, busyDepth - 1)
}

export function isForegroundBusy(): boolean {
  return busyDepth > 0
}
