from app.db.models.video_tasks import VideoTask
from app.db.engine import get_db
from app.utils.logger import get_logger
from app.utils.url_parser import normalize_video_id

from sqlalchemy.exc import IntegrityError

logger = get_logger(__name__)


# 插入任务（幂等：UI 重试复用同一 task_id，重复插入不再抛 UNIQUE 异常）
def insert_video_task(video_id: str, platform: str, task_id: str):    # 多 P 视频下载器 id 带 _pN 后缀，落库统一归一化为裸 bvid——
    # 否则自动化按裸 bvid 查「已有成功笔记」永远查不到（2026-10-06 潘通视频每轮重跑）
    video_id = normalize_video_id(video_id, platform) or video_id
    db = next(get_db())
    try:
        existing = db.query(VideoTask).filter_by(task_id=task_id).first()
        if existing:
            logger.info(f"Video task already exists, skip insert. task_id: {task_id}")
            return existing.task_id
        task = VideoTask(video_id=video_id, platform=platform, task_id=task_id)
        db.add(task)
        db.commit()
        db.refresh(task)
        logger.info(f"Video task inserted successfully. video_id: {video_id}, platform: {platform}, task_id: {task_id}")
        return task.task_id
    except IntegrityError:
        db.rollback()
        logger.info(f"Video task insert duplicated (race), ignored. task_id: {task_id}")
        return task_id
    except Exception as e:
        logger.error(f"Failed to insert video task: {e}")
    finally:
        db.close()


# 查询任务（最新一条）
def get_task_by_video(video_id: str, platform: str):
    video_id = normalize_video_id(video_id, platform) or video_id
    db = next(get_db())
    try:
        task = (
            db.query(VideoTask)
            .filter_by(video_id=video_id, platform=platform)
            .order_by(VideoTask.created_at.desc())
            .first()
        )
        if task:
            logger.info(f"Task found for video_id: {video_id} and platform: {platform}")
            return task.task_id
        else:
            logger.info(f"No task found for video_id: {video_id} and platform: {platform}")
            return None
    except Exception as e:
        logger.error(f"Failed to get task by video: {e}")
    finally:
        db.close()


# 删除任务
def delete_task_by_video(video_id: str, platform: str):
    video_id = normalize_video_id(video_id, platform) or video_id
    db = next(get_db())
    try:
        tasks = (
            db.query(VideoTask)
            .filter_by(video_id=video_id, platform=platform)
            .all()
        )
        for task in tasks:
            db.delete(task)
        db.commit()
        logger.info(f"Task(s) deleted for video_id: {video_id} and platform: {platform}")
    except Exception as e:
        logger.error(f"Failed to delete task by video: {e}")
    finally:
        db.close()


def upsert_task_video_id(task_id: str, video_id: str, platform: str):
    """修复存量任务记录：补/改 task_id 对应的 video_id。

    2026-10-24 的「未命名笔记」在元信息抓取失败时把 video_id=NULL 落库（甚至
    因 NOT NULL 约束插入失败），导致自动化按视频 ID 查不到已有笔记。刷新元
    信息时顺带把这条记录补齐，避免同一视频被反复重跑。
    """
    if not task_id or not video_id:
        return None
    video_id = normalize_video_id(video_id, platform) or video_id
    db = next(get_db())
    try:
        existing = db.query(VideoTask).filter_by(task_id=task_id).first()
        if existing:
            existing.video_id = video_id
            if platform:
                existing.platform = platform
            db.commit()
            logger.info(f"Video task meta repaired. video_id: {video_id}, task_id: {task_id}")
            return task_id
        task = VideoTask(video_id=video_id, platform=platform, task_id=task_id)
        db.add(task)
        db.commit()
        logger.info(f"Video task meta inserted. video_id: {video_id}, task_id: {task_id}")
        return task_id
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to upsert task video_id: {e}")
        return None
    finally:
        db.close()