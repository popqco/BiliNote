import enum


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    PARSING = "PARSING"
    DOWNLOADING = "DOWNLOADING"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    FORMATTING = "FORMATTING"
    SAVING = "SAVING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    # 用户主动停止（/cancel_task）：终态，但不是错误——前端展示「已停止」，
    # 自动化不计入失败冷却与告警（见 services/task_runs.py 模块文档）
    CANCELLED = "CANCELLED"

    @classmethod
    def description(cls, status):
        desc_map = {
            cls.PENDING: "排队中",
            cls.PARSING: "解析链接",
            cls.DOWNLOADING: "下载中",
            cls.TRANSCRIBING: "转录中",
            cls.SUMMARIZING: "总结中",
            cls.FORMATTING: "格式化中",
            cls.SAVING: "保存中",
            cls.SUCCESS: "完成",
            cls.FAILED: "失败",
            cls.CANCELLED: "已停止",
        }
        return desc_map.get(status, "未知状态")
