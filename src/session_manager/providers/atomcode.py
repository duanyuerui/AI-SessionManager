#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AtomCode：~/.atomcode/sessions/<项目>/<session-id>.meta + .snapshot

.meta 是会话元信息（名字、工作目录、时间、消息数），
.snapshot 是消息数组。会话由几个同名文件组成，删除时整组移入回收站。
"""
from __future__ import annotations

import json
import os
import shutil
import time

from .base import (Provider, app_trash_root, add_bucket, day_key,
                   human_size, new_bucket, to_epoch)
from .generic import _find_usage, normalize_usage, pick_role, pick_text, real_text


class AtomCodeProvider(Provider):
    key = "atomcode"
    name = "AtomCode"
    color = "#8a8f98"
    hint = "~/.atomcode/sessions"
    kind = "file"

    @property
    def sessions_root(self):
        return os.path.join(self.root, "sessions")

    @property
    def trash_root(self):
        return app_trash_root(self.key)

    def available(self):
        return os.path.isdir(self.sessions_root)

    def _pairs(self):
        """[(session_id, project_dir, meta_path, 同名文件列表)]"""
        out = []
        root = self.sessions_root
        if not os.path.isdir(root):
            return out
        for proj in os.listdir(root):
            pdir = os.path.join(root, proj)
            if not os.path.isdir(pdir):
                continue
            for fn in os.listdir(pdir):
                if not fn.endswith(".meta"):
                    continue
                sid = fn[:-5]
                prefix = os.path.join(pdir, sid)
                group = [p for p in (prefix + ".meta", prefix + ".snapshot",
                                     prefix + ".ui.json", prefix + ".lease")
                         if os.path.isfile(p)]
                out.append((sid, pdir, prefix + ".meta", group))
        return out

    def quick_stats(self):
        n = size = 0
        for _sid, _pdir, _meta, group in self._pairs():
            n += 1
            for p in group:
                try:
                    size += os.path.getsize(p)
                except OSError:
                    pass
        return n, size

    def _parse(self, sid, meta_path):
        meta = {}
        total = new_bucket()
        days = {}
        models = {}
        first_ts = last_ts = None
        first_text = last_text = ""
        users = asst = 0
        try:
            with open(meta_path, encoding="utf-8", errors="replace") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        prefix = meta_path[:-5]
        snap = {}
        try:
            with open(prefix + ".snapshot", encoding="utf-8", errors="replace") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            snap = {}
        msgs = snap.get("messages") if isinstance(snap, dict) else None
        model = (snap.get("model") if isinstance(snap, dict) else "") or ""
        seen = set()
        if isinstance(msgs, list):
            for idx, m in enumerate(msgs):
                if not isinstance(m, dict):
                    continue
                ts = to_epoch(m.get("timestamp") or m.get("time") or m.get("created_at"))
                if ts:
                    first_ts = ts if first_ts is None else first_ts
                    last_ts = ts
                role = pick_role(m)
                text = pick_text(m)
                if role == "user":
                    txt = real_text(text)
                    if txt:
                        users += 1
                        first_text = first_text or txt[:400]
                        last_text = txt[:400]
                elif role == "assistant":
                    asst += 1
                u = _find_usage(m)
                mid = str(m.get("id") or idx)
                if u and mid not in seen:
                    seen.add(mid)
                    b = normalize_usage(u)
                    if b:
                        add_bucket(total, b)
                        dk = day_key(ts) or ""
                        if dk:
                            add_bucket(days.setdefault(dk, new_bucket()), b)
                        add_bucket(models.setdefault(model or "unknown",
                                                     new_bucket()), b)
        created = to_epoch(meta.get("created_at"))
        updated = to_epoch(meta.get("updated_at"))
        first_ts = first_ts or created
        last_ts = last_ts or updated
        title = meta.get("name") or ""
        if title.startswith("session-"):
            title = ""
        if not title and first_text:
            title = first_text[:80]
        return {
            "sid": sid,
            "totals": total, "days": days, "models": models,
            "meta": {
                "cwd": meta.get("working_dir") or "",
                "title": title, "users": users, "asst": asst,
                "first_text": first_text, "last_text": last_text,
                "first_ts": first_ts, "last_ts": last_ts,
                "messages": meta.get("message_count"),
                "turns": meta.get("turn_count"),
            },
        }

    def _rows(self):
        from .. import usage
        from .base import cached_entry
        cache = usage.load_cache()
        rows = []
        for sid, _pdir, meta_path, group in self._pairs():
            e = cached_entry(cache, self.key, meta_path,
                             lambda p, _sid=sid: self._parse(_sid, p)) or {}
            meta = e.get("meta") or {}
            size = sum(os.path.getsize(p) for p in group if os.path.exists(p))
            mtime = max((os.path.getmtime(p) for p in group if os.path.exists(p)),
                        default=0)
            rows.append({
                "tool": self.key, "id": sid,
                "title": meta.get("title") or "",
                "project": meta.get("cwd") or os.path.basename(os.path.dirname(meta_path)),
                "cwd": meta.get("cwd") or "",
                "size": size, "mtime": mtime, "path": meta_path,
                "live": False, "kind": "file",
                "models": sorted((e.get("models") or {}).keys()),
                "tokens": e.get("totals"),
                "messages": meta.get("messages") or
                ((meta.get("users") or 0) + (meta.get("asst") or 0)),
                "turns": meta.get("turns"),
                "first_ts": meta.get("first_ts"),
                "last_ts": meta.get("last_ts"),
                "note": "",
            })
        rows.sort(key=lambda s: (-(s.get("mtime") or 0), s["id"]))
        return rows

    def sessions(self):
        return self._rows()

    def detail(self, s):
        e = self._parse(s["id"], s["path"]) or {}
        meta = e.get("meta") or {}
        return {
            "users": meta.get("users") or 0,
            "asst": meta.get("asst") or 0,
            "first_text": meta.get("first_text") or "",
            "last_text": meta.get("last_text") or "",
            "start_ts": _fmt(meta.get("first_ts")),
            "end_ts": _fmt(meta.get("last_ts")),
            "lines": None, "tokens": e.get("totals"),
            "models": sorted((e.get("models") or {}).keys()),
        }

    def delete(self, ids, purge=False, force=False):
        rows = {s["id"]: s for s in self.sessions()}
        out = {"deleted": [], "skipped_live": [], "errors": [], "freed": 0,
               "purge": purge}
        for sid in ids:
            s = rows.get(str(sid))
            if not s:
                out["errors"].append("未找到会话 %s" % str(sid)[:8])
                continue
            prefix = s["path"][:-5]
            group = [p for p in (prefix + ".meta", prefix + ".snapshot",
                                 prefix + ".ui.json", prefix + ".lease")
                     if os.path.exists(p)]
            try:
                if purge:
                    for p in group:
                        os.remove(p)
                else:
                    os.makedirs(self.trash_root, exist_ok=True)
                    dest = os.path.join(self.trash_root,
                                        "%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), sid))
                    os.makedirs(dest, exist_ok=True)
                    for p in group:
                        shutil.move(p, os.path.join(dest, os.path.basename(p)))
                    with open(dest + ".meta.json", "w", encoding="utf-8") as f:
                        json.dump({"session_id": sid, "original_path": s["path"],
                                   "title": s["title"], "cwd": s["cwd"],
                                   "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                  f, ensure_ascii=False, indent=2)
                out["deleted"].append({"id": sid,
                                       "title": s["title"] or "(无标题)"})
                out["freed"] += s["size"]
            except OSError as e:
                out["errors"].append("%s: %s" % (s["title"] or sid[:8], e))
        out["freed_str"] = human_size(out["freed"])
        return out

    def trash_list(self):
        from .base import FileTrash
        out = []
        for e in FileTrash(self.trash_root).entries():
            m = e.get("meta") or {}
            out.append({"token": e["token"], "id": m.get("session_id") or "",
                        "title": m.get("title") or "",
                        "deleted_at": m.get("deleted_at") or _fmt(e["mtime"]),
                        "original_path": m.get("original_path") or "",
                        "size": e["size"], "size_str": human_size(e["size"])})
        return out

    def trash_restore(self, tokens):
        out = {"restored": [], "errors": []}
        for t in tokens:
            if not t or t != os.path.basename(t):
                out["errors"].append("非法条目")
                continue
            src = os.path.join(self.trash_root, t)
            meta_path = src + ".meta.json"
            try:
                with open(meta_path, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                out["errors"].append("缺少恢复信息")
                continue
            dest = m.get("original_path")
            if not dest:
                out["errors"].append("缺少原始路径")
                continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                for fn in os.listdir(src):
                    shutil.move(os.path.join(src, fn),
                                os.path.join(os.path.dirname(dest), fn))
                os.rmdir(src)
                os.remove(meta_path)
                out["restored"].append(os.path.basename(dest))
            except OSError as e:
                out["errors"].append("%s: %s" % (t, e))
        return out

    def trash_empty(self):
        from .base import FileTrash
        return FileTrash(self.trash_root).empty()

    def usage_entries(self, cache):
        from .base import cached_entry
        entries = []
        paths = set()
        for sid, _pdir, meta_path, _group in self._pairs():
            paths.add(meta_path)
            e = cached_entry(cache, self.key, meta_path,
                             lambda p, _sid=sid: self._parse(_sid, p))
            if e:
                entries.append(e)
        self.last_paths = paths
        return entries


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
