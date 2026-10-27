#!/usr/bin/env python3
"""ClaudeSessionManager 的本地网页服务。

用法:
    python run.py                   # 启动并自动打开浏览器
    python -m session_manager web   # 同上（命令行入口）
    python run.py --port 8899 --no-browser

只监听 127.0.0.1，不对外网开放。删除默认进回收站，可恢复。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:  # 作为包的一部分（python -m session_manager / 打包后的 exe）
    from . import core as sm
except ImportError:  # 直接运行本文件
    import core as sm  # type: ignore


def _webui_root():
    """界面目录：PyInstaller 单文件包解包在 sys._MEIPASS，开发时就在本文件旁边。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for cand in (os.path.join(base, "webui"), base):
            if os.path.isfile(os.path.join(cand, "index.html")):
                return cand
    return os.path.join(HERE, "webui")


WEBUI = _webui_root()
UI_FILE = os.path.join(WEBUI, "index.html")
VENDOR = os.path.join(WEBUI, "vendor")
STATIC_TYPES = {".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8"}

ROOT = os.path.join(sm.HOME, ".claude")
_LINES = {}


# ---------- 公共 ----------

def ns(root=None, **kw):
    a = argparse.Namespace(
        root=root, project=None, here=False, sort="time", limit=0,
        force=False, purge=False, y=True, dry_run=False,
        older_than=None, bigger_than=None, keep_last=0,
    )
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def lines_of(s, compute=True):
    key = (s["path"], s["size"], s["mtime"])
    if key in _LINES:
        return _LINES[key]
    if not compute:
        return None
    try:
        n = sm.count_lines(s["path"])
    except OSError:
        return None
    if len(_LINES) > 5000:
        _LINES.clear()
    _LINES[key] = n
    return n


def project_label(s):
    return s["cwd"] or s["project_dir"]


def session_json(s, compute_lines=True):
    return {
        "id": s["session_id"],
        "title": s["title"] or "",
        "project": project_label(s),
        "project_short": sm.short_path(project_label(s), 2),
        "size": s["size"],
        "size_str": sm.human_size(s["size"]),
        "mtime": s["mtime"],
        "mtime_str": sm.fmt_mtime(s["mtime"]),
        "lines": lines_of(s, compute_lines),
        "live": bool(sm.current_sid()) and s["session_id"] == sm.current_sid(),
        "untitled": not s["title"],
    }


def scan_sessions(root, project=None):
    return sm.scan(ns(root), project=project or None)


def filter_rows(rows, q="", project="", untitled=False):
    q = (q or "").strip().lower()
    out = []
    for s in rows:
        if untitled and s["title"]:
            continue
        if project and not sm.match_project(s, project):
            continue
        if q:
            hay = " ".join([s["title"] or "", project_label(s), s["session_id"]]).lower()
            if q not in hay:
                continue
        out.append(s)
    return out


# ---------- 操作 ----------

def delete_sessions(root, ids, purge=False, force=False):
    """按完整会话 ID 删除。返回 JSON 友好的结果。"""
    rows = scan_sessions(root)
    by_id = {s["session_id"]: s for s in rows}
    cur = sm.current_sid()
    out = {"deleted": [], "skipped_live": [], "errors": [], "freed": 0, "purge": purge}
    targets = []
    for sid in ids:
        s = by_id.get(str(sid))
        if not s:
            out["errors"].append("未找到会话 %s" % str(sid)[:8])
            continue
        if sid == cur and not force:
            out["skipped_live"].append(sid)
            continue
        targets.append(s)
    if not targets:
        return out
    troot = sm.trash_root(ns(root))
    if not purge:
        os.makedirs(troot, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for s in targets:
        try:
            if purge:
                os.remove(s["path"])
            else:
                dest = os.path.join(troot, "%s_%s.jsonl" % (stamp, s["session_id"]))
                shutil.move(s["path"], dest)
                meta = {
                    "session_id": s["session_id"],
                    "original_path": s["path"],
                    "cwd": s["cwd"],
                    "title": s["title"],
                    "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                with open(dest + sm.META_SUFFIX, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            out["deleted"].append({"id": s["session_id"],
                                   "title": s["title"] or "(无标题)"})
            out["freed"] += s["size"]
        except OSError as e:
            out["errors"].append("%s: %s（可能正被其他 Claude 窗口占用）"
                                 % (s["title"] or s["session_id"][:8], e))
    out["freed_str"] = sm.human_size(out["freed"])
    return out


