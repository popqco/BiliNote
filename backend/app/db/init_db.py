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
    ("providers", "extra_headers", "VARCHAR DEFAULT '{}'"),
    ("providers", "thinking_effort", "VARCHAR DEFAULT ''"),
    ("models", "thinking_effort", "VARCHAR DEFAULT ''"),
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


def _migrate_provider_effort_to_models(engine):
    """思考强度从供应商级迁到模型级（2026-10-11 定版）。

    把每个供应商上的 thinking_effort 复制到它名下全部模型行（模型行已有值
    则不覆盖），然后清空供应商列。跑一次即为稳态：之后供应商列恒为空，
    函数开头即返回。兼容 models.provider_id 混存字符串 UUID 与数字两种历史形态。
    """
    try:
        with engine.connect() as conn:
            prov_rows = conn.execute(
                text(
                    "SELECT id, thinking_effort FROM providers "
                    "WHERE thinking_effort IS NOT NULL AND thinking_effort != ''"
                )
            ).fetchall()
            if not prov_rows:
                return
            for pid, effort in prov_rows:
                for val in dict.fromkeys([pid, str(pid)]):
                    conn.execute(
                        text(
                            "UPDATE models SET thinking_effort = :e "
                            "WHERE provider_id = :p AND (thinking_effort IS NULL OR thinking_effort = '')"
                        ),
                        {"e": effort, "p": val},
                    )
            conn.execute(text("UPDATE providers SET thinking_effort = ''"))
            conn.commit()
            logger.info(
                f"思考强度已从供应商级迁到模型级：{len(prov_rows)} 个供应商的设置已复制到其模型行"
            )
    except Exception as e:
        logger.warning(f"thinking_effort 供应商→模型数据迁移失败（可忽略）: {e}")


def init_db():
    engine = get_engine()

    Base.metadata.create_all(bind=engine)
    if engine.url.get_backend_name() == "sqlite":
        _migrate_sqlite_columns(engine)
        _migrate_provider_effort_to_models(engine)
