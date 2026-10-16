# 用户主动停止任务：run 登记 + 终态 CANCELLED，不计失败

任务一旦开始就无法中断——用户设错模型/参数时只能干等整条流程（含转写重试、
总结降级阶梯这类「一直不停地尝试」）跑完才能改设置重来（2026-10-16 用户需求）。
现引入用户主动取消：

- **每次执行登记一个 run**（`services/task_runs.py`）：`register_run` 生成带
  `cancel_event` 的 TaskRun 并登记为 task_id 的当前 run；`/cancel_task` 置位该
  event；执行方在阶段边界/重试间隙/流式增量处查谓词，命中抛 `TaskCancelled`
  （`exceptions/task_cancel.py`），由 `NoteGenerator.generate` 收敛为终态。
- **CANCELLED 是终态但不是失败**：进 `TERMINAL_STATUSES`（去重、reap、自动化
  等待都按终态处理），但不计入失败冷却/告警；前端展示「已停止」（中性色）而不是
  红字失败，卡片/表单直接给「重新生成」。
- **旧 run 作废（superseded）**：停止后立刻重试会启动新 run，而旧 run 可能还在
  不可中断的调用里退场。`register_run` 把同 task_id 的旧 run 标为作废并置位其
  event——旧 run 之后**一切状态写入被抑制**（否则它会用迟到的 CANCELLED/进度
  覆盖新 run 的 PENDING/阶段），它也同时在下一个检查点自杀。
- **预取消墓碑**：点停止与执行线程启动之间存在毫秒级窗口（提交写入磁盘后
  `background_tasks`/线程才跑 `register_run`）。此时没有 live run，
  `request_cancel_task` 落一枚墓碑，晚到的 `register_run` 直接以已取消状态诞生；
  下一次提交（重试/复用 task_id）会 `clear_cancel_request` 清墓碑——停止只作用于
  用户点它的那一次执行，不误伤新的重试。
- **取消检查点**：yt-dlp 进度回调（抛异常穿出 `extract_info`，不落进它的
  DownloadError 重试分类）、UniversalGPT 流式增量 / 重试退避 / 分批与合并循环、
  分段转写段间与段内退避、VideoReader 逐帧与拼图、转写信号量等待
  （`_acquire_cancellable`）、以及主流程各阶段边界。等 GPU 信号量与长退避这两处
  最容易「点了停止还在等」，单独做了可中断获取/分片睡眠。

_Considered options_: ① 杀线程（`PyThread` 无法安全终止，撞上文件句柄/ffmpeg
子进程会留下脏文件）；② 杀进程重启 sidecar（会打断同进程其它在跑任务，且冷启动
几十秒）；③ 只做「前端不再显示」的假停止（任务仍在跑，白耗配额与 GPU）——均被否，
采用协作式取消：检查点最密处（流式增量）秒级响应，不可中断的单次调用（yt-dlp
元信息、单次上游请求）最多等到该调用返回。