def trash_json(root):
    items = []
    for e in sm.trash_entries(ns(root)):
        m = e.get("meta") or {}
        items.append({
            "name": os.path.basename(e["path"]),
            "id": m.get("session_id") or "",
            "title": m.get("title") or "",
            "deleted_at": m.get("deleted_at") or sm.fmt_mtime(e["mtime"]),
            "original_path": m.get("original_path") or "",
            "project_short": sm.short_path(os.path.dirname(m.get("original_path") or ""), 2),
            "size": e["size"],
            "size_str": sm.human_size(e["size"]),
        })
    return items


def restore_entries(root, names):
    wanted = set(names or [])
    out = {"restored": [], "errors": []}
    for e in sm.trash_entries(ns(root)):
        if os.path.basename(e["path"]) not in wanted:
            continue
        dest = (e.get("meta") or {}).get("original_path")
        if not dest:
            out["errors"].append("缺少原始路径，无法恢复")
            continue
        if os.path.exists(dest):
            stem, ext = os.path.splitext(dest)
            dest = "%s_restored%s" % (stem, ext)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(e["path"], dest)
            meta_path = e["path"] + sm.META_SUFFIX
            if os.path.exists(meta_path):
                os.remove(meta_path)
            out["restored"].append(os.path.basename(dest))
        except OSError as ex:
            out["errors"].append("%s: %s" % (os.path.basename(e["path"]), ex))
    return out


def empty_trash(root):
    out = {"removed": 0, "freed": 0, "freed_str": "", "errors": []}
    for e in sm.trash_entries(ns(root)):
        for p in (e["path"], e["path"] + sm.META_SUFFIX):
            if not os.path.exists(p):
                continue
            try:
                os.remove(p)
                out["removed"] += 1
                if p == e["path"]:
                    out["freed"] += e["size"]
            except OSError as ex:
                out["errors"].append("%s: %s" % (os.path.basename(p), ex))
    out["freed_str"] = sm.human_size(out["freed"])
    return out


def clean_candidates(root, body):
    rows = scan_sessions(root, project=body.get("project") or None)
    days = float(body.get("older_days") or 0)
    bigger_mb = float(body.get("bigger_mb") or 0)
    keepn = int(body.get("keep_last") or 0)
    if not (days or bigger_mb or keepn):
        raise ValueError("至少设置一个条件（天数 / 大小 / 每项目保留数）")
    now = time.time()
    protected = {sm.current_sid()}
    if keepn:
        byproj = {}
        for s in rows:
            byproj.setdefault(s["project_dir"], []).append(s)
        for lst in byproj.values():
            lst.sort(key=lambda s: -s["mtime"])
            for s in lst[:keepn]:
                protected.add(s["session_id"])
    cands = []
    for s in rows:
        if s["session_id"] in protected:
            continue
        if days and now - s["mtime"] < days * 86400:
            continue
        if bigger_mb and s["size"] < bigger_mb * 1024 * 1024:
            continue
        cands.append(s)
    return cands


def stats_json(root):
    rows = scan_sessions(root)
    total = sum(s["size"] for s in rows)
    groups = {}
    for s in rows:
        groups.setdefault(project_label(s), []).append(s)
    items = []
    for p, lst in sorted(groups.items(),
                         key=lambda kv: -sum(x["size"] for x in kv[1])):
        latest = max(x["mtime"] for x in lst)
        items.append({
            "project": p,
            "project_short": sm.short_path(p, 3),
            "count": len(lst),
            "size": sum(x["size"] for x in lst),
            "size_str": sm.human_size(sum(x["size"] for x in lst)),
            "latest": latest,
            "latest_str": sm.fmt_mtime(latest),
            "live": any(x["session_id"] == sm.current_sid() for x in lst),
        })
    trash = trash_json(root)
    return {"total": len(rows), "total_size": total,
            "total_size_str": sm.human_size(total),
            "groups": items,
            "trash_count": len(trash),
            "trash_size_str": sm.human_size(sum(t["size"] for t in trash))}


