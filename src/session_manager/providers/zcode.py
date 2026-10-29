#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZCode：~/.zcode/cli/db/db.sqlite

会话、消息、用量都在同一个 SQLite 里：
    session       会话（time_deleted 是应用自己的软删除）
    message/part  消息与内容片段
    model_usage   每次模型请求的 token
删除沿用应用的软删除（写 time_deleted），恢复就是清空。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .base import (Provider, add_bucket, bucket_total, day_key, human_size,
                   new_bucket, to_epoch)

_QUICK = {}  # db 路径 -> (时间, (会话数, 占用))


def db_path(root):
    for rel in (("cli", "db", "db.sqlite"), ("db.sqlite",)):
        p = os.path.join(root, *rel)
        if os.path.isfile(p):
            return p
    return os.path.join(root, "cli", "db", "db.sqlite")


def connect(root, readonly=True):
    p = db_path(root)
    if not os.path.isfile(p):
        raise OSError("找不到 %s" % p)
    if readonly:
        con = sqlite3.connect("file:///" + p.replace("\\", "/") + "?mode=ro",
                              uri=True, timeout=5)
    else:
        con = sqlite3.connect(p, timeout=15)
        con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


def _bucket_from_row(r):
    b = new_bucket()
    inp = int(r["input_tokens"] or 0)
    cached = int(r["cache_read_input_tokens"] or 0)
    b["input"] = max(0, inp - cached)
    b["cache_read"] = cached
    b["cache_write"] = int(r["cache_creation_input_tokens"] or 0)
    b["output"] = int(r["output_tokens"] or 0)
    b["reasoning"] = int(r["reasoning_tokens"] or 0)
    b["requests"] = 1
    return b


