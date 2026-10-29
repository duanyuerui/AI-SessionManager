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

删除：对方应用没有「已删除」标记，删一个会话要同时清掉十几张表里的关联行。
所以删除前先把这些行导出成 JSON 放进回收站（`~/.ai-session-manager/trash/opensquilla/`），
再在一个事务里删干净；恢复就是把 JSON 里的行按顺序插回去。删除前提是 OpenSquilla 已退出。
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import time

from .base import (Provider, add_bucket, app_trash_root, bucket_total, day_key,
                   human_size, new_bucket, to_epoch)

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


# ---------- 删除 / 恢复用的小工具 ----------

_REL_COLS = ("session_id", "session_key")
_SKIP_TABLES = ("transcript_fts", "search_index", "sqlite_")
_RESTORE_FIRST = ("sessions", "usage_events", "usage_event_items")


def _jsonable(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
    return v


def _from_json(v):
    if isinstance(v, dict) and list(v.keys()) == ["__b64__"]:
        try:
            return base64.b64decode(v["__b64__"])
        except Exception:  # noqa: BLE001
            return None
    return v


def _rel_tables(con):
    """[(表名, [session_id / session_key 里存在的列])]，跳过 FTS / 系统表。"""
    out = []
    for (name,) in con.execute("select name from sqlite_master where type='table'"):
        low = name.lower()
        if any(low.startswith(p) for p in _SKIP_TABLES):
            continue
        try:
            cols = {r[1].lower() for r in con.execute('pragma table_info("%s")' % name)}
        except sqlite3.Error:
            continue
        hit = [c for c in _REL_COLS if c in cols]
        if hit:
            out.append((name, hit))
    return out


def _related_sessions(con, session_key):
    """这个会话 + 由它派生出的子会话（parent_session_key / spawned_by）。"""
    try:
        rows = [dict(r) for r in con.execute(
            "select session_key, session_id, parent_session_key, spawned_by "
            "from sessions")]
    except sqlite3.Error:
        return []
    by_key = {r["session_key"]: r for r in rows if r.get("session_key")}
    if session_key not in by_key:
        return []
    keep = {session_key}
    changed = True
    while changed:
        changed = False
        for r in rows:
            k = r.get("session_key")
            if not k or k in keep:
                continue
            if (r.get("parent_session_key") in keep
                    or r.get("spawned_by") in keep):
                keep.add(k)
                changed = True
    return [by_key[k] for k in keep if k in by_key]


def _app_running():
    """OpenSquilla 开着的时候不要动它的库。"""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq OpenSquilla.exe", "/NH"],
            capture_output=True, text=True, timeout=5)
        return "OpenSquilla.exe" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


