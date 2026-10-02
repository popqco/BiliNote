# app/routers/export.py
# 笔记导出接口：POST /api/export_note
# 前端直接传 markdown 内容 + 标题（而不是 task_id 回读 note_results）：
# 多版本笔记只有前端 store 里有全部版本，后端文件只有最初版，
# 传内容才能保证「导出即当前所选版本」。
# 故意不走 R.success 信封、也不用 NoteError——统一 request 拦截器按 JSON 信封
# 解析会把 blob 误判，错误必须以真实 4xx/5xx 状态码抛出。

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.utils import export as export_utils
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ExportRequest(BaseModel):
    markdown: str
    title: str = "note"
    # pdf | docx（兼容 "word"）
    output_format: str


@router.post("/export_note")
def export_note(payload: ExportRequest):
    markdown = (payload.markdown or "").strip()
    if not markdown:
        raise HTTPException(status_code=400, detail="笔记内容为空，无法导出")
    fmt = (payload.output_format or "").strip().lower()
    title = export_utils.safe_filename(payload.title)

    try:
        if fmt == "pdf":
            path = export_utils.export_pdf(markdown, title)
            return FileResponse(path, media_type="application/pdf", filename=f"{title}.pdf")
        if fmt in ("docx", "word"):
            path = export_utils.export_docx(markdown, title)
            return FileResponse(path, media_type=_DOCX_MIME, filename=f"{title}.docx")
        raise HTTPException(status_code=400, detail=f"不支持的导出格式: {payload.output_format}（支持 pdf / docx）")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"导出失败 [{fmt}]: {e}")
        raise HTTPException(status_code=500, detail=f"导出失败: {e}")
