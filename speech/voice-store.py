#!/usr/bin/env python3
"""Durable storage for qwentts cloned voices.

WHY THIS EXISTS: tts-server keeps its cloned-voice registry in an in-memory map
(g_voices in tts-server.cpp) with no save or load path, so every pod restart and
every Flux redeploy silently wipes every registered voice. Storing the clips only
in the browser fixes restarts but not devices — a voice cloned on a phone is
invisible from a desktop.

This sidecar owns the clips on disk and re-registers them with tts-server on
startup, so voices survive restarts AND are shared by every client.

It runs in the same pod as tts-server, so it reaches it on 127.0.0.1 and shares
the /models volume.

Endpoints
    POST /voices/register  {name, ref_text, wav_b64}  save then register
    GET  /voices/stored                                names held on disk
    DELETE /voices/<name>                              forget one
    GET  /healthz
"""

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STORE = os.environ.get("VOICE_STORE", "/models/qwen3-tts/voices")
UPSTREAM = os.environ.get("TTS_UPSTREAM", "http://127.0.0.1:8080")
PORT = int(os.environ.get("PORT", "8090"))

# Names become filenames and are echoed back to the model, so keep them boring.
SAFE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


def log(msg):
    print("[voice-store] %s" % msg, file=sys.stderr, flush=True)


def register_upstream(name, ref_text, wav_bytes):
    body = json.dumps({
        "name": name,
        "ref_text": ref_text or "",
        "wav_b64": base64.b64encode(wav_bytes).decode(),
    }).encode()
    req = urllib.request.Request(
        UPSTREAM + "/v1/audio/voices", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def stored_names():
    if not os.path.isdir(STORE):
        return []
    return sorted(f[:-4] for f in os.listdir(STORE) if f.endswith(".wav"))


def restore_all():
    """Re-push every stored clip once tts-server is answering.

    Runs in the background so a slow model load never blocks the HTTP listener.
    """
    for _ in range(180):
        try:
            urllib.request.urlopen(UPSTREAM + "/v1/models", timeout=5).read()
            break
        except Exception:
            time.sleep(5)
    else:
        log("upstream never became ready; skipping restore")
        return

    names = stored_names()
    if not names:
        log("no stored voices to restore")
        return
    ok = 0
    for name in names:
        try:
            with open(os.path.join(STORE, name + ".wav"), "rb") as f:
                wav = f.read()
            ref = ""
            tpath = os.path.join(STORE, name + ".txt")
            if os.path.exists(tpath):
                with open(tpath, "r", encoding="utf-8") as f:
                    ref = f.read().strip()
            register_upstream(name, ref, wav)
            ok += 1
        except Exception as e:
            log("restore of %r failed: %s" % (name, e))
    log("restored %d/%d voice(s)" % (ok, len(names)))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass  # the default logger writes a line per request to stderr

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        elif self.path == "/voices/stored":
            self._send(200, {"stored": stored_names()})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if not self.path.startswith("/voices/"):
            self._send(404, {"error": "not found"})
            return
        name = self.path.rsplit("/", 1)[-1]
        if not SAFE.match(name):
            self._send(400, {"error": "bad name"})
            return
        removed = False
        for ext in (".wav", ".txt"):
            p = os.path.join(STORE, name + ext)
            if os.path.exists(p):
                os.remove(p)
                removed = True
        self._send(200 if removed else 404, {"deleted": name, "found": removed})

    def do_POST(self):
        if self.path != "/voices/register":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": "bad json: %s" % e})
            return

        name = (req.get("name") or "").strip()
        if not SAFE.match(name):
            self._send(400, {"error": "name must match [A-Za-z0-9_-]{1,48}"})
            return
        try:
            wav = base64.b64decode(req.get("wav_b64") or "", validate=True)
        except Exception:
            self._send(400, {"error": "wav_b64 is not valid base64"})
            return
        if len(wav) < 1024:
            self._send(400, {"error": "wav payload too small"})
            return
        ref_text = (req.get("ref_text") or "").strip()

        # Register first: if tts-server rejects it there is no point storing it,
        # and the client gets the real upstream error rather than a false success.
        try:
            register_upstream(name, ref_text, wav)
        except urllib.error.HTTPError as e:
            self._send(e.code, {"error": e.read().decode("utf-8", "replace")[:300]})
            return
        except Exception as e:
            self._send(502, {"error": "upstream unreachable: %s" % e})
            return

        try:
            os.makedirs(STORE, exist_ok=True)
            # Write to a temp file then rename, so a crash mid-write cannot leave a
            # truncated wav that would fail every future restore.
            tmp = os.path.join(STORE, "." + name + ".wav.tmp")
            with open(tmp, "wb") as f:
                f.write(wav)
            os.replace(tmp, os.path.join(STORE, name + ".wav"))
            if ref_text:
                with open(os.path.join(STORE, name + ".txt"), "w", encoding="utf-8") as f:
                    f.write(ref_text)
        except Exception as e:
            # Registered but not persisted: say so rather than claim success.
            self._send(207, {"name": name, "registered": True, "stored": False,
                             "error": "could not persist: %s" % e})
            return

        log("registered and stored %r" % name)
        self._send(200, {"name": name, "registered": True, "stored": True})


if __name__ == "__main__":
    threading.Thread(target=restore_all, daemon=True).start()
    log("listening on :%d, store=%s, upstream=%s" % (PORT, STORE, UPSTREAM))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
