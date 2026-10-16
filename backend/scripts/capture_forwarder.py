"""临时诊断转发器：记录 BiliNote 活体发往 OpenCode Go 的原始请求，再忠实转发。

用途（2026-10-14 拒答之谜）：活体任务的批次请求收到罐头拒答，但外部复刻全部正常。
把 provider base_url 临时指向本器（http://127.0.0.1:9999/v1），捕获活体的确切
请求字节（headers + body 全文落盘），同时流式转发到 opencode.ai 保证任务继续。

用法：venv python backend/scripts/capture_forwarder.py
产物：/tmp/fwd_capture.log（摘要）+ /tmp/fwd_bodies/req_N.json（全文）
"""
import http.server
import json
import os
import socketserver
import time

import httpx

UPSTREAM = "https://opencode.ai"
LOG_PATH = r"C:\Users\popqco\AppData\Local\Temp\fwd_capture.log"
BODY_DIR = r"C:\Users\popqco\AppData\Local\Temp\fwd_bodies"
os.makedirs(BODY_DIR, exist_ok=True)

_seq = 0


def _log(rec: dict):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        global _seq
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        _seq += 1
        sid = _seq
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = None

        imgs = 0
        txt_len = 0
        instr = (payload or {}).get("instructions") or ""
        if isinstance(payload, dict):
            for item in (payload.get("input") or []):
                if not isinstance(item, dict):
                    continue
                for c in (item.get("content") or []):
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "input_image":
                        imgs += 1
                    elif c.get("type") == "input_text":
                        txt_len += len(c.get("text") or "")

        with open(os.path.join(BODY_DIR, f"req_{sid}.json"), "wb") as f:
            f.write(body)

        _log({
            "sid": sid, "t": time.strftime("%H:%M:%S"), "path": self.path,
            "body_len": len(body), "imgs": imgs, "txt_len": txt_len,
            "instructions_len": len(instr),
            "instructions_head": instr[:300],
            "model": (payload or {}).get("model"), "stream": (payload or {}).get("stream"),
            "reasoning": (payload or {}).get("reasoning"),
            "headers": {k: ("<masked>" if k.lower() == "authorization" else v)
                        for k, v in self.headers.items()},
            "input_summary": [
                {"role": it.get("role"), "types": [c.get("type") for c in (it.get("content") or []) if isinstance(c, dict)]}
                for it in (payload or {}).get("input") or [] if isinstance(it, dict)
            ],
        })

        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "transfer-encoding")}
        try:
            with httpx.Client(timeout=600, trust_env=False) as c:
                with c.stream("POST", UPSTREAM + self.path, content=body, headers=fwd_headers) as r:
                    self.send_response(r.status_code)
                    for k, v in r.headers.items():
                        if k.lower() in ("content-length", "transfer-encoding", "content-encoding", "connection"):
                            continue
                        self.send_header(k, v)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    total = 0
                    head = b""
                    for chunk in r.iter_raw():
                        total += len(chunk)
                        if len(head) < 3000:
                            head += chunk
                        self.wfile.write(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    _log({"sid": sid, "resp_status": r.status_code, "resp_bytes": total,
                          "resp_head": head.decode("utf-8", "ignore")[:1200]})
        except Exception as e:
            _log({"sid": sid, "fwd_error": str(e)[:300]})
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print("forwarder on 127.0.0.1:9999 → " + UPSTREAM, flush=True)
    with Server(("127.0.0.1", 9999), Handler) as srv:
        srv.serve_forever()
