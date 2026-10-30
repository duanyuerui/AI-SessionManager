#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code：~/.claude/projects/<项目>/<session-id>.jsonl"""
from __future__ import annotations

import argparse
import os
import time

from .. import core as sm
from .. import usage as usage_mod
from .base import (FileTrash, Provider, TRASH_NAME, add_bucket,
                   add_usage_buckets, cached_entry, day_key, new_bucket,
                   to_epoch)


def _ns(root):
    return argparse.Namespace(
        root=root, project=None, here=False, sort="time", limit=0,
        force=False, purge=False, y=True, dry_run=False,
        older_than=None, bigger_than=None, keep_last=0,
    )


# 单次请求的合理耗时区间：太短多半是重复记录，太长多半是挂着没动
_MIN_REQ_SEC = 0.02
_MAX_REQ_SEC = 600.0


def parse_usage(path):
    """一次遍历：token（按 message.id 去重）+ 首尾提问 + 消息数。

    顺带记两件工作台要用的东西：
    - 失败请求：Claude Code 会写一条 model 为 <synthetic> 的 assistant 记录，
      正文以 "API Error:" 开头，token 全是 0；
    - 请求耗时：api-request 记录是发请求的时刻，assistant 记录的 timestamp
      是这段回复落盘的时刻，两者相减就是这次请求的往返耗时。
    """
    totals = new_bucket()
    days = {}
    models = {}
    mdays = {}
    hours = {}
    mhours = {}
    seen = {}
    req_start = {}   # api-request id -> 发出时刻
    reqs = {}        # requestRef -> {end, outs: {message.id: output_tokens}}
    errors = []
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
                    if d.get("isApiErrorMessage") or m.get("model") == "<synthetic>":
                        errors.append(ts)
                        continue
                    mid = m.get("id") or ("#%d" % idx)
                    out = int(m["usage"].get("output_tokens") or 0)
                    seen[mid] = (m["usage"], m.get("model") or "unknown", ts)
                    ref = d.get("requestRef")
                    if ref:
                        r = reqs.setdefault(
                            ref, {"end": None, "outs": {}})
                        if ts is not None and (r["end"] is None or ts > r["end"]):
                            r["end"] = ts
                        if out > r["outs"].get(mid, 0):
                            r["outs"][mid] = out
            elif t == "api-request":
                rid = d.get("id")
                if rid and ts is not None:
                    if rid not in req_start or ts < req_start[rid]:
                        req_start[rid] = ts
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
        add_usage_buckets(days, models, mdays, ts, model, b, hours, mhours)
    # 失败请求：只进总量和按日/按小时，不进模型榜
    for ts in errors:
        b = new_bucket()
        b["errors"] = 1
        add_bucket(totals, b)
        add_usage_buckets(days, {}, {}, ts, "error", b, hours, None)
    # 生成速度：只统计「有 api-request 起点、有 assistant 终点」的请求
    for ref, r in reqs.items():
        start, end = req_start.get(ref), r["end"]
        if start is None or end is None:
            continue
        dur = end - start
        out = sum(r["outs"].values())
        if not (_MIN_REQ_SEC < dur < _MAX_REQ_SEC) or out <= 0:
            continue
        b = new_bucket()
        b["gen_ms"] = int(dur * 1000)
        b["gen_tokens"] = out
        add_bucket(totals, b)
        add_usage_buckets(days, {}, {}, end, "speed", b, hours, None)
    sid = os.path.splitext(os.path.basename(path))[0]
    return {
        "sid": sid, "totals": totals, "days": days, "models": models,
        "mdays": mdays,
        "hours": hours, "mhours": mhours,
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
    tracks_errors = True
    tracks_timing = True

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
