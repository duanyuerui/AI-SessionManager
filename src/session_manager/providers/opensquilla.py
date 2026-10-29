#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenSquilla（桌面版）：Electron 用户数据目录下的 SQLite。

Windows 默认位置：
    %APPDATA%\\@opensquilla\\desktop-electron\\opensquilla\\state\\sessions.db

库里的关键表：
    sessions            会话（标题、模型、时间、累计 token）
    transcript_entries  消息（role / content / turn_usage）
    usage_events        每次模型请求（started_at_ms / model / token）
    usage_event_items   同上，按模型拆开的明细

只读接入：这是对方应用自己的库，没有官方的「已删除」标记，直接删行会留下
孤立记录，所以只浏览、不修改。
"""
from __future__ import annotations

import os
import sqlite3
import time

from .base import (Provider, add_bucket, bucket_total, day_key, human_size,
                   new_bucket, to_epoch)

_QUICK = {}  # db 路径 -> (时间, (会话数, 占用))
_DB_NAMES = ("sessions.db", "session.db", "sessions.sqlite",
             "sessions.sqlite3")
_TITLE_FIELDS = ("label", "derived_title", "display_name", "subject")
# 用户消息前面带的时间戳前缀：[2026-10-12T16:49+08:00 Mon Asia/Shanghai]
_TIME_PREFIX = ("[",)


def _strip_prefix(text):
    s = (text or "").strip()
    if s.startswith(_TIME_PREFIX):
        end = s.find("]")
        if 0 < end < 80:
            s = s[end + 1:].strip()
    return s


def find_db(root):
    """在用户数据目录里找 sessions.db（安装位置各家版本略有差异）。"""
    direct = (
        os.path.join(root, "opensquilla", "state", "sessions.db"),
        os.path.join(root, "state", "sessions.db"),
        os.path.join(root, "sessions.db"),
        os.path.join(root, "opensquilla", "sessions.db"),
    )
    for p in direct:
        if os.path.isfile(p):
            return p
    # 兜底：浅层找一遍
    base_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirs, files in os.walk(root):
        depth = dirpath.count(os.sep) - base_depth
        dirs[:] = [d for d in dirs
                   if d.lower() not in ("cache", "caches", "logs", "log",
                                        "gpucache", "code cache", "indexeddb",
                                        "local storage", "session storage",
                                        "blob_storage", "node_modules")
                   and not d.startswith(".")]
        if depth >= 4:
            dirs[:] = []
        for fn in files:
            if fn.lower() in _DB_NAMES:
                return os.path.join(dirpath, fn)
    return os.path.join(root, "opensquilla", "state", "sessions.db")


def _connect(db, readonly=True):
    if readonly:
        con = sqlite3.connect("file:///" + db.replace("\\", "/") + "?mode=ro",
                              uri=True, timeout=5)
    else:
        con = sqlite3.connect(db, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _bucket(inp, cache_read, cache_write, out, reasoning, requests=1):
    """OpenSquilla 的 input_tokens 含缓存读，要减掉才是「非缓存输入」。"""
    b = new_bucket()
    cached = int(cache_read or 0)
    b["input"] = max(0, int(inp or 0) - cached)
    b["cache_read"] = cached
    b["cache_write"] = int(cache_write or 0)
    b["output"] = int(out or 0)
    b["reasoning"] = int(reasoning or 0)
    b["requests"] = requests
    return b


class OpenSquillaProvider(Provider):
    key = "opensquilla"
    name = "OpenSquilla"
    color = "#8a8f98"
    hint = "%APPDATA%/@opensquilla/desktop-electron/opensquilla/state/sessions.db"
    kind = "soft"
    readonly = True
    note = "只读"

    @property
    def db_file(self):
        return find_db(self.root)

    def available(self):
        return os.path.isfile(self.db_file)

    # ---------- token ----------
    def _usage_by_session(self):
        out = {}
        known = set()
        try:
            con = _connect(self.db_file)
        except (OSError, sqlite3.Error):
            return out
        try:
            tables = {r[0] for r in con.execute(
                "select name from sqlite_master where type='table'")}
            if "sessions" in tables:
                known = {r[0] for r in con.execute("select session_id from sessions")}
            rows = []
            if "usage_event_items" in tables and "usage_events" in tables:
                rows = con.execute(
                    "select e.session_id as sid, e.event_id as eid, "
                    "e.started_at_ms as ts, "
                    "coalesce(i.model, e.model) as model, "
                    "i.input_tokens as inp, i.output_tokens as outp, "
                    "i.cache_read_tokens as cr, i.cache_write_tokens as cw, "
                    "i.reasoning_tokens as rsn "
                    "from usage_event_items i join usage_events e "
                    "on e.event_id = i.event_id").fetchall()
            elif "usage_events" in tables:
                rows = con.execute(
                    "select session_id as sid, event_id as eid, started_at_ms as ts, "
                    "model, input_tokens as inp, output_tokens as outp, "
                    "cache_read_tokens as cr, cache_write_tokens as cw, "
                    "reasoning_tokens as rsn from usage_events").fetchall()
            # 事件有明细就用明细；只有事件、没有明细（或没有明细表）时补上事件本身
            seen_events = {r["eid"] for r in rows} if rows else set()
            if "usage_events" in tables and "usage_event_items" in tables:
                for r in con.execute(
                        "select session_id as sid, event_id as eid, "
                        "started_at_ms as ts, model, input_tokens as inp, "
                        "output_tokens as outp, cache_read_tokens as cr, "
                        "cache_write_tokens as cw, reasoning_tokens as rsn "
                        "from usage_events").fetchall():
                    if r["eid"] not in seen_events:
                        rows.append(r)
        except sqlite3.Error:
            rows = []
        finally:
            con.close()

        # 只统计现有的会话：已删除/临时子任务的用量不算进来，
        # 这样和工作台里的会话数、应用自己 sessions 表的累计值都对得上
        seen_req = set()
        for r in rows:
            sid = r["sid"]
            if not sid or (known and sid not in known):
                continue
            b = _bucket(r["inp"], r["cr"], r["cw"], r["outp"], r["rsn"])
            e = out.setdefault(sid, {"sid": sid, "totals": new_bucket(),
                                     "days": {}, "models": {}, "meta": {}})
            # 请求数按 event 计，不按 item 计
            eid = r["eid"]
            req = 0
            if eid not in seen_req:
                seen_req.add(eid)
                req = 1
            b["requests"] = req
            add_bucket(e["totals"], b)
            dk = day_key(to_epoch(r["ts"])) or ""
            if dk:
                add_bucket(e["days"].setdefault(dk, new_bucket()), b)
            add_bucket(e["models"].setdefault(r["model"] or "unknown",
                                              new_bucket()), b)
        return out

    def _session_row_bucket(self, r):
        """usage_events 不全时的兜底：会话表自己的累计值。"""
        keys = r.keys()
        inp = r["input_tokens"] if "input_tokens" in keys else 0
        cr = r["cache_read"] if "cache_read" in keys else 0
        cw = r["cache_write"] if "cache_write" in keys else 0
        outp = r["output_tokens"] if "output_tokens" in keys else 0
        if not (inp or cr or cw or outp):
            return None
        return _bucket(inp, cr, cw, outp, 0, requests=0)

    def quick_usage(self, cache=None):
        totals = new_bucket()
        models = {}
        for e in self._usage_by_session().values():
            add_bucket(totals, e["totals"])
            for m, b in e["models"].items():
                add_bucket(models.setdefault(m, new_bucket()), b)
        return {"totals": totals, "total": bucket_total(totals),
                "models": {m: bucket_total(b) for m, b in models.items()}}

    def token_map(self, cache=None):
        return {sid: e["totals"] for sid, e in self._usage_by_session().items()}

    # ---------- 列表 ----------
    def _rows(self):
        try:
            con = _connect(self.db_file)
        except (OSError, sqlite3.Error):
            return []
        try:
            sql = (
                "select s.session_id, s.session_key, s.created_at, s.updated_at, "
                "s.label, s.derived_title, s.display_name, s.subject, "
                "s.model, s.model_provider, s.execution_workspace, s.status, "
                "s.input_tokens, s.output_tokens, s.cache_read, s.cache_write, "
                "(select count(*) from transcript_entries t "
                " where t.session_id = s.session_id) as msgs, "
                "(select coalesce(sum(length(t.content)), 0) from transcript_entries t "
                " where t.session_id = s.session_id) as bytes, "
                "(select t.content from transcript_entries t "
                " where t.session_id = s.session_id and t.role = 'user' "
                " order by t.created_at, t.id limit 1) as first_user "
                "from sessions s order by s.updated_at desc"
            )
            rows = con.execute(sql).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        usage = self._usage_by_session()
        out = []
        for r in rows:
            sid = r["session_id"]
            title = ""
            for k in _TITLE_FIELDS:
                v = r[k]
                if isinstance(v, str) and v.strip():
                    title = v.strip()
                    break
            if not title:
                title = _strip_prefix(r["first_user"])[:80]
            u = usage.get(sid) or {}
            totals = u.get("totals")
            if not totals:
                totals = self._session_row_bucket(r)
            models = sorted((u.get("models") or {}).keys())
            if not models and r["model"]:
                models = [r["model"]]
            out.append({
                "tool": self.key, "id": sid,
                "title": title,
                "project": r["execution_workspace"] or "",
                "cwd": r["execution_workspace"] or "",
                "size": int(r["bytes"] or 0),
                "mtime": to_epoch(r["updated_at"]) or 0,
                "path": self.db_file, "live": False, "kind": "soft",
                "models": models,
                "tokens": totals,
                "messages": int(r["msgs"] or 0),
                "first_ts": to_epoch(r["created_at"]),
                "last_ts": to_epoch(r["updated_at"]),
                "note": self.note,
            })
        return out

    def sessions(self):
        return self._rows()

    def quick_stats(self):
        now = time.time()
        key = self.db_file
        hit = _QUICK.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
        try:
            con = _connect(self.db_file)
            try:
                n = con.execute("select count(*) from sessions").fetchone()[0]
            finally:
                con.close()
            data = (int(n or 0), os.path.getsize(self.db_file))
            _QUICK[key] = (now, data)
            return data
        except (OSError, sqlite3.Error):
            return 0, 0

    def detail(self, s):
        try:
            con = _connect(self.db_file)
        except (OSError, sqlite3.Error):
            return {}
        try:
            row = con.execute(
                "select * from sessions where session_id=?", (s["id"],)
            ).fetchone()
            if not row:
                return {}
            users = con.execute(
                "select count(*) from transcript_entries "
                "where session_id=? and role='user'", (s["id"],)).fetchone()[0]
            asst = con.execute(
                "select count(*) from transcript_entries "
                "where session_id=? and role='assistant'", (s["id"],)).fetchone()[0]
            texts = [_strip_prefix(r[0]) for r in con.execute(
                "select content from transcript_entries "
                "where session_id=? and role='user' "
                "order by created_at, id", (s["id"],)) if r[0]]
            texts = [t for t in texts if t]
        except sqlite3.Error:
            return {}
        finally:
            con.close()
        usage = self._usage_by_session().get(s["id"]) or {}
        models = sorted((usage.get("models") or {}).keys())
        if not models and row["model"]:
            models = [row["model"]]
        return {
            "users": int(users or 0), "asst": int(asst or 0),
            "first_text": texts[0][:600] if texts else "",
            "last_text": texts[-1][:600] if texts else "",
            "start_ts": _fmt(to_epoch(row["created_at"])),
            "end_ts": _fmt(to_epoch(row["updated_at"])),
            "lines": None,
            "tokens": usage.get("totals") or self._session_row_bucket(row),
            "models": models,
        }

    # ---------- 删除（只读） ----------
    def delete(self, ids, purge=False, force=False):
        return {"deleted": [], "skipped_live": [],
                "errors": ["「%s」是数据库型客户端，为避免破坏它的数据，"
                           "目前只读展示" % self.name],
                "freed": 0, "freed_str": "0B", "purge": purge}

    def trash_list(self):
        return []

    def trash_restore(self, tokens):
        return {"restored": [], "errors": ["只读展示，不支持恢复"]}

    def trash_empty(self):
        return {"removed": 0, "freed": 0, "freed_str": "0B", "errors": []}

    def usage_entries(self, cache):
        self.last_paths = None
        entries = list(self._usage_by_session().values())
        if entries:
            return entries
        # 没有用量表：退回会话累计值
        out = []
        for s in self._rows():
            if s.get("tokens"):
                out.append({"sid": s["id"], "totals": s["tokens"],
                            "days": {}, "models": {}, "meta": {}})
        return out


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