def show_json(root, sid):
    rows = scan_sessions(root)
    s = next((x for x in rows if x["session_id"] == sid), None)
    if not s:
        raise ValueError("未找到会话")
    info = sm.deep_info(s)
    d = session_json(s, compute_lines=True)
    d.update({
        "path": s["path"],
        "cwd": s["cwd"],
        "users": info["users"],
        "asst": info["asst"],
        "cost": info["cost"],
        "dur": sm.fmt_dur(info["dur_ms"]) if info["dur_ms"] else "",
        "first_text": sm.trunc(info["first_text"], 400),
        "last_text": sm.trunc(info["last_text"], 400),
        "start_ts": sm.short_ts(info["start_ts"]),
        "end_ts": sm.short_ts(info["end_ts"]),
        "last_prompt": sm.trunc(s["last_prompt"], 400),
        "resume": "claude --resume %s" % s["session_id"],
    })
    return d


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "SessionManager/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默，避免刷屏
        pass

    # -- helpers --
    def _json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, text):
        raw = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, path, ctype):
        """发送 webui/vendor 下的静态资源（只走白名单路径）。"""
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _local(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 1_000_000:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _origin_ok(self):
        st = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if st and st not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.startswith(("http://127.0.0.1", "http://localhost",
                                  "http://[::1]"))

    # -- routes --
    def do_GET(self):
        if not self._local():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(UI_FILE, encoding="utf-8") as f:
                    return self._html(f.read())
            if u.path.startswith("/webui/vendor/"):
                name = u.path[len("/webui/vendor/"):]
                ext = os.path.splitext(name)[1].lower()
                if name and os.path.basename(name) == name and ext in STATIC_TYPES:
                    return self._static(os.path.join(VENDOR, name), STATIC_TYPES[ext])
                return self._json({"error": "not found"}, 404)
            if u.path == "/api/sessions":
                rows = scan_sessions(ROOT, project=qs.get("project"))
                untitled = qs.get("untitled") == "1"
                rows = filter_rows(rows, qs.get("q", ""), "", untitled)
                sort = qs.get("sort", "time")
                compute = sort == "lines"
                if sort == "size":
                    rows.sort(key=lambda s: (-s["size"], -s["mtime"]))
                elif sort == "lines":
                    for s in rows:
                        lines_of(s, True)
                    rows.sort(key=lambda s: (-(s["lines"] or 0), -s["mtime"]))
                else:
                    rows.sort(key=lambda s: (-s["mtime"], s["session_id"]))
                total_size = sum(s["size"] for s in rows)
                return self._json({
                    "total": len(rows),
                    "total_size": total_size,
                    "total_size_str": sm.human_size(total_size),
                    "live_id": sm.current_sid(),
                    "root": ROOT,
                    "sessions": [session_json(s, True) for s in rows],
                })
            if u.path == "/api/trash":
                items = trash_json(ROOT)
                return self._json({"items": items,
                                   "total_size": sum(i["size"] for i in items),
                                   "total_size_str": sm.human_size(
                                       sum(i["size"] for i in items))})
            if u.path == "/api/stats":
                return self._json(stats_json(ROOT))
            if u.path == "/api/show":
                return self._json(show_json(ROOT, qs.get("id", "")))
            return self._json({"error": "not found"}, 404)
        except (ValueError, sm.UserError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        if not self._local() or not self._origin_ok():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/delete":
                ids = body.get("ids") or []
                if not ids:
                    raise ValueError("没有选择会话")
                return self._json(delete_sessions(
                    ROOT, ids,
                    purge=bool(body.get("purge")),
                    force=bool(body.get("force"))))
            if u.path == "/api/restore":
                names = body.get("names") or []
                if not names:
                    raise ValueError("没有选择要恢复的文件")
                return self._json(restore_entries(ROOT, names))
            if u.path == "/api/empty-trash":
                return self._json(empty_trash(ROOT))
            if u.path == "/api/clean":
                cands = clean_candidates(ROOT, body)
                if body.get("dry_run"):
                    total = sum(s["size"] for s in cands)
                    return self._json({
                        "dry_run": True, "count": len(cands),
                        "total_size": total, "total_size_str": sm.human_size(total),
                        "sessions": [session_json(s, False) for s in cands],
                    })
                if not cands:
                    return self._json({"deleted": [], "freed": 0, "freed_str": "0B",
                                       "errors": [], "skipped_live": []})
                res = delete_sessions(ROOT, [s["session_id"] for s in cands],
                                      purge=bool(body.get("purge")))
                res["count"] = len(cands)
                return self._json(res)
            return self._json({"error": "not found"}, 404)
        except (ValueError, sm.UserError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)


def serve(root=None, port=8765, open_browser=True):
    global ROOT
    if root:
        ROOT = os.path.abspath(os.path.expanduser(root))
    sm.setup_stdio()
    if not os.path.isfile(UI_FILE):
        print("缺少界面文件: %s" % UI_FILE, file=sys.stderr)
        return 2
    httpd = None
    last_err = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError as e:
            last_err = e
    if not httpd:
        print("端口都被占用: %s" % last_err, file=sys.stderr)
        return 1
    url = "http://127.0.0.1:%d" % port
    print("会话管理页面: %s" % url)
    print("数据目录:   %s" % ROOT)
    print("按 Ctrl+C 退出（直接关闭这个窗口也可以）")
    duration = None
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="session_web", description="session-manager 网页界面（本地）")
    ap.add_argument("--root", default=None,
                    help="覆盖默认根目录 ~/.claude（测试用）")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    a = ap.parse_args(argv)
    return serve(a.root, a.port, not a.no_browser)


if __name__ == "__main__":
    sys.exit(main())
