#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy：会话索引在 ~/.workbuddy/workbuddy.db，正文在 ~/.workbuddy/projects/<项目>/<id>.jsonl

删除沿用应用自己的软删除（sessions.deleted_at），恢复就是把时间清空。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .base import (Provider, add_bucket, add_usage_buckets, cached_entry,
                   day_key, human_size, new_bucket, to_epoch)

_JUNK = ("<", "# AGENTS.md", "## ", "AGENTS.md instructions")


def _db_path(root):
    return os.path.join(root, "workbuddy.db")


def _connect(root, readonly=True):
    p = _db_path(root)
    if not os.path.isfile(p):
        raise OSError("找不到 %s" % p)
    if readonly:
        uri = "file:///" + p.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
    else:
        con = sqlite3.connect(p, timeout=10)
        con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for it in content:
        if isinstance(it, dict) and it.get("type") in (
                "input_text", "text", "output_text"):
            out.append(it.get("text") or "")
    return "\n".join(x for x in out if x)


def _real_user_text(s):
    s = (s or "").strip()
    if not s:
        return ""
    for p in _JUNK:
        if s.startswith(p):
            return ""
    if s.startswith("Caveat:"):
        return ""
    return s


def parse_usage(path):
    totals = new_bucket()
    days = {}
    models = {}
    mdays = {}
    hours = {}
    mhours = {}
    users = 0
    title = ""
    first_text = last_text = ""
    first_ts = last_ts = None
    seen = set()
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
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            t = d.get("type")
            if t == "ai-title":
                title = d.get("aiTitle") or title
                continue
            if t == "message" and d.get("role") == "user":
                txt = _real_user_text(_content_text(d.get("content")))
                if txt:
                    users += 1
                    if not first_text:
                        first_text = txt
                    last_text = txt
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            pd = d.get("providerData") if isinstance(d.get("providerData"), dict) else {}
            u = msg.get("usage")
            if not isinstance(u, dict):
                u = pd.get("usage") if isinstance(pd.get("usage"), dict) else None
            if not isinstance(u, dict):
                continue
            key = pd.get("messageId") or msg.get("id") or d.get("id")
            if key in seen:
                continue
            seen.add(key)
            raw = pd.get("rawUsage") if isinstance(pd.get("rawUsage"), dict) else {}
            inp = raw.get("prompt_tokens")
            if inp is None:
                inp = u.get("input_tokens", u.get("inputTokens", 0))
            cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
            if cached is None:
                cached = u.get("cache_read_input_tokens",
                               (u.get("inputTokensDetails") or [{}])[0].get("cached_tokens", 0))
            out_tok = raw.get("completion_tokens")
            if out_tok is None:
                out_tok = u.get("output_tokens", u.get("outputTokens", 0))
            reason = (raw.get("completion_tokens_details") or {}).get("reasoning_tokens")
            if reason is None:
                reason = (u.get("outputTokensDetails") or [{}])[0].get("reasoning_tokens", 0)
            b = new_bucket()
            inp = int(inp or 0)
            cached = int(cached or 0)
            b["input"] = max(0, inp - cached)
            b["cache_read"] = cached
            b["cache_write"] = int(u.get("cache_creation_input_tokens") or 0)
            b["output"] = int(out_tok or 0)
            b["reasoning"] = int(reason or 0)
            b["requests"] = 1
            add_bucket(totals, b)
            model = pd.get("model") or msg.get("model") or "unknown"
            add_usage_buckets(days, models, mdays, ts, model, b, hours, mhours)
    sid = os.path.splitext(os.path.basename(path))[0]
    return {
        "sid": sid, "totals": totals, "days": days, "models": models,
        "mdays": mdays,
        "hours": hours, "mhours": mhours,
        "meta": {"title": title, "users": users,
                 "first_text": first_text[:400], "last_text": last_text[:400],
                 "first_ts": first_ts, "last_ts": last_ts},
    }


