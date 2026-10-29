#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex：~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl

标题优先取 ~/.codex/session_index.jsonl 里的 thread_name，
没有就用第一条真正的用户消息。token 来自 event_msg/token_count 的增量。
"""
from __future__ import annotations

import json
import os
import re
import time

from .base import (FileTrash, Provider, TRASH_NAME, add_bucket, cached_entry,
                   day_key, human_size, new_bucket, to_epoch)

_SID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")

_JUNK_PREFIX = ("<", "# AGENTS.md", "## ", "AGENTS.md instructions")


def real_user_text(s):
    s = (s or "").strip()
    if not s:
        return ""
    for p in _JUNK_PREFIX:
        if s.startswith(p):
            return ""
    return s


def _json(obj, path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError):
        return obj


def parse_usage(path):
    totals = new_bucket()
    days = {}
    models = {}
    sid = ""
    cwd = ""
    model = ""
    users = 0
    first_text = last_text = ""
    first_ts = last_ts = None
    prev_total = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            ts = to_epoch(d.get("timestamp"))
            t = d.get("type")
            pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            if t == "session_meta":
                sid = sid or pl.get("id") or pl.get("session_id") or ""
                cwd = cwd or pl.get("cwd") or ""
            elif t == "turn_context":
                model = pl.get("model") or model
            elif t == "response_item":
                if pl.get("type") == "message" and pl.get("role") == "user":
                    txt = ""
                    for c in pl.get("content") or []:
                        if isinstance(c, dict) and c.get("type") in (
                                "input_text", "text"):
                            txt += c.get("text") or ""
                    txt = real_user_text(txt)
                    if txt:
                        users += 1
                        if not first_text:
                            first_text = txt
                        last_text = txt
            elif t == "event_msg" and pl.get("type") == "token_count":
                info = pl.get("info") or {}
                # last_token_usage 是本次请求的增量；旧版本没有时用累计值差分
                u = info.get("last_token_usage")
                if not isinstance(u, dict):
                    total_u = info.get("total_token_usage") or {}
                    if prev_total is None:
                        u = total_u
                    else:
                        u = {k: int(total_u.get(k) or 0)
                             - int(prev_total.get(k) or 0)
                             for k in ("input_tokens", "cached_input_tokens",
                                       "cache_write_input_tokens", "output_tokens",
                                       "reasoning_output_tokens", "total_tokens")}
                    prev_total = total_u
                b = new_bucket()
                inp = max(0, int(u.get("input_tokens") or 0))
                cached = max(0, int(u.get("cached_input_tokens") or 0))
                b["input"] = max(0, inp - cached)
                b["cache_read"] = cached
                b["cache_write"] = max(0, int(u.get("cache_write_input_tokens") or 0))
                b["output"] = max(0, int(u.get("output_tokens") or 0))
                b["reasoning"] = max(0, int(u.get("reasoning_output_tokens") or 0))
                b["requests"] = 1 if int(u.get("total_tokens") or 0) else 0
                add_bucket(totals, b)
                dk = day_key(ts) or ""
                if dk:
                    add_bucket(days.setdefault(dk, new_bucket()), b)
                add_bucket(models.setdefault(model or "unknown", new_bucket()), b)
    if not sid:
        m = _SID_RE.search(os.path.basename(path))
        sid = m.group(1) if m else os.path.splitext(os.path.basename(path))[0]
    return {
        "sid": sid, "totals": totals, "days": days, "models": models,
        "meta": {"cwd": cwd, "users": users, "model": model,
                 "first_text": first_text[:400], "last_text": last_text[:400],
                 "first_ts": first_ts, "last_ts": last_ts},
    }


class CodexProvider(Provider):
    key = "codex"
    name = "Codex"
    color = "#1baf7a"
    hint = "~/.codex/sessions"

    @property
    def sessions_root(self):
        return os.path.join(self.root, "sessions")

    @property
    def index_path(self):
        return os.path.join(self.root, "session_index.jsonl")

    def trash(self):
        return FileTrash(os.path.join(self.root, TRASH_NAME))

    def available(self):
        return os.path.isdir(self.sessions_root) or super().available()

    def _index(self):
        out = {}
        try:
            with open(self.index_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("id") and d.get("thread_name"):
                        out[d["id"]] = d["thread_name"]
        except OSError:
            pass
        return out

    def files(self):
        out = []
        root = self.sessions_root
        if not os.path.isdir(root):
            return out
        for dirpath, _dirs, fnames in os.walk(root):
            for fn in fnames:
                if fn.startswith("rollout-") and fn.endswith(".jsonl"):
                    out.append(os.path.join(dirpath, fn))
        out.sort()
        return out

    def _rows(self):
        from .. import usage
        cache = usage.load_cache()
        rows = []
        for path in self.files():
            e = cached_entry(cache, self.key, path, parse_usage) or {}
            meta = e.get("meta") or {}
            try:
                st = os.stat(path)
            except OSError:
                continue
            sid = e.get("sid") or os.path.splitext(os.path.basename(path))[0]
            rows.append({
                "tool": self.key, "id": sid,
                "title": "", "project": meta.get("cwd") or "",
                "cwd": meta.get("cwd") or "",
                "size": st.st_size, "mtime": st.st_mtime, "path": path,
                "live": False, "kind": "file",
                "models": sorted((e.get("models") or {}).keys()),
                "tokens": e.get("totals"),
                "messages": meta.get("users"),
                "first_ts": meta.get("first_ts"),
                "last_ts": meta.get("last_ts"),
                "_first_text": meta.get("first_text") or "",
            })
        titles = self._index()
        for r in rows:
            r["title"] = titles.get(r["id"]) or (
                r["_first_text"][:80] if r["_first_text"] else "")
            r.pop("_first_text", None)
        rows.sort(key=lambda s: (-s["mtime"], s["id"]))
        self.last_paths = set(self.files())
        return rows

    def sessions(self):
        return self._rows()

    def quick_stats(self):
        n = size = 0
        for p in self.files():
            try:
                size += os.path.getsize(p)
                n += 1
            except OSError:
                pass
        return n, size

    def detail(self, s):
        from .. import usage
        cache = usage.load_cache()
        e = cached_entry(cache, self.key, s["path"], parse_usage) or {}
        meta = e.get("meta") or {}
        return {
            "users": meta.get("users") or 0,
            "asst": None,
            "first_text": meta.get("first_text") or "",
            "last_text": meta.get("last_text") or "",
            "start_ts": _fmt(meta.get("first_ts")),
            "end_ts": _fmt(meta.get("last_ts")),
            "lines": _count_lines(s["path"]),
            "tokens": e.get("totals"),
            "models": sorted((e.get("models") or {}).keys()),
        }

    def resume_cmd(self, s):
        return "codex resume %s" % s["id"]

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
                    os.remove(s["path"])
                else:
                    trash.move(s["path"], os.path.basename(s["path"]), {
                        "session_id": s["id"], "original_path": s["path"],
                        "cwd": s["cwd"], "title": s["title"],
                        "tool": self.key,
                        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                out["deleted"].append({"id": s["id"], "title": s["title"] or "(无标题)"})
                out["freed"] += s["size"]
            except OSError as e:
                out["errors"].append("%s: %s" % (s["title"] or s["id"][:8], e))
        if purge:
            self._drop_index({d["id"] for d in out["deleted"]})
        out["freed_str"] = _human(out["freed"])
        return out

    def _drop_index(self, ids):
        """永久删除时顺手把 session_index.jsonl 里的条目去掉。"""
        if not ids or not os.path.exists(self.index_path):
            return
        keep = []
        try:
            with open(self.index_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        keep.append(line)
                        continue
                    if d.get("id") in ids:
                        continue
                    keep.append(line)
            with open(self.index_path, "w", encoding="utf-8") as f:
                f.writelines(keep)
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
                "size": e["size"], "size_str": _human(e["size"]),
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
        paths = set(self.files())
        for path in paths:
            e = cached_entry(cache, self.key, path, parse_usage)
            if e:
                entries.append(e)
        self.last_paths = paths
        return entries


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _fmt_mtime(ts):
    return time.strftime("%y-%m-%d %H:%M", time.localtime(ts))


def _human(n):
    from .base import human_size
    return human_size(n)


def _count_lines(path):
    n = 0
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                n += chunk.count(b"\n")
    except OSError:
        return 0
    return n
