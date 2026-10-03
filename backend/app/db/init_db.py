from sqlalchemy import text

from app.db.models.models import Model
from app.db.models.providers import Provider
from app.db.models.video_tasks import VideoTask
from app.db.engine import get_engine, Base
from app.utils.logger import get_logger

logger = get_logger(__name__)

# create_all 只建新表、不加列；老库升级靠这里逐个补列。
# 值必须与对应 ORM 模型上的 server_default 保持一致。
_SQLITE_COLUMN_MIGRATIONS = [
    ("providers", "api_format", "VARCHAR NOT NULL DEFAULT 'chat'"),
]


def _migrate_sqlite_columns(engine):
    for table, column, ddl in _SQLITE_COLUMN_MIGRATIONS:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                if not rows:
                    continue  # 表还不存在，create_all 会按最新模型建
                if any(r[1] == column for r in rows):
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                conn.commit()
                logger.info(f"已迁移: {table}.{column} ({ddl})")
        except Exception as e:
            logger.warning(f"列迁移失败 ({table}.{column}): {e}")


def init_db():
    engine = get_engine()

    Base.metadata.create_all(bind=engine)
    if engine.url.get_backend_name() == "sqlite":
        _migrate_sqlite_columns(engine)