class ZcodeProvider(Provider):
    key = "zcode"
    name = "ZCode"
    color = "#2a78d6"
    hint = "~/.zcode/cli/db/db.sqlite"
    kind = "soft"

    @property
    def db_file(self):
        return db_path(self.root)

    def available(self):
        return os.path.isfile(self.db_file)

    # ---------- 列表 ----------
    def _usage_by_session(self):
        out = {}
        con = connect(self.root)
        try:
            rows = con.execute(
                "select session_id, model_id, started_at, input_tokens, "
                "cache_read_input_tokens, cache_creation_input_tokens, "
                "output_tokens, reasoning_tokens from model_usage"
            ).fetchall()
        finally:
            con.close()
        for r in rows:
            sid = r["session_id"]
            b = _bucket_from_row(r)
            e = out.setdefault(sid, {
                "sid": sid, "totals": new_bucket(), "days": {}, "models": {},
                "meta": {},
            })
            add_bucket(e["totals"], b)
            dk = day_key(to_epoch(r["started_at"])) or ""
            if dk:
                add_bucket(e["days"].setdefault(dk, new_bucket()), b)
            add_bucket(e["models"].setdefault(r["model_id"] or "unknown",
                                              new_bucket()), b)
        return out

    def _rows(self, deleted=False):
        con = connect(self.root)
        try:
            where = "s.time_deleted is not null" if deleted else "s.time_deleted is null"
            sql = (
                "select s.id, s.directory, s.title, s.time_created, s.time_updated, "
                "s.time_deleted, s.task_type, "
                "(select count(*) from message m where m.session_id=s.id) as msgs, "
                "(select coalesce(sum(length(p.data)),0) from part p "
                " where p.session_id=s.id) as bytes "
                "from session s where %s "
                "order by coalesce(s.time_deleted, s.time_updated) desc" % where
            )
            rows = [dict(r) for r in con.execute(sql)]
        finally:
            con.close()
        usage = self._usage_by_session()
        out = []
        for r in rows:
            u = usage.get(r["id"]) or {}
            out.append({
                "tool": self.key, "id": r["id"],
                "title": r.get("title") or "",
                "project": r.get("directory") or "",
                "cwd": r.get("directory") or "",
                "size": int(r.get("bytes") or 0),
                "mtime": (r.get("time_updated") or 0) / 1000.0,
                "deleted_mtime": (r.get("time_deleted") or 0) / 1000.0,
                "path": self.db_file, "live": False, "kind": "soft",
                "models": sorted((u.get("models") or {}).keys()),
                "tokens": u.get("totals"),
                "messages": int(r.get("msgs") or 0),
                "first_ts": to_epoch(r.get("time_created")),
                "last_ts": to_epoch(r.get("time_updated")),
                "note": "",
            })
        return out

    def sessions(self):
        return self._rows(False)

    def quick_stats(self):
        now = time.time()
        key = self.db_file
        hit = _QUICK.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
        try:
            con = connect(self.root)
            try:
                n = con.execute(
                    "select count(*) from session where time_deleted is null"
                ).fetchone()[0]
                size = con.execute(
                    "select coalesce(sum(length(data)),0) from part"
                ).fetchone()[0]
            finally:
                con.close()
            data = (n, int(size or 0))
            _QUICK[key] = (now, data)
            return data
        except (OSError, sqlite3.Error):
            return 0, 0

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

    def detail(self, s):
        con = connect(self.root)
        try:
            row = con.execute(
                "select s.*, "
                "(select count(*) from message m where m.session_id=s.id) msgs, "
                "(select count(*) from turn_usage t where t.session_id=s.id) turns "
                "from session s where s.id=?", (s["id"],)
            ).fetchone()
            if not row:
                raise ValueError("未找到会话")
            row = dict(row)
            texts = []
            for p in con.execute(
                "select p.data from part p join message m on m.id=p.message_id "
                "where m.session_id=? and m.data like '%\"role\":\"user\"%' "
                "order by m.time_created, p.sequence limit 40", (s["id"],)
            ):
                try:
                    d = json.loads(p["data"])
                except ValueError:
                    continue
                if d.get("type") == "text" and (d.get("text") or "").strip():
                    texts.append(d["text"].strip())
            models = sorted((self._usage_by_session().get(s["id"], {})
                             .get("models") or {}).keys())
        finally:
            con.close()
        return {
            "users": None, "asst": None,
            "first_text": texts[0][:600] if texts else "",
            "last_text": texts[-1][:600] if texts else "",
            "start_ts": _fmt((row.get("time_created") or 0) / 1000.0),
            "end_ts": _fmt((row.get("time_updated") or 0) / 1000.0),
            "lines": None,
            "tokens": self.token_map().get(s["id"]),
            "models": models,
            "turns": int(row.get("turns") or 0),
            "messages": int(row.get("msgs") or 0),
        }

    def resume_cmd(self, s):
        return "zcode --resume %s" % s["id"]

    # ---------- 删除 ----------
    def delete(self, ids, purge=False, force=False):
        out = {"deleted": [], "skipped_live": [], "errors": [], "freed": 0,
               "purge": purge}
        rows = {s["id"]: s for s in self.sessions()}
        if purge:
            rows.update({s["id"]: s for s in self._rows(True)})
        con = connect(self.root, readonly=False)
        try:
            for sid in ids:
                s = rows.get(str(sid))
                if not s:
                    out["errors"].append("未找到会话 %s" % str(sid)[:8])
                    continue
                try:
                    if purge:
                        for table in ("part", "message", "model_usage",
                                      "turn_usage", "tool_usage", "todo",
                                      "session_input", "session_entry",
                                      "session_target"):
                            con.execute("delete from %s where session_id=?"
                                        % table, (sid,))
                        con.execute("delete from session where id=?", (sid,))
                    else:
                        con.execute("update session set time_deleted=? where id=?",
                                    (int(time.time() * 1000), sid))
                    out["deleted"].append({"id": sid,
                                           "title": s["title"] or "(无标题)"})
                    out["freed"] += s.get("size") or 0
                except sqlite3.Error as e:
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
        con = connect(self.root, readonly=False)
        try:
            for sid in tokens:
                try:
                    cur = con.execute(
                        "update session set time_deleted=null where id=?", (sid,))
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
        rows = self._rows(True)
        return self.delete([s["id"] for s in rows], purge=True, force=True)

    def usage_entries(self, cache):
        self.last_paths = None
        return list(self._usage_by_session().values())


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
