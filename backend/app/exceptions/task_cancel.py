"""用户停止生成任务。

任务被「停止」不是错误：与失败（FAILED）语义分开，最终状态落 CANCELLED，
前端展示「已停止」而不是红字失败，自动化也不计入失败冷却/告警。

放在 exceptions 包而非 services.note 里：gpt / transcriber / downloader 等
下游组件都要抛它，让它们 import app.services.note 会形成环。
"""


class TaskCancelled(Exception):
    def __init__(self, message: str = "已停止生成（用户手动停止）"):
        super().__init__(message)
        self.message = message
