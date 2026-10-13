"""一次性补偿：给手动提交的 PENDING 任务补标题/封面。

2026-10-13：手动单条提交路径写 PENDING 时不含 audio_meta，卡片一直「未命名笔记」
直到任务开始。本脚本用 app 自身的 fetch_video_meta（YouTube 走用户配置的代理）
给现有排队任务补上 audio_meta，前端轮询 /tasks/recent 后即可显示。

用法（从安装目录跑，保证 config/proxy.json 等相对路径解析正确）：
  cd "D:/Program Files/BiliNote"
  <venv-python> backfill_pending_meta.py
"""
import json
import io
import os
import sys
import time
import glob

INSTALL_DIR = r"D:\Program Files\BiliNote"
SRC_BACKEND = r"D:\C-Moved\Codex\2026-09-24\new-chat\work\BiliNote-src-2\backend"

os.chdir(INSTALL_DIR)
# 防止 shell 继承的代理 env 干扰（app 自己从 config/proxy.json 取代理）
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)
sys.path.insert(0, SRC_BACKEND)

from app.services.video_meta import fetch_video_meta  # noqa: E402

OUT = "note_results"
targets = []
for f in glob.glob(os.path.join(OUT, "*.status.json")):
    stem = os.path.basename(f)[: -len(".status.json")]
    if "_" in stem:
        continue
    try:
        d = json.load(io.open(f, encoding="utf-8"))
    except Exception:
        continue
    if d.get("status") != "PENDING":
        continue
    am = d.get("audio_meta") or {}
    if am.get("title"):
        continue
    if not d.get("video_url") or not d.get("platform"):
        continue
    targets.append((f, d))

print(f"待补元信息 PENDING 任务: {len(targets)}", flush=True)
ok = fail = 0
for f, d in targets:
    url, plat = d["video_url"], d["platform"]
    meta = None
    try:
        meta = fetch_video_meta(url, plat)
    except Exception as e:
        print(f"  {os.path.basename(f)[:8]} fetch 异常: {e}", flush=True)
    if not meta or not meta.get("title"):
        fail += 1
        print(f"  {os.path.basename(f)[:8]} 元信息获取失败 ({plat})", flush=True)
        continue
    # 重读后再写：任务若已开始跑，其自身写入的 audio_meta 更权威
    try:
        cur = json.load(io.open(f, encoding="utf-8"))
        if (cur.get("audio_meta") or {}).get("title"):
            continue
        if cur.get("status") != "PENDING":
            continue
        cur["audio_meta"] = {
            "title": meta.get("title"),
            "cover_url": meta.get("cover_url"),
            "video_id": meta.get("video_id") or d.get("video_id"),
            "duration": meta.get("duration"),
            "platform": plat,
        }
        tmp = f + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(json.dumps(cur, ensure_ascii=False, indent=2))
        os.replace(tmp, f)
        ok += 1
        print(f"  {os.path.basename(f)[:8]} ✓ {str(meta.get('title'))[:34]}", flush=True)
    except Exception as e:
        fail += 1
        print(f"  {os.path.basename(f)[:8]} 写入失败: {e}", flush=True)

print(f"完成: 成功 {ok} / 失败 {fail} / 共 {len(targets)}", flush=True)
