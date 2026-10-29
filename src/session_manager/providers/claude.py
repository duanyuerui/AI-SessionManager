#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code：~/.claude/projects/<项目>/<session-id>.jsonl"""
from __future__ import annotations

import argparse
import os
import time

from .. import core as sm
from .. import usage as usage_mod
from .base import (FileTrash, Provider, TRASH_NAME, add_bucket, cached_entry,
                   day_key, new_bucket, to_epoch)


def _ns(root):
    return argparse.Namespace(
        root=root, project=None, here=False, sort="time", limit=0,
        force=False, purge=False, y=True, dry_run=False,
        older_than=None, bigger_than=None, keep_last=0,
    )


def parse_usage(path):
    """一次遍历：token（按 message.id 去重）+ 首尾提问 + 消息数。"""
    totals = new_bucket()
    days = {}
    models = {}
    seen = {}
    users = asst = 0
    first_text = last_text = ""
    first_ts = last_ts = None
    idx = 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            d = sm.parse_line(line)
            if d is None:
                continue
            idx += 1
            ts = to_epoch(d.get("timestamp"))
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            t = d.get("type")
            if t == "assistant":
                asst += 1
                m = d.get("message")
                if isinstance(m, dict) and isinstance(m.get("usage"), dict):
                    mid = m.get("id") or ("#%d" % idx)
                    seen[mid] = (m["usage"], m.get("model") or "unknown", ts)
            elif t == "user":
                txt = sm.real_user_text(sm.msg_text(d))
                if txt:
                    users += 1
                    if not first_text:
                        first_text = txt
                    last_text = txt
    for u, model, ts in seen.values():
        b = new_bucket()
        b["input"] = int(u.get("input_tokens") or 0)
        b["cache_read"] = int(u.get("cache_read_input_tokens") or 0)
        b["cache_write"] = int(u.get("cache_creation_input_tokens") or 0)
        b["output"] = int(u.get("output_tokens") or 0)
        det = u.get("output_tokens_details") or {}
        b["reasoning"] = int(det.get("thinking_tokens") or 0)
        b["requests"] = 1
        add_bucket(totals, b)
        dk = day_key(ts) or ""
        if dk:
            add_bucket(days.setdefault(dk, new_bucket()), b)
        add_bucket(models.setdefault(model, new_bucket()), b)
    sid = os.path.splitext(os.path.basename(path))[0]
    return {
        "sid": sid, "totals": totals, "days": days, "models": models,
        "meta": {
            "users": users, "asst": asst,
            "first_text": first_text[:400], "last_text": last_text[:400],
            "first_ts": first_ts, "last_ts": last_ts,
        },
    }


class ClaudeProvider(Provider):
    key = "claude"
    name = "Claude Code"
    color = "#eb6834"
    hint = "~/.claude/projects"

    @property
    def projects_root(self):
        return os.path.join(self.root, "projects")

    def trash(self):
        return FileTrash(os.path.join(self.root, TRASH_NAME))

    def live_id(self):
        return sm.current_sid()

    def quick_stats(self):
        n = size = 0
        root = self.projects_root
        if os.path.isdir(root):
            for pdir in os.listdir(root):
                ppath = os.path.join(root, pdir)
                if not os.path.isdir(ppath):
                    continue
                for fn in os.listdir(ppath):
                    if not fn.endswith(".jsonl"):
                        continue
                    try:
                        size += os.path.getsize(os.path.join(ppath, fn))
                        n += 1
                    except OSError:
                        pass
        return n, size

    def sessions(self):
        rows = sm.scan(_ns(self.root))
        cache = usage_mod.load_cache()
        tokens = self.token_map(cache)
        live = self.live_id()
        out = []
        for s in rows:
            entry = usage_mod.entry_for(cache, self.key, s["path"])
            models = sorted((entry or {}).get("models", {}).keys())
            out.append({
                "tool": self.key,
                "id": s["session_id"],
                "title": s["title"],
                "project": s["cwd"] or s["project_dir"],
                "cwd": s["cwd"],
                "size": s["size"],
                "mtime": s["mtime"],
                "path": s["path"],
                "live": bool(live) and s["session_id"] == live,
                "kind": "file",
                "models": models,
                "tokens": tokens.get(s["session_id"]),
                "last_prompt": s.get("last_prompt") or "",
                "first_ts": to_epoch(s.get("first_ts")),
                "last_ts": to_epoch(s.get("last_ts")),
            })
        return out

    def detail(self, s):
        row = sm.scan(_ns(self.root))
        target = next((x for x in row if x["session_id"] == s["id"]), None)
        if not target:
            raise ValueError("未找到会话")
        info = sm.deep_info(target)
        cache = usage_mod.load_cache()
        tokens = self.token_map(cache).get(s["id"])
        entry = usage_mod.entry_for(cache, self.key, target["path"]) or {}
        return {
            "users": info["users"], "asst": info["asst"],
            "cost": info["cost"],
            "dur": sm.fmt_dur(info["dur_ms"]) if info["dur_ms"] else "",
            "first_text": sm.trunc(info["first_text"], 600),
            "last_text": sm.trunc(info["last_text"], 600),
            "last_prompt": sm.trunc(target.get("last_prompt") or "", 600),
            "start_ts": sm.short_ts(info["start_ts"]),
            "end_ts": sm.short_ts(info["end_ts"]),
            "lines": sm.count_lines(target["path"]),
            "tokens": tokens,
            "models": sorted((entry.get("models") or {}).keys()),
        }

    def resume_cmd(self, s):
        return "claude --resume %s" % s["id"]

    # -- 删除 --
    def delete(self, ids, purge=False, force=False):
        rows = {s["id"]: s for s in self.sessions()}
        out = {"deleted": [], "skipped_live": [], "errors": [], "freed": 0,
               "purge": purge}
        trash = self.trash()
        live = self.live_id()
        for sid in ids:
            s = rows.get(str(sid))
            if not s:
                out["errors"].append("未找到会话 %s" % str(sid)[:8])
                continue
            if sid == live and not force:
                out["skipped_live"].append(sid)
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
        out["freed_str"] = sm.human_size(out["freed"])
        return out

    def trash_list(self):
        out = []
        for e in self.trash().entries():
            m = e.get("meta") or {}
            out.append({
                "token": e["token"], "id": m.get("session_id") or "",
                "title": m.get("title") or "",
                "deleted_at": m.get("deleted_at") or sm.fmt_mtime(e["mtime"]),
                "original_path": m.get("original_path") or "",
                "size": e["size"], "size_str": sm.human_size(e["size"]),
            })
        return out

    def trash_restore(self, tokens):
        out = {"restored": [], "errors": []}
        trash = self.trash()
        for t in tokens:
            try:
                dest, err = trash.restore(t)
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

    # -- token --
    def usage_entries(self, cache):
        entries = []
        paths = set()
        root = self.projects_root
        if os.path.isdir(root):
            for pdir in os.listdir(root):
                ppath = os.path.join(root, pdir)
                if not os.path.isdir(ppath):
                    continue
                for fn in os.listdir(ppath):
                    if not fn.endswith(".jsonl"):
                        continue
                    fpath = os.path.join(ppath, fn)
                    if not os.path.isfile(fpath):
                        continue
                    paths.add(fpath)
                    e = cached_entry(cache, self.key, fpath, parse_usage)
                    if e:
                        entries.append(e)
        self.last_paths = paths
        return entries
