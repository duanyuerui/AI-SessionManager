#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH（DeepSeek Harness）：~/.dsh/sessions/<工作区>/session-<uuid>/session.v4.jsonl.zstd

会话正文是多个 zstd 帧首尾相接。解压依赖见 zstd.py；
解不了时退回到 ~/.dsh/storages/session_projcache 里的标题、项目、token 总量，
仍能列出和管理会话，只是 token 只能按会话最后活动时间归到某一天（标记为估算）。
"""
from __future__ import annotations

import json
import os
import re
import time

from .. import zstd
from .base import (FileTrash, Provider, TRASH_NAME, add_bucket,
                   add_usage_buckets, cached_entry, day_key, dir_size,
                   human_size, new_bucket, to_epoch)

SESSION_FILE = "session.v4.jsonl.zstd"
_SID_IN_DIR = re.compile(r"^(session-[0-9a-fA-F-]+)$")


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for it in content:
        if isinstance(it, dict) and it.get("type") in ("text", "input_text"):
            parts.append(it.get("text") or "")
    return "\n".join(p for p in parts if p)


def parse_usage(path):
    raw = zstd.decompress_file(path)
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    totals = new_bucket()
    days = {}
    models = {}
    mdays = {}
    hours = {}
    mhours = {}
    sid = ""
    cwd = ""
    title = ""
    users = 0
    first_text = last_text = ""
    first_ts = last_ts = None
    seen = set()
    model = ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        ts = to_epoch(d.get("time"))
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        data = d.get("data") if isinstance(d.get("data"), dict) else {}
        if t == "session":
            sid = sid or d.get("id") or ""
            cwd = cwd or d.get("cwd") or ""
        elif t == "session/title":
            title = data.get("title") or title
        elif t == "model/selection":
            model = data.get("model") or data.get("modelId") or model
        elif t == "request/header":
            cfg = (data.get("header") or {}).get("config") or {}
            model = cfg.get("model") or data.get("model") or model
        elif t == "user/message":
            src = (data.get("source") or {}).get("kind") or ""
            txt = _content_text(data.get("content"))
            if src in ("user", "", "user-input") and txt.strip():
                users += 1
                if not first_text:
                    first_text = txt
                last_text = txt
        elif t == "assistant/message":
            u = data.get("usage")
            seq = d.get("seq")
            if not isinstance(u, dict) or (seq is not None and seq in seen):
                continue
            if seq is not None:
                seen.add(seq)
            b = new_bucket()
            inp = int(u.get("inputTokens") or 0)
            cr = int(u.get("cacheReadTokens") or 0)
            b["input"] = max(0, inp)
            b["cache_read"] = max(0, cr)
            b["cache_write"] = int(u.get("cacheWriteTokens") or 0)
            b["output"] = int(u.get("outputTokens") or 0)
            b["reasoning"] = int(u.get("reasoningTokens") or 0)
            b["requests"] = 1
            add_bucket(totals, b)
            add_usage_buckets(days, models, mdays, ts, model, b, hours, mhours)
    if not sid:
        sid = os.path.basename(os.path.dirname(path))
    return {
        "sid": sid, "totals": totals, "days": days, "models": models,
        "mdays": mdays,
        "hours": hours, "mhours": mhours,
        "meta": {"cwd": cwd, "title": title, "users": users,
                 "first_text": first_text[:400], "last_text": last_text[:400],
                 "first_ts": first_ts, "last_ts": last_ts},
    }


class DshProvider(Provider):
    key = "dsh"
    name = "DSH"
    color = "#eda100"
    hint = "~/.dsh/sessions"

    @property
    def sessions_root(self):
        return os.path.join(self.root, "sessions")

    @property
    def projcache_root(self):
        return os.path.join(self.root, "storages", "session_projcache", "sessions")

    def trash(self):
        return FileTrash(os.path.join(self.root, TRASH_NAME))

    def available(self):
        return os.path.isdir(self.sessions_root) or super().available()

    # -- 元数据缓存（标题 / cwd / token 总量） --
    def _projcache(self):
        out = {}
        root = self.projcache_root
        if not os.path.isdir(root):
            return out
        for fn in os.listdir(root):
            if not fn.endswith(".json"):
                continue
            sid = fn[:-5]
            try:
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            rec = (d or {}).get("record") or {}
            rows = rec.get("rows") or {}
            ident = rec.get("identity") or {}

            def val(k):
                return ((rows.get(k) or {}).get("val"))

            usage = val("tokenUsage") or {}
            totals = usage.get("totals") or {}
            b = new_bucket()
            b["input"] = int(totals.get("uncachedInputTokens") or 0)
            b["cache_read"] = int(totals.get("cacheReadTokens") or 0)
            b["cache_write"] = int(totals.get("cacheWriteTokens") or 0)
            b["output"] = int(totals.get("outputTokens") or 0)
            stats = val("sessionStats") or {}
            out[sid] = {
                "title": val("title") or "",
                "cwd": ident.get("cwd") or val("workingDirectory") or "",
                "created": to_epoch(ident.get("createdAt")),
                "tokens": b,
                "turns": int(stats.get("turns") or 0),
            }
        return out

    def session_dirs(self):
        out = []
        root = self.sessions_root
        if not os.path.isdir(root):
            return out
        for proj in os.listdir(root):
            ppath = os.path.join(root, proj)
            if not os.path.isdir(ppath):
                continue
            for sid in os.listdir(ppath):
                d = os.path.join(ppath, sid)
                f = os.path.join(d, SESSION_FILE)
                if os.path.isdir(d) and os.path.isfile(f):
                    out.append((sid, proj, d, f))
        return out

    def _rows(self):
        from .. import usage
        cache = usage.load_cache()
        meta = self._projcache()
        rows = []
        for sid, _proj, d, f in self.session_dirs():
            m = meta.get(sid) or {}
            try:
                st = os.stat(f)
            except OSError:
                continue
            entry = usage.entry_for(cache, self.key, f) or {}
            emeta = entry.get("meta") or {}
            tokens = entry.get("totals") or m.get("tokens")
            rows.append({
                "tool": self.key, "id": sid,
                "title": emeta.get("title") or m.get("title") or "",
                "project": emeta.get("cwd") or m.get("cwd") or _decode_proj(_proj),
                "cwd": emeta.get("cwd") or m.get("cwd") or _decode_proj(_proj),
                "size": dir_size(d), "mtime": st.st_mtime, "path": d,
                "live": False, "kind": "file",
                "models": sorted((entry.get("models") or {}).keys()),
                "tokens": tokens,
                "messages": emeta.get("users"),
                "turns": m.get("turns"),
                "first_ts": emeta.get("first_ts") or m.get("created"),
                "last_ts": emeta.get("last_ts"),
                "note": "" if entry else "用量为估算",
            })
        rows.sort(key=lambda s: (-s["mtime"], s["id"]))
        return rows

    def sessions(self):
        return self._rows()

    def quick_stats(self):
        n = size = 0
        for _sid, _proj, d, _f in self.session_dirs():
            n += 1
            size += dir_size(d)
        return n, size

    def detail(self, s):
        from .. import usage
        cache = usage.load_cache()
        entry = cached_entry(cache, self.key, os.path.join(s["path"], SESSION_FILE),
                             parse_usage) or {}
        meta = entry.get("meta") or {}
        return {
            "users": meta.get("users") or 0,
            "asst": None,
            "first_text": meta.get("first_text") or "",
            "last_text": meta.get("last_text") or "",
            "start_ts": _fmt(meta.get("first_ts")),
            "end_ts": _fmt(meta.get("last_ts")),
            "lines": None,
            "tokens": entry.get("totals"),
            "models": sorted((entry.get("models") or {}).keys()),
            "zstd": zstd.available(),
        }

    def resume_cmd(self, s):
        return "dsh --resume %s" % s["id"]

    def delete(self, ids, purge=False, force=False):
        rows = {s["id"]: s for s in self.sessions()}
        out = {"deleted": [], "skipped_live": [], "errors": [], "freed": 0,
               "purge": purge}
        trash = self.trash()
        for sid in ids:
            s = rows.get(str(sid))
            if not s:
                out["errors"].append("未找到会话 %s" % str(sid)[:8])
                continue
            try:
                if purge:
                    import shutil
                    shutil.rmtree(s["path"])
                else:
                    trash.move(s["path"], s["id"], {
                        "session_id": s["id"], "original_path": s["path"],
                        "cwd": s["cwd"], "title": s["title"],
                        "tool": self.key,
                        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                self._drop_projcache(s["id"])
                out["deleted"].append({"id": s["id"], "title": s["title"] or "(无标题)"})
                out["freed"] += s["size"]
            except OSError as e:
                out["errors"].append("%s: %s" % (s["title"] or s["id"][:8], e))
        out["freed_str"] = human_size(out["freed"])
        return out

    def _drop_projcache(self, sid):
        p = os.path.join(self.projcache_root, sid + ".json")
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    def trash_list(self):
        out = []
        for e in self.trash().entries():
            m = e.get("meta") or {}
            out.append({
                "token": e["token"], "id": m.get("session_id") or "",
                "title": m.get("title") or "",
                "deleted_at": m.get("deleted_at") or _fmt_mtime(e["mtime"]),
                "original_path": m.get("original_path") or "",
                "size": e["size"], "size_str": human_size(e["size"]),
            })
        return out

    def trash_restore(self, tokens):
        out = {"restored": [], "errors": []}
        for t in tokens:
            try:
                dest, err = self.trash().restore(t)
            except ValueError as e:
                out["errors"].append(str(e))
                continue
            if err:
                out["errors"].append(err)
            else:
                out["restored"].append(os.path.basename(dest))
        return out

    def trash_empty(self):
        return self.trash().empty()

    def usage_entries(self, cache):
        entries = []
        paths = set()
        meta = self._projcache()
        for sid, _proj, _d, f in self.session_dirs():
            paths.add(f)
            e = cached_entry(cache, self.key, f, parse_usage)
            if e:
                entries.append(e)
                continue
            # 解压不可用：把 projcache 的总量按最后活动时间归到一天
            m = meta.get(sid) or {}
            b = m.get("tokens") or new_bucket()
            if any(b.values()):
                try:
                    ts = os.path.getmtime(f)
                except OSError:
                    ts = None
                dk = day_key(ts) or ""
                entries.append({
                    "sid": sid, "totals": b,
                    "days": {dk: b} if dk else {},
                    "models": {"deepseek (估算)": b},
                })
        self.last_paths = paths
        return entries


def _decode_proj(name):
    """--D-Company-LIS~9879~76EE~76F8~5173-- → D:\\Company\\LIS项目相关（尽力而为）"""
    s = name
    if s.startswith("--"):
        s = s[2:]
    if s.endswith("--"):
        s = s[:-2]

    def rep(m):
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)

    s = re.sub(r"~([0-9A-Fa-f]{4})", rep, s)
    if len(s) >= 2 and s[1] == "-":
        s = s[0] + ":\\" + s[2:]
    return s.replace("-", "\\")


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _fmt_mtime(ts):
    return time.strftime("%y-%m-%d %H:%M", time.localtime(ts))
