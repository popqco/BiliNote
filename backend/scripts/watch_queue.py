"""过夜巡检快照：一条命令看清 BiliNote 队列与近期故障。

读安装目录的 note_results 状态文件 + logs/app.log 尾部，输出：
- 各状态计数（PENDING/活跃/终态）
- 活跃任务的 vid/阶段/消息（卡在哪一步）
- 最近 2 小时 FAILED 的任务及原因
- app.log 尾部的 ERROR/WARNING（去重后前若干条）

用法：<venv-python> backend/scripts/watch_queue.py
"""
import json
import io
import os
import re
import sys
import glob
import time
from collections import Counter, defaultdict

INSTALL_DIR = r"D:\Program Files\BiliNote"
NOTE_DIR = os.path.join(INSTALL_DIR, "note_results")
LOG_FILE = os.path.join(INSTALL_DIR, "logs", "app.log")

ACTIVE = ("DOWNLOADING", "TRANSCRIBING", "SUMMARIZING", "SAVING", "PENDING")
now = time.time()

counts = Counter()
active_rows = []
failed_rows = []
for f in glob.glob(os.path.join(NOTE_DIR, "*.status.json")):
    stem = os.path.basename(f)[: -len(".status.json")]
    if "_" in stem:
        continue
    try:
        d = json.load(io.open(f, encoding="utf-8"))
    except Exception:
        continue
    st = d.get("status") or "?"
    counts[st] += 1
    age_min = (now - os.path.getmtime(f)) / 60
    if st in ACTIVE:
        active_rows.append((age_min, stem[:8], st, d.get("video_id"), str(d.get("message") or "")[:80],
                            (d.get("audio_meta") or {}).get("title", "")[:24]))
    elif st == "FAILED" and age_min < 120:
        failed_rows.append((age_min, stem[:8], d.get("video_id"), str(d.get("message") or "")[:110],
                            (d.get("audio_meta") or {}).get("title", "")[:24]))

print("== 状态计数 ==")
for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
    print(f"  {k}: {v}")

active_rows.sort()
print(f"\n== 活跃/排队（{len(active_rows)}）==")
for age, tid, st, vid, msg, title in active_rows:
    print(f"  {tid} {st:<12} age={age:>5.0f}m vid={vid} {title} | {msg}")

failed_rows.sort()
print(f"\n== 近 2 小时 FAILED（{len(failed_rows)}）==")
for age, tid, vid, msg, title in failed_rows:
    print(f"  {tid} age={age:.0f}m vid={vid} {title} | {msg}")

print("\n== app.log 尾部 ERROR/WARNING ==")
try:
    lines = io.open(LOG_FILE, encoding="utf-8", errors="ignore").read().splitlines()[-400:]
    hits = [l for l in lines if ("[ERROR]" in l or "[WARNING]" in l)]
    seen = Counter()
    for l in hits[-40:]:
        key = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "", l)[:120]
        seen[key] += 1
    for k, n in seen.most_common(15):
        print(f"  x{n} {k}")
except Exception as e:
    print(f"  读日志失败: {e}")