class OpenSquillaProvider(Provider):
    key = "opensquilla"
    name = "OpenSquilla"
    color = "#8a8f98"
    hint = "%APPDATA%/@opensquilla/desktop-electron/opensquilla/state/sessions.db"
    kind = "soft"
    readonly = False
    note = ""

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
                "session_key": r["session_key"],
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

    # ---------- 删除 / 回收站 ----------
    @property
    def trash_root(self):
        return app_trash_root(self.key)

    def _select_related(self, con, sessions, table, cols):
        keys = [r["session_key"] for r in sessions]
        ids = [r["session_id"] for r in sessions]
        where, params = [], []
        if "session_id" in cols and ids:
            where.append("session_id in (%s)" % ",".join("?" * len(ids)))
            params += ids
        if "session_key" in cols and keys:
            where.append("session_key in (%s)" % ",".join("?" * len(keys)))
            params += keys
        if not where:
            return []
        return [dict(r) for r in con.execute(
            'select * from "%s" where %s' % (table, " or ".join(where)),
            params)]

    def _export(self, con, sessions):
        """把这个会话（含子会话）在每张表里的行都导出来，恢复时原样插回去。"""
        data = {}
        for table, cols in _rel_tables(con):
            rows = self._select_related(con, sessions, table, cols)
            if rows:
                data[table] = [{k: _jsonable(v) for k, v in r.items()}
                               for r in rows]
        # usage_event_items 没有 session 列，靠 event_id 挂在 usage_events 下面
        eids = [r.get("event_id") for r in (data.get("usage_events") or [])
                if r.get("event_id")]
        if eids:
            try:
                rows = [dict(r) for r in con.execute(
                    "select * from usage_event_items where event_id in (%s)"
                    % ",".join("?" * len(eids)), eids)]
            except sqlite3.Error:
                rows = []
            if rows:
                data["usage_event_items"] = [
                    {k: _jsonable(v) for k, v in r.items()} for r in rows]
        return data

    def _purge_rows(self, con, sessions):
        keys = [r["session_key"] for r in sessions]
        ids = [r["session_id"] for r in sessions]
        # usage_event_items 靠 event_id 关联；先删掉，别指望外键级联总是开着
        try:
            eids = [r[0] for r in con.execute(
                "select event_id from usage_events where session_id in (%s)"
                % ",".join("?" * len(ids)), ids)]
            if eids:
                con.execute("delete from usage_event_items where event_id in (%s)"
                            % ",".join("?" * len(eids)), eids)
        except sqlite3.Error:
            pass
        for table, cols in _rel_tables(con):
            if table == "sessions":
                continue
            where, params = [], []
            if "session_id" in cols:
                where.append("session_id in (%s)" % ",".join("?" * len(ids)))
                params += ids
            if "session_key" in cols:
                where.append("session_key in (%s)" % ",".join("?" * len(keys)))
                params += keys
            if where:
                con.execute('delete from "%s" where %s'
                            % (table, " or ".join(where)), params)
        con.execute("delete from sessions where session_key in (%s)"
                    % ",".join("?" * len(keys)), keys)

    def _write_trash(self, session, data):
        os.makedirs(self.trash_root, exist_ok=True)
        sid = session.get("id") or session.get("session_id") or ""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        token = "%s_%s.json" % (stamp, sid)
        i = 1
        while os.path.exists(os.path.join(self.trash_root, token)):
            i += 1
            token = "%s_%d_%s.json" % (stamp, i, sid)
        path = os.path.join(self.trash_root, token)
        payload = {
            "tool": self.key,
            "session_id": sid,
            "session_key": session.get("session_key") or "",
            "title": session.get("title") or "",
            "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db": self.db_file,
            "tables": data,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return token

    def delete(self, ids, purge=False, force=False):
        out = {"deleted": [], "skipped_live": [], "errors": [], "freed": 0,
               "purge": purge}
        if _app_running():
            out["errors"].append("OpenSquilla 正在运行，请先退出它再删除")
            out["freed_str"] = "0B"
            return out
        rows = {s["id"]: s for s in self._rows()}
        try:
            con = _connect(self.db_file, readonly=False)
        except (OSError, sqlite3.Error) as e:
            out["errors"].append("打不开数据库：%s" % e)
            out["freed_str"] = "0B"
            return out
        try:
            con.execute("PRAGMA foreign_keys=ON")
            for sid in ids:
                s = rows.get(str(sid))
                if not s:
                    out["errors"].append("未找到会话 %s" % str(sid)[:8])
                    continue
                try:
                    con.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as e:
                    out["errors"].append(
                        "数据库被占用（%s），请先退出 OpenSquilla" % e)
                    break
                try:
                    sessions = _related_sessions(con, s.get("session_key") or "")
                    if not sessions:
                        raise sqlite3.Error("找不到会话记录")
                    data = self._export(con, sessions)
                    if not purge:
                        self._write_trash(s, data)
                    self._purge_rows(con, sessions)
                    con.commit()
                except Exception as e:  # noqa: BLE001
                    con.rollback()
                    out["errors"].append(
                        "%s: %s" % (s.get("title") or str(sid)[:8], e))
                    continue
                out["deleted"].append({"id": sid,
                                       "title": s.get("title") or "(无标题)"})
                out["freed"] += s.get("size") or 0
        finally:
            con.close()
        out["freed_str"] = human_size(out["freed"])
        return out

    def trash_list(self):
        out = []
        root = self.trash_root
        if not os.path.isdir(root):
            return out
        for fn in sorted(os.listdir(root), reverse=True):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    meta = json.load(f)
                size = os.path.getsize(p)
            except (OSError, ValueError):
                continue
            out.append({
                "token": fn, "id": meta.get("session_id") or "",
                "title": meta.get("title") or "",
                "deleted_at": meta.get("deleted_at") or "",
                "original_path": meta.get("db") or "",
                "size": size, "size_str": human_size(size),
            })
        return out

    def trash_restore(self, tokens):
        out = {"restored": [], "errors": []}
        for t in tokens:
            if not t or t != os.path.basename(t) or not t.endswith(".json"):
                out["errors"].append("非法条目")
                continue
            path = os.path.join(self.trash_root, t)
            try:
                with open(path, encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                out["errors"].append("备份文件损坏：%s" % t)
                continue
            tables = meta.get("tables") or {}
            try:
                con = _connect(self.db_file, readonly=False)
            except (OSError, sqlite3.Error) as e:
                out["errors"].append("打不开数据库：%s" % e)
                continue
            ok = False
            try:
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("BEGIN IMMEDIATE")
                order = [x for x in _RESTORE_FIRST if x in tables]
                order += [x for x in sorted(tables) if x not in order]
                for table in order:
                    for row in tables[table]:
                        cols = list(row.keys())
                        vals = [_from_json(row[c]) for c in cols]
                        con.execute(
                            'insert into "%s" (%s) values (%s)'
                            % (table,
                               ",".join('"%s"' % c for c in cols),
                               ",".join("?" * len(cols))), vals)
                con.commit()
                ok = True
            except Exception as e:  # noqa: BLE001
                try:
                    con.rollback()
                except sqlite3.Error:
                    pass
                out["errors"].append("%s: %s" % (meta.get("title") or t, e))
            finally:
                con.close()
            if not ok:
                continue
            try:
                os.remove(path)
            except OSError:
                pass
            out["restored"].append(meta.get("session_id") or t)
        return out

    def trash_empty(self):
        out = {"removed": 0, "freed": 0, "errors": []}
        root = self.trash_root
        if not os.path.isdir(root):
            out["freed_str"] = "0B"
            return out
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(root, fn)
            try:
                size = os.path.getsize(p)
                os.remove(p)
                out["removed"] += 1
                out["freed"] += size
            except OSError as e:
                out["errors"].append("%s: %s" % (fn, e))
        out["freed_str"] = human_size(out["freed"])
        return out

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