class WorkbuddyProvider(Provider):
    key = "workbuddy"
    name = "WorkBuddy"
    color = "#e87ba4"
    hint = "~/.workbuddy"
    kind = "soft"

    @property
    def db_path(self):
        return _db_path(self.root)

    @property
    def projects_root(self):
        return os.path.join(self.root, "projects")

    def available(self):
        return os.path.isfile(self.db_path)

    def _file_map(self):
        out = {}
        root = self.projects_root
        if not os.path.isdir(root):
            return out
        for proj in os.listdir(root):
            ppath = os.path.join(root, proj)
            if not os.path.isdir(ppath):
                continue
            for fn in os.listdir(ppath):
                if fn.endswith(".jsonl") and not fn.endswith(".file-rollback.ndjson"):
                    out[fn[:-6]] = os.path.join(ppath, fn)
        return out

    def _rows(self, deleted=False):
        con = _connect(self.root)
        try:
            sql = ("select id, cwd, title, custom_title, created_at, updated_at, "
                   "last_activity_at, deleted_at, model, is_background_automation "
                   "from sessions where deleted_at is %s "
                   "order by coalesce(last_activity_at, updated_at) desc"
                   % ("not null" if deleted else "null"))
            rows = [dict(r) for r in con.execute(sql)]
        finally:
            con.close()
        files = self._file_map()
        from .. import usage
        cache = usage.load_cache()
        out = []
        for r in rows:
            path = files.get(r["id"]) or ""
            size = 0
            mtime = 0
            if path:
                try:
                    st = os.stat(path)
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    pass
            if not mtime:
                mtime = (r.get("last_activity_at") or r.get("updated_at") or 0) / 1000.0
            entry = (usage.entry_for(cache, self.key, path) if path else None) or {}
            meta = entry.get("meta") or {}
            if deleted and r.get("deleted_at"):
                mtime = (r.get("deleted_at") or 0) / 1000.0
            out.append({
                "tool": self.key, "id": r["id"],
                "title": r.get("custom_title") or r.get("title") or meta.get("title") or "",
                "project": r.get("cwd") or "", "cwd": r.get("cwd") or "",
                "size": size, "mtime": mtime,
                "deleted_mtime": (r.get("deleted_at") or 0) / 1000.0,
                "path": path, "live": False, "kind": "soft",
                "models": sorted((entry.get("models") or {}).keys()),
                "tokens": entry.get("totals"),
                "messages": meta.get("users"),
                "first_ts": to_epoch(r.get("created_at")),
                "last_ts": to_epoch(r.get("last_activity_at") or r.get("updated_at")),
                "note": "" if path else "找不到会话文件",
            })
        return out

    def sessions(self):
        return self._rows(False)

    def quick_stats(self):
        try:
            con = _connect(self.root)
            try:
                n = con.execute(
                    "select count(*) from sessions where deleted_at is null"
                ).fetchone()[0]
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            n = 0
        size = 0
        for p in self._file_map().values():
            try:
                size += os.path.getsize(p)
            except OSError:
                pass
        return n, size

    def detail(self, s):
        from .. import usage
        cache = usage.load_cache()
        entry = {}
        if s.get("path"):
            entry = cached_entry(cache, self.key, s["path"], parse_usage) or {}
        meta = entry.get("meta") or {}
        return {
            "users": meta.get("users") or 0,
            "asst": None,
            "first_text": meta.get("first_text") or "",
            "last_text": meta.get("last_text") or "",
            "start_ts": _fmt(meta.get("first_ts") or s.get("first_ts")),
            "end_ts": _fmt(meta.get("last_ts") or s.get("last_ts")),
            "lines": None,
            "tokens": entry.get("totals"),
            "models": sorted((entry.get("models") or {}).keys()),
        }

    def resume_cmd(self, s):
        return ""

    def delete(self, ids, purge=False, force=False):
        out = {"deleted": [], "skipped_live": [], "errors": [], "freed": 0,
               "purge": purge}
        rows = {s["id"]: s for s in self.sessions()}
        if purge:
            rows.update({s["id"]: s for s in self._rows(True)})
        con = _connect(self.root, readonly=False)
        try:
            for sid in ids:
                s = rows.get(str(sid))
                if not s:
                    out["errors"].append("未找到会话 %s" % str(sid)[:8])
                    continue
                try:
                    if purge:
                        con.execute("delete from session_usage where session_id=?", (sid,))
                        con.execute("delete from sessions where id=?", (sid,))
                        for p in (s.get("path") or "",
                                  (s.get("path") or "") + ".meta.json"):
                            if p and os.path.exists(p):
                                os.remove(p)
                    else:
                        con.execute("update sessions set deleted_at=? where id=?",
                                    (int(time.time() * 1000), sid))
                    out["deleted"].append({"id": sid,
                                           "title": s["title"] or "(无标题)"})
                    out["freed"] += s.get("size") or 0
                except (OSError, sqlite3.Error) as e:
                    out["errors"].append("%s: %s" % (s["title"] or sid[:8], e))
            con.commit()
        finally:
            con.close()
        out["freed_str"] = human_size(out["freed"])
        return out

    def trash_list(self):
        out = []
        for s in self._rows(True):
            out.append({
                "token": s["id"], "id": s["id"], "title": s["title"],
                "deleted_at": _fmt(s.get("deleted_mtime")),
                "original_path": s.get("cwd") or "", "size": s["size"],
                "size_str": human_size(s["size"]),
            })
        return out

    def trash_restore(self, tokens):
        out = {"restored": [], "errors": []}
        con = _connect(self.root, readonly=False)
        try:
            for sid in tokens:
                try:
                    cur = con.execute("update sessions set deleted_at=null where id=?",
                                      (sid,))
                    con.commit()
                    if cur.rowcount:
                        out["restored"].append(sid)
                    else:
                        out["errors"].append("未找到会话 %s" % str(sid)[:8])
                except sqlite3.Error as e:
                    out["errors"].append("%s: %s" % (str(sid)[:8], e))
        finally:
            con.close()
        return out

    def trash_empty(self):
        out = {"removed": 0, "freed": 0, "errors": []}
        rows = self._rows(True)
        con = _connect(self.root, readonly=False)
        try:
            for s in rows:
                try:
                    con.execute("delete from session_usage where session_id=?", (s["id"],))
                    con.execute("delete from sessions where id=?", (s["id"],))
                    out["removed"] += 1
                    out["freed"] += s.get("size") or 0
                except sqlite3.Error as e:
                    out["errors"].append("%s: %s" % (s["id"][:8], e))
            con.commit()
        finally:
            con.close()
        out["freed_str"] = human_size(out["freed"])
        return out

    def usage_entries(self, cache):
        entries = []
        paths = set(self._file_map().values())
        for p in paths:
            e = cached_entry(cache, self.key, p, parse_usage)
            if e:
                entries.append(e)
        self.last_paths = paths
        return entries


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
