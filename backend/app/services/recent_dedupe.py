"""/tasks/recent 的视频级去重选择（纯函数，零依赖，独立可测）。

用户诉求（2026-10-06）：生成历史里一个视频只能出现一次。
背景：失败风暴时代同一视频留下几十条 FAILED 状态文件，列表全量返回
后前端历史里出现几十张重复卡。

规则（前端 useTasksSync 的本地清理保持同一份语义）：
  - 按 video_id 分组，每组只保留一条：
      非终态(进行中) > SUCCESS > 其他终态(FAILED)；同优先级取 updated_at 新者。
    进行中盖住老 SUCCESS（重新生成时显示新卡）；重新生成失败后老 SUCCESS
    重新可见（FAILED 盖不住 SUCCESS）。
  - 没有 video_id 的条目不参与合并，原样保留。
  - 返回按 updated_at 倒序。
"""

from typing import Any, Dict, List

ACTIVE_STATUSES = {
    "PENDING",
    "PARSING",
    "DOWNLOADING",
    "TRANSCRIBING",
    "SUMMARIZING",
    "SAVING",
    "RUNNING",
}
_SUCCESS = "SUCCESS"


def _rank(item: Dict[str, Any]) -> int:
    status = item.get("status")
    if status in ACTIVE_STATUSES:
        return 2
    if status == _SUCCESS:
        return 1
    return 0


def _ts(item: Dict[str, Any]) -> int:
    return item.get("updated_at") or 0


def _video_id(item: Dict[str, Any]) -> str:
    return (item.get("video_id") or "").strip()


def pick_latest_per_video(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keep_without_video: List[Dict[str, Any]] = []
    by_video: Dict[str, Dict[str, Any]] = {}
    for it in items:
        vid = _video_id(it)
        if not vid:
            keep_without_video.append(it)
            continue
        cur = by_video.get(vid)
        if cur is None or (_rank(it), _ts(it)) > (_rank(cur), _ts(cur)):
            by_video[vid] = it
    merged = list(by_video.values()) + keep_without_video
    merged.sort(key=_ts, reverse=True)
    return merged
