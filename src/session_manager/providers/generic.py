#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用适配器：不写死某个客户端，尽量识别常见的会话存储格式。

覆盖三类：
    GenericFileProvider   一个文件一次会话（.jsonl 一行一条，或 .json 整份）
    TaskDirProvider       一个目录一次会话（Cline / Roo / Kilo 的 tasks/<id>/）
    SqliteSessionsProvider  SQLite 里的 sessions / turns 表（GitHub Copilot Chat 等）

发现逻辑在 discovery.py，这里只负责「给定根目录和文件模式，怎么读」。
解析是宽松的：字段名、时间格式、token 字段各家不统一，按最常见的几种猜，
猜不出来的字段就留空，不阻塞列表。
"""
from __future__ import annotations

import glob as _glob
import json
import os
import re
import time

from .base import (FileTrash, Provider, add_bucket, app_trash_root,
                   cached_entry, day_key, dir_size, human_size, new_bucket,
                   to_epoch)

JSON_MAX = 32 * 1024 * 1024
HEAD_BYTES = 65536
JUNK_PREFIX = ("<", "# AGENTS.md", "## ", "Caveat:", "AGENTS.md instructions")


# ---------- 文本 / 字段提取 ----------

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for it in content:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                if it.get("type") in (None, "text", "input_text", "output_text"):
                    out.append(it.get("text") or it.get("content") or "")
        return "\n".join(x for x in out if x)
    if isinstance(content, dict):
        return content.get("text") or content.get("content") or ""
    return ""


def real_text(s):
    s = (s or "").strip()
    if not s:
        return ""
    for p in JUNK_PREFIX:
        if s.startswith(p):
            return ""
    return s


def first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def nested(d, *path):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def pick_ts(d):
    for k in ("timestamp", "time", "ts", "createdAt", "created_at",
              "time_created", "startTime", "start_time"):
        v = d.get(k)
        if isinstance(v, (int, float)) and v:
            return to_epoch(v)
        if isinstance(v, str):
            got = to_epoch(v)
            if got:
                return got
    t = d.get("time")
    if isinstance(t, dict):
        for k in ("created", "start", "updated"):
            got = to_epoch(t.get(k))
            if got:
                return got
    return None


def pick_cwd(d):
    for src in (d, d.get("metadata") if isinstance(d.get("metadata"), dict) else {},
                d.get("context") if isinstance(d.get("context"), dict) else {}):
        v = first(src, "cwd", "working_dir", "workingDir", "projectPath",
                  "workspace", "workspaceFolder", "workspaceDirectory",
                  "directory", "root")
        if isinstance(v, str) and v:
            return v
    return ""


def pick_title(d):
    for src in (d, d.get("metadata") if isinstance(d.get("metadata"), dict) else {}):
        v = first(src, "customTitle", "custom-title", "aiTitle", "ai-title",
                  "summary", "title", "name", "sessionName", "thread_name")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def pick_role(d):
    role = d.get("role")
    if isinstance(role, str):
        return role.lower()
    t = d.get("type")
    if isinstance(t, str):
        t = t.lower()
        if t in ("user", "human"):
            return "user"
        if t in ("assistant", "ai", "gemini", "model", "bot", "agent"):
            return "assistant"
    msg = d.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("role"), str):
        return msg["role"].lower()
    return ""


def pick_text(d):
    for src in (d, d.get("message") if isinstance(d.get("message"), dict) else {}):
        for k in ("content", "text", "parts", "message", "body"):
            if k in src:
                s = text_of(src.get(k))
                if s:
                    return s
    return ""


# ---------- token 归一化 ----------

def _num(v):
    try:
        return max(0, int(float(v)))
    except (TypeError, ValueError):
        return 0


def _find_usage(d):
    candidates = [d]
    if isinstance(d.get("message"), dict):
        candidates.append(d["message"])
    for k in ("usage", "tokenUsage", "token_usage", "tokens"):
        if isinstance(d.get(k), dict):
            return d[k]
    for holder in (d.get("message"), d.get("providerData"), d.get("metadata"),
                   d.get("data"), d.get("payload")):
        if not isinstance(holder, dict):
            continue
        for k in ("usage", "tokenUsage", "token_usage", "tokens", "rawUsage"):
            u = holder.get(k)
            if isinstance(u, dict):
                return u
    return None


def normalize_usage(u):
    if not isinstance(u, dict):
        return None
    inp = first(u, "input_tokens", "inputTokens", "prompt_tokens", "promptTokens",
                "input", "uncachedInputTokens")
    out = first(u, "output_tokens", "outputTokens", "completion_tokens",
                "completionTokens", "output")
    if inp is None and out is None:
        return None
    cached = first(u, "cache_read_input_tokens", "cacheReadTokens",
                   "cached_input_tokens", "cached_tokens",
                   "prompt_cache_hit_tokens", "cacheRead", "cached",
                   "cacheReadInputTokens")
    if cached is None:
        cached = nested(u, "prompt_tokens_details", "cached_tokens")
    if cached is None:
        details = u.get("inputTokensDetails")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            cached = details[0].get("cached_tokens")
    cache_write = first(u, "cache_creation_input_tokens", "cacheWriteTokens",
                        "prompt_cache_write_tokens", "cacheCreation")
    reasoning = first(u, "reasoning_output_tokens", "reasoning_tokens",
                      "reasoningTokens", "thoughts")
    if reasoning is None:
        reasoning = nested(u, "completion_tokens_details", "reasoning_tokens")
    if reasoning is None:
        details = u.get("outputTokensDetails")
        if isinstance(details, list) and details and isinstance(details[0], dict):
            reasoning = details[0].get("reasoning_tokens")

    inp, cached, out = _num(inp), _num(cached), _num(out)
    total = _num(first(u, "total_tokens", "totalTokens", "total", "totalTokenCount"))
    # 判断 input 是否已包含 cached：有 total 时按 total 反推，否则按字段风格猜
    inclusive = None
    if total and cached:
        if abs(total - (inp + out)) <= 2:
            inclusive = True
        elif abs(total - (inp + out + cached)) <= 2:
            inclusive = False
    if inclusive is None:
        if any(k in u for k in ("prompt_tokens", "promptTokens",
                                "cached_input_tokens")):
            inclusive = True
        elif any(k in u for k in ("cache_read_input_tokens", "cacheReadTokens",
                                  "cache_read_tokens")):
            inclusive = False
        else:
            inclusive = False
    b = new_bucket()
    b["input"] = max(0, inp - cached) if inclusive else inp
    b["cache_read"] = cached
    b["cache_write"] = _num(cache_write)
    b["output"] = out
    b["reasoning"] = _num(reasoning)
    b["requests"] = 1
    return b


# ---------- 单文件解析 ----------

class _Acc:
    def __init__(self):
        self.totals = new_bucket()
        self.days = {}
        self.models = {}
        self.seen = set()
        self.users = 0
        self.asst = 0
        self.first_text = ""
        self.last_text = ""
        self.first_ts = None
        self.last_ts = None
        self.cwd = ""
        self.title = ""
        self.model = ""

    def add_usage(self, u, ts, model, key):
        b = normalize_usage(u)
        if not b or key in self.seen:
            return
        self.seen.add(key)
        add_bucket(self.totals, b)
        dk = day_key(ts) or ""
        if dk:
            add_bucket(self.days.setdefault(dk, new_bucket()), b)
        add_bucket(self.models.setdefault(model or "unknown", new_bucket()), b)

    def add_message(self, role, text, ts):
        if not text:
            return
        if role == "user":
            txt = real_text(text)
            if not txt:
                return
            self.users += 1
            if not self.first_text:
                self.first_text = txt
            self.last_text = txt
            if not self.title:
                self.title = txt[:80]
            if ts is not None:
                self.first_ts = ts if self.first_ts is None else self.first_ts
                self.last_ts = ts
        elif role == "assistant":
            self.asst += 1
            if ts is not None:
                self.last_ts = ts
                if self.first_ts is None:
                    self.first_ts = ts


def _entry(path, acc, sid=""):
    return {
        "sid": sid or os.path.splitext(os.path.basename(path))[0],
        "totals": acc.totals, "days": acc.days, "models": acc.models,
        "meta": {
            "cwd": acc.cwd, "title": acc.title, "users": acc.users,
            "asst": acc.asst, "first_text": acc.first_text[:400],
            "last_text": acc.last_text[:400],
            "first_ts": acc.first_ts, "last_ts": acc.last_ts,
            "model": acc.model,
        },
    }


def parse_jsonl(path):
    acc = _Acc()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            # 有些格式把内容包在 payload/data 里
            inner = None
            for k in ("payload", "data", "event"):
                if isinstance(d.get(k), dict) and (
                        d[k].get("role") or d[k].get("type") or d[k].get("message")):
                    inner = d[k]
                    break
            src = dict(d)
            if inner:
                for k, v in inner.items():
                    src.setdefault(k, v)
            ts = pick_ts(src) or pick_ts(d)
            if ts is not None:
                acc.first_ts = ts if acc.first_ts is None else acc.first_ts
                acc.last_ts = ts
            if not acc.cwd:
                acc.cwd = pick_cwd(src) or pick_cwd(d)
            t = pick_title(src) or pick_title(d)
            if t and not acc.title:
                acc.title = t
            role = pick_role(src) or pick_role(d)
            if role:
                acc.add_message(role, pick_text(src) or pick_text(d), ts)
            model = (first(src, "model", "modelId", "model_id")
                     or nested(src, "message", "model") or acc.model)
            if isinstance(model, str) and model:
                acc.model = model
            u = _find_usage(src) or _find_usage(d)
            if u:
                mid = (nested(src, "message", "id") or src.get("id")
                       or nested(src, "providerData", "messageId") or str(idx))
                acc.add_usage(u, ts, acc.model, str(mid))
    return _entry(path, acc)


def parse_json(path):
    try:
        if os.path.getsize(path) > JSON_MAX:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, (dict, list)):
        return None
    acc = _Acc()
    root = d if isinstance(d, dict) else {}
    acc.cwd = pick_cwd(root)
    acc.title = pick_title(root)
    acc.model = first(root, "model", "modelId", "model_id") or ""
    start = pick_ts(root)
    if start:
        acc.first_ts = start
    sid = first(root, "sessionId", "session_id", "id") or ""
    msgs = None
    for k in ("messages", "history", "conversation", "chat", "turns", "events"):
        v = root.get(k)
        if isinstance(v, list):
            msgs = v
            break
    if msgs is None and isinstance(d, list):
        msgs = d
    if msgs:
        for idx, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            src = m
            if isinstance(m.get("message"), dict):
                src = {**m, **m["message"]}
            ts = pick_ts(src) or pick_ts(m)
            if ts:
                acc.last_ts = ts
                if acc.first_ts is None:
                    acc.first_ts = ts
            role = pick_role(src) or pick_role(m)
            if role:
                acc.add_message(role, pick_text(src) or pick_text(m), ts)
            model = first(src, "model", "modelId") or acc.model
            if isinstance(model, str) and model:
                acc.model = model
            u = _find_usage(src) or _find_usage(m)
            if u:
                acc.add_usage(u, ts, acc.model,
                              str(src.get("id") or src.get("messageId") or idx))
    if not acc.first_ts:
        try:
            acc.first_ts = os.path.getmtime(path)
        except OSError:
            pass
    return _entry(path, acc, sid=os.path.splitext(os.path.basename(path))[0]
                  if not sid else sid)


# ---------- 文件型 provider ----------

class GenericFileProvider(Provider):
    """root + 若干 glob，每个文件算一次会话。"""

    def __init__(self, key, name, root, globs, color="#8a8f98",
                 readonly=False, note="", kind="file"):
        super().__init__(root)
        self.key = key
        self.name = name
        self.color = color
        self.globs = globs
        self.readonly = readonly
        self.note = note
        self.kind = kind

    @property
    def trash_root(self):
        return app_trash_root(self.key)

    def trash(self):
        return FileTrash(self.trash_root)

    def files(self):
        seen = set()
        out = []
        for pattern in self.globs:
            full = os.path.join(self.root, pattern)
            for p in _glob.glob(full, recursive=True):
                if not os.path.isfile(p) or p in seen:
                    continue
                low = p.replace("\\", "/").lower()
                if "/session-trash/" in low or "/cache/" in low or "/logs/" in low:
                    continue
                if not (p.endswith(".jsonl") or p.endswith(".json")):
                    continue
                seen.add(p)
                out.append(p)
        out.sort(key=lambda p: -os.path.getmtime(p) if os.path.exists(p) else 0)
        return out

    def available(self):
        return os.path.isdir(self.root)

    def quick_stats(self):
        n = size = 0
        for p in self.files():
            try:
                size += os.path.getsize(p)
                n += 1
            except OSError:
                pass
        return n, size

    def _rows(self):
        from .. import usage
        cache = usage.load_cache()
        rows = []
        for path in self.files():
            e = cached_entry(cache, self.key, path,
                             parse_json if path.endswith(".json") else parse_jsonl)
            if not e:
                continue
            meta = e.get("meta") or {}
            try:
                st = os.stat(path)
            except OSError:
                continue
            rows.append({
                "tool": self.key, "id": e.get("sid") or os.path.basename(path),
                "title": meta.get("title") or "",
                "project": meta.get("cwd") or os.path.basename(os.path.dirname(path)),
                "cwd": meta.get("cwd") or "",
                "size": st.st_size, "mtime": st.st_mtime, "path": path,
                "live": False, "kind": self.kind,
                "models": sorted((e.get("models") or {}).keys()),
                "tokens": e.get("totals"),
                "messages": (meta.get("users") or 0) + (meta.get("asst") or 0),
                "first_ts": meta.get("first_ts"),
                "last_ts": meta.get("last_ts"),
                "note": self.note,
            })
        rows.sort(key=lambda s: (-(s.get("mtime") or 0), s["id"]))
        self.last_paths = set(self.files())
        return rows

    def sessions(self):
        return self._rows()

    def detail(self, s):
        from .. import usage
        cache = usage.load_cache()
        parser = parse_json if s["path"].endswith(".json") else parse_jsonl
        e = cached_entry(cache, self.key, s["path"], parser) or {}
        meta = e.get("meta") or {}
        return {
            "users": meta.get("users") or 0,
            "asst": meta.get("asst") or 0,
            "first_text": meta.get("first_text") or "",
            "last_text": meta.get("last_text") or "",
            "start_ts": _fmt(meta.get("first_ts")),
            "end_ts": _fmt(meta.get("last_ts")),
            "lines": None,
            "tokens": e.get("totals"),
            "models": sorted((e.get("models") or {}).keys()),
        }

    def resume_cmd(self, s):
        return ""

    def delete(self, ids, purge=False, force=False):
        rows = {s["id"]: s for s in self.sessions()}
        out = {"deleted": [], "skipped_live": [], "errors": [], "freed": 0,
               "purge": purge}
        if self.readonly:
            out["errors"].append("「%s」目前是只读发现，暂不支持删除" % self.name)
            return out
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
                out["deleted"].append({"id": s["id"],
                                       "title": s["title"] or "(无标题)"})
                out["freed"] += s["size"]
            except OSError as e:
                out["errors"].append("%s: %s" % (s["title"] or s["id"][:8], e))
        out["freed_str"] = human_size(out["freed"])
        return out

    def trash_list(self):
        out = []
        for e in (self.trash().entries() if not self.readonly else []):
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
        if self.readonly:
            out["errors"].append("只读发现，不支持从回收站恢复")
            return out
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
        if self.readonly:
            return {"removed": 0, "freed": 0, "freed_str": "0B", "errors": []}
        return self.trash().empty()

    def usage_entries(self, cache):
        entries = []
        paths = set(self.files())
        for path in paths:
            parser = parse_json if path.endswith(".json") else parse_jsonl
            e = cached_entry(cache, self.key, path, parser)
            if e:
                entries.append(e)
        self.last_paths = paths
        return entries


# ---------- Cline / Roo / Kilo：一个任务目录一次会话 ----------

_TASK_FILES = ("api_conversation_history.json", "ui_messages.json",
               "task_metadata.json", "claude_messages.json")


class TaskDirProvider(Provider):
    kind = "file"

    def __init__(self, key, name, root, color="#8a8f98", readonly=False, note=""):
        super().__init__(root)
        self.key = key
        self.name = name
        self.color = color
        self.readonly = readonly
        self.note = note

    def trash(self):
        return FileTrash(app_trash_root(self.key))

    def available(self):
        return os.path.isdir(self.root)

    def task_dirs(self):
        out = []
        try:
            names = os.listdir(self.root)
        except OSError:
            return out
        for n in names:
            d = os.path.join(self.root, n)
            if n.startswith(".") or not os.path.isdir(d):
                continue
            if any(os.path.isfile(os.path.join(d, f)) for f in _TASK_FILES):
                out.append((n, d))
        return out

    def quick_stats(self):
        n = size = 0
        for _sid, d in self.task_dirs():
            n += 1
            size += dir_size(d)
        return n, size

    def _parse(self, sid, d):
        totals = new_bucket()
        days = {}
        models = {}
        title = ""
        first_text = last_text = ""
        first_ts = last_ts = None
        users = asst = 0
        meta_path = os.path.join(d, "task_metadata.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, encoding="utf-8", errors="replace") as f:
                    md = json.load(f)
                title = md.get("task") or md.get("title") or ""
                if md.get("tokensIn") is not None:
                    b = new_bucket()
                    b["input"] = _num(md.get("tokensIn"))
                    b["output"] = _num(md.get("tokensOut"))
                    b["cache_read"] = _num(md.get("cacheReads"))
                    b["cache_write"] = _num(md.get("cacheWrites"))
                    b["requests"] = 1
                    add_bucket(totals, b)
                    dk = day_key(os.path.getmtime(meta_path))
                    if dk:
                        add_bucket(days.setdefault(dk, new_bucket()), b)
                    add_bucket(models.setdefault(md.get("model") or "unknown",
                                                 new_bucket()), b)
            except (OSError, ValueError):
                pass
        hist = None
        for name in ("api_conversation_history.json", "claude_messages.json"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        hist = json.load(f)
                    break
                except (OSError, ValueError):
                    pass
        if isinstance(hist, list):
            for m in hist:
                if not isinstance(m, dict):
                    continue
                role = pick_role(m)
                txt = pick_text(m)
                if role == "user" and not first_text:
                    first_text = real_text(txt)[:400]
                if role == "user" and real_text(txt):
                    users += 1
                    last_text = real_text(txt)[:400]
                elif role == "assistant":
                    asst += 1
                u = _find_usage(m)
                if u:
                    # 历史里通常是 Anthropic 格式，input 不含缓存
                    b = normalize_usage(u)
                    if b:
                        add_bucket(totals, b)
                        dk = day_key(pick_ts(m) or os.path.getmtime(d))
                        if dk:
                            add_bucket(days.setdefault(dk, new_bucket()), b)
                        add_bucket(models.setdefault("unknown", new_bucket()), b)
        ui = os.path.join(d, "ui_messages.json")
        if os.path.isfile(ui):
            try:
                with open(ui, encoding="utf-8", errors="replace") as f:
                    msgs = json.load(f)
                if isinstance(msgs, list):
                    for m in msgs:
                        ts = pick_ts(m)
                        if ts:
                            first_ts = ts if first_ts is None else first_ts
                            last_ts = ts
                        if m.get("say") == "task" and not title:
                            title = (m.get("text") or "")[:120]
            except (OSError, ValueError):
                pass
        if not first_ts:
            try:
                first_ts = os.path.getmtime(d)
                last_ts = first_ts
            except OSError:
                pass
        return {
            "sid": sid, "totals": totals, "days": days, "models": models,
            "meta": {"title": title, "users": users, "asst": asst,
                     "first_text": first_text, "last_text": last_text,
                     "first_ts": first_ts, "last_ts": last_ts, "cwd": ""},
        }

    def _rows(self):
        from .. import usage
        cache = usage.load_cache()
        rows = []
        for sid, d in self.task_dirs():
            e = cached_entry(cache, self.key, d, lambda p: self._parse(sid, p)) or {}
            meta = e.get("meta") or {}
            try:
                st = os.stat(d)
            except OSError:
                continue
            rows.append({
                "tool": self.key, "id": sid,
                "title": meta.get("title") or "",
                "project": os.path.basename(os.path.dirname(self.root)),
                "cwd": "",
                "size": dir_size(d), "mtime": st.st_mtime, "path": d,
                "live": False, "kind": "file",
                "models": sorted((e.get("models") or {}).keys()),
                "tokens": e.get("totals"),
                "messages": (meta.get("users") or 0) + (meta.get("asst") or 0),
                "first_ts": meta.get("first_ts"),
                "last_ts": meta.get("last_ts"),
                "note": self.note,
            })
        rows.sort(key=lambda s: (-(s.get("mtime") or 0), s["id"]))
        return rows

    def sessions(self):
        return self._rows()

    def detail(self, s):
        from .. import usage
        cache = usage.load_cache()
        e = cached_entry(cache, self.key, s["path"],
                         lambda p: self._parse(s["id"], p)) or {}
        meta = e.get("meta") or {}
        return {
            "users": meta.get("users") or 0, "asst": meta.get("asst") or 0,
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
        if self.readonly:
            out["errors"].append("只读发现，暂不支持删除")
            return out
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
                        "title": s["title"], "tool": self.key,
                        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                out["deleted"].append({"id": s["id"],
                                       "title": s["title"] or "(无标题)"})
                out["freed"] += s["size"]
            except OSError as e:
                out["errors"].append("%s: %s" % (s["title"] or s["id"][:8], e))
        out["freed_str"] = human_size(out["freed"])
        return out

    def trash_list(self):
        if self.readonly:
            return []
        out = []
        for e in self.trash().entries():
            m = e.get("meta") or {}
            out.append({"token": e["token"], "id": m.get("session_id") or "",
                        "title": m.get("title") or "",
                        "deleted_at": m.get("deleted_at") or _fmt_mtime(e["mtime"]),
                        "original_path": m.get("original_path") or "",
                        "size": e["size"], "size_str": human_size(e["size"])})
        return out

    def trash_restore(self, tokens):
        out = {"restored": [], "errors": []}
        if self.readonly:
            out["errors"].append("只读发现，不支持恢复")
            return out
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
        if self.readonly:
            return {"removed": 0, "freed": 0, "freed_str": "0B", "errors": []}
        return self.trash().empty()

    def usage_entries(self, cache):
        entries = []
        paths = set()
        for sid, d in self.task_dirs():
            paths.add(d)
            e = cached_entry(cache, self.key, d, lambda p: self._parse(sid, p))
            if e:
                entries.append(e)
        self.last_paths = paths
        return entries


# ---------- SQLite：sessions / turns / 任意消息表 ----------

_ID_COLS = ("session_id", "id", "session_key", "thread_id", "conversation_id",
            "chat_id", "uuid", "key")
_TITLE_COLS = ("derived_title", "summary", "title", "name", "label",
               "display_name", "subject", "thread_name")
_CWD_COLS = ("cwd", "directory", "workspace", "project", "path",
             "execution_workspace", "working_dir", "workspace_id")
_CREATED_COLS = ("created_at", "time_created", "created", "created_at_ms",
                 "started_at", "start_time", "started_at_ms")
_UPDATED_COLS = ("updated_at", "time_updated", "updated", "updated_at_ms",
                 "last_activity", "last_message_at")
_TEXT_COLS = ("content", "text", "message", "body", "data", "user_message",
              "prompt", "parts")
_ROLE_COLS = ("role", "type", "sender", "author")
_TS_COLS = ("created_at", "timestamp", "time", "created", "ts", "id")
_JUNK_TABLES = ("sqlite_sequence", "sqlite_stat1", "schema_version", "yoyo_lock")
_JUNK_PREFIX = ("_yoyo_", "sqlite_", "transcript_fts", "search_index",
                "session_fts")


def _pick(names, order):
    names = set(names)
    for c in order:
        if c in names:
            return c
    return None


def _is_junk_table(name):
    low = name.lower()
    if low in _JUNK_TABLES:
        return True
    return any(low.startswith(p) for p in _JUNK_PREFIX)


def _message_tables(con, exclude=("sessions", "turns")):
    """找出所有「会话 id + 正文」的表，按行数从多到少排（正文主表通常最大）。"""
    try:
        names = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in sorted(names):
        if t in exclude or _is_junk_table(t):
            continue
        try:
            cols = {r[1] for r in con.execute('pragma table_info("%s")' % t)}
        except Exception:  # noqa: BLE001
            continue
        idc = _pick(cols, _ID_COLS)
        txt = _pick(cols, _TEXT_COLS)
        if not (idc and txt):
            continue
        try:
            n = con.execute('select count(*) from "%s"' % t).fetchone()[0]
        except Exception:  # noqa: BLE001
            n = 0
        if n <= 0:
            continue
        out.append({"table": t, "idc": idc, "textc": txt,
                    "rolec": _pick(cols, _ROLE_COLS),
                    "tsc": _pick(cols, _TS_COLS), "rows": int(n)})
    out.sort(key=lambda c: (-c["rows"], 0 if c["rolec"] else 1))
    return out


def _token_bucket(cols, r):
    """有些库直接在会话表里存累计 token；有就换算成统一 bucket。"""
    inp = _num(_row_get(r, "input_tokens", "inputTokens", "prompt_tokens"))
    out = _num(_row_get(r, "output_tokens", "outputTokens", "completion_tokens"))
    cached = _num(_row_get(r, "cache_read", "cache_read_tokens",
                           "cache_read_input_tokens", "cached_tokens"))
    cw = _num(_row_get(r, "cache_write", "cache_write_tokens",
                       "cache_creation_input_tokens"))
    reasoning = _num(_row_get(r, "reasoning_tokens", "reasoning_output_tokens"))
    if not any((inp, out, cached, cw, reasoning)):
        return None
    b = new_bucket()
    b["input"] = max(0, inp - cached)
    b["cache_read"] = cached
    b["cache_write"] = cw
    b["output"] = out
    b["reasoning"] = reasoning
    b["requests"] = 0
    return b


def _row_get(r, *names):
    keys = r.keys()
    for n in names:
        if n in keys and r[n] is not None:
            return r[n]
    return None


class SqliteSessionsProvider(Provider):
    """数据库里的会话：优先用 sessions 表，没有就退回任意「消息表」。"""

    kind = "soft"
    readonly = True

    def __init__(self, key, name, root, db_name, color="#8a8f98",
                 note="只读", readonly=True):
        super().__init__(root)
        self.key = key
        self.name = name
        self.db_name = db_name
        self.color = color
        self.note = note
        self.readonly = readonly

    @property
    def db_file(self):
        return os.path.join(self.root, self.db_name)

    def available(self):
        return os.path.isfile(self.db_file)

    def _conn(self):
        import sqlite3
        con = sqlite3.connect("file:///" + self.db_file.replace("\\", "/")
                              + "?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def _tables(self, con):
        return {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}

    # -- 找数据源 --
    def _source(self, con):
        """返回描述会话来源的 dict：
        {"kind": "sessions", ...} 或 {"kind": "messages", ...}，找不到返回 None。"""
        tables = self._tables(con)
        if "sessions" in tables:
            cols = {r[1] for r in con.execute("pragma table_info(sessions)")}
            idc = _pick(cols, _ID_COLS) or next(iter(cols), None)
            if not idc:
                return None
            return {
                "kind": "sessions", "table": "sessions", "idc": idc,
                "titlec": _pick(cols, _TITLE_COLS),
                "cwdc": _pick(cols, _CWD_COLS),
                "createdc": _pick(cols, _CREATED_COLS),
                "updatedc": _pick(cols, _UPDATED_COLS),
                "tokenc": any(c in cols for c in (
                    "input_tokens", "inputTokens", "prompt_tokens",
                    "output_tokens", "outputTokens")),
            }
        # 没有 sessions 表：找一张带「会话 id + 角色 + 正文」的消息表
        for cand in _message_tables(con, exclude=()):
            if not cand["rolec"]:
                continue
            return {
                "kind": "messages", "table": cand["table"], "idc": cand["idc"],
                "textc": cand["textc"], "rolec": cand["rolec"],
                "tsc": cand["tsc"],
            }
        return None

    # -- 列表 --
    def quick_stats(self):
        try:
            con = self._conn()
            try:
                src = self._source(con)
                if not src:
                    return 0, os.path.getsize(self.db_file)
                if src["kind"] == "sessions":
                    n = con.execute('select count(*) from "%s"'
                                    % src["table"]).fetchone()[0]
                else:
                    n = con.execute('select count(distinct "%s") from "%s"'
                                    % (src["idc"], src["table"])).fetchone()[0]
            finally:
                con.close()
            return int(n), os.path.getsize(self.db_file)
        except Exception:  # noqa: BLE001
            return 0, 0

    def _message_stats(self, con, cand, idc=None, table=None):
        """按会话统计消息数 / 正文长度 / 最后一次时间 / 首条用户消息。"""
        table = table or cand["table"]
        idc = idc or cand["idc"]
        textc = cand.get("textc")
        rolec = cand.get("rolec")
        tsc = cand.get("tsc")
        if textc is None:
            cols = {r[1] for r in con.execute('pragma table_info("%s")' % table)}
            textc = _pick(cols, _TEXT_COLS)
        sel = ['"%s" as _id' % idc, "count(*) as n"]
        sel.append(('sum(length("%s"))' % textc) if textc else "0")
        sel.append(('max("%s")' % tsc) if tsc else "null")
        sql = 'select %s from "%s" group by "%s"' % (", ".join(sel), table, idc)
        out = {}
        try:
            for r in con.execute(sql):
                out[str(r["_id"])] = {
                    "n": r[1], "bytes": int(r[2] or 0),
                    "last": to_epoch(r[3]),
                }
        except Exception:  # noqa: BLE001
            return out
        if textc and rolec:
            try:
                q = ('select "%s" as _id, "%s" as _txt from "%s" '
                     'where lower("%s") in (\'user\', \'human\')'
                     % (idc, textc, table, rolec))
                if tsc:
                    q += ' order by "%s"' % tsc
                for r in con.execute(q):
                    sid = str(r["_id"])
                    if sid in out and not out[sid].get("first"):
                        out[sid]["first"] = (r["_txt"] or "")[:400]
            except Exception:  # noqa: BLE001
                pass
        return out

    def _rows(self):
        try:
            con = self._conn()
        except Exception:  # noqa: BLE001
            return []
        try:
            src = self._source(con)
            if not src:
                return []
            if src["kind"] == "sessions":
                return self._rows_sessions(con, src)
            return self._rows_messages(con, src)
        except Exception:  # noqa: BLE001
            return []
        finally:
            con.close()

    def _rows_sessions(self, con, src):
        cols = {r[1] for r in con.execute('pragma table_info("sessions")')}
        title_cols = [c for c in _TITLE_COLS if c in cols]
        title_sel = ("coalesce(%s)" % ", ".join('"%s"' % c for c in title_cols)
                     if title_cols else "null")
        sel = ['"%s" as _id' % src["idc"], "%s as _title" % title_sel]
        for alias, col in (("_cwd", src["cwdc"]),
                           ("_created", src["createdc"]),
                           ("_updated", src["updatedc"])):
            sel.append(('"%s" as %s' % (col, alias)) if col
                       else "null as %s" % alias)
        sel.append("*")
        rows = con.execute(
            'select %s from "sessions"' % ", ".join(sel)).fetchall()

        # 消息统计：优先 turns 表，否则用最大的那张消息表
        stats = {}
        tables = self._tables(con)
        if "turns" in tables:
            tcols = {r[1] for r in con.execute("pragma table_info(turns)")}
            idc = _pick(tcols, ("session_id", "session_key", "thread_id"))
            if idc:
                stats = self._message_stats(con, {
                    "table": "turns", "idc": idc, "rolec": None, "tsc": None,
                })
        if not stats:
            cands = _message_tables(con)
            if cands:
                stats = self._message_stats(con, cands[0])

        out = []
        for r in rows:
            sid = str(r["_id"])
            st = stats.get(sid) or {}
            tokens = _token_bucket(cols, r) if src.get("tokenc") else None
            created = to_epoch(r["_created"]) or to_epoch(st.get("last"))
            updated = to_epoch(r["_updated"]) or to_epoch(st.get("last"))
            out.append({
                "tool": self.key, "id": sid,
                "title": (str(r["_title"]) if r["_title"] else "")
                or st.get("first") or "",
                "project": r["_cwd"] or "", "cwd": r["_cwd"] or "",
                "size": st.get("bytes") or 0,
                "mtime": updated or created or 0,
                "path": self.db_file, "live": False, "kind": self.kind,
                "models": [], "tokens": tokens,
                "messages": st.get("n"),
                "first_ts": created, "last_ts": updated,
                "note": self.note,
            })
        return out

    def _rows_messages(self, con, src):
        table, idc = src["table"], src["idc"]
        stats = self._message_stats(con, src, idc)
        out = []
        for sid, st in stats.items():
            created = to_epoch(st.get("last"))
            out.append({
                "tool": self.key, "id": sid,
                "title": st.get("first") or "",
                "project": "", "cwd": "",
                "size": st.get("bytes") or 0,
                "mtime": created or 0,
                "path": self.db_file, "live": False, "kind": self.kind,
                "models": [], "tokens": None,
                "messages": st.get("n"),
                "first_ts": created, "last_ts": created,
                "note": self.note,
            })
        return out

    def sessions(self):
        return self._rows()

    # -- 详情 --
    def detail(self, s):
        try:
            con = self._conn()
        except Exception:  # noqa: BLE001
            return {}
        try:
            src = self._source(con)
            if not src:
                return {}
            table, idc = src["table"], src["idc"]
            cols = {r[1] for r in con.execute('pragma table_info("%s")' % table)}
            textc = _pick(cols, _TEXT_COLS)
            rolec = _pick(cols, _ROLE_COLS)
            if src["kind"] == "sessions":
                # 在消息表里找这个会话的提问（多张表时按行数优先，接着往下找）
                for cand in _message_tables(con):
                    if not cand["rolec"]:
                        continue
                    q = ('select "%s" as _txt from "%s" where "%s"=? '
                         'and lower("%s") in (\'user\', \'human\')'
                         % (cand["textc"], cand["table"], cand["idc"],
                            cand["rolec"]))
                    if cand["tsc"]:
                        q += ' order by "%s"' % cand["tsc"]
                    vals = [r["_txt"] for r in con.execute(q, (s["id"],))
                            if r["_txt"]]
                    if vals:
                        return {"users": len(vals), "asst": None,
                                "first_text": str(vals[0])[:600],
                                "last_text": str(vals[-1])[:600],
                                "start_ts": "", "end_ts": "",
                                "lines": None, "tokens": s.get("tokens"),
                                "models": []}
                return {"users": None, "asst": None, "first_text": "",
                        "last_text": "", "start_ts": "", "end_ts": "",
                        "lines": None, "tokens": s.get("tokens"), "models": []}
            # messages 源：直接查这张表
            tts = _pick(cols, _TS_COLS)
            q = ('select "%s" as _txt from "%s" where "%s"=? '
                 'and lower("%s") in (\'user\', \'human\')'
                 % (textc, table, idc, rolec))
            if tts:
                q += ' order by "%s"' % tts
            vals = [r["_txt"] for r in con.execute(q, (s["id"],)) if r["_txt"]]
            return {
                "users": len(vals) or None, "asst": None,
                "first_text": str(vals[0])[:600] if vals else "",
                "last_text": str(vals[-1])[:600] if vals else "",
                "start_ts": "", "end_ts": "", "lines": None,
                "tokens": s.get("tokens"), "models": [],
            }
        except Exception:  # noqa: BLE001
            return {}
        finally:
            con.close()

    def delete(self, ids, purge=False, force=False):
        return {"deleted": [], "skipped_live": [],
                "errors": ["「%s」目前是只读发现，暂不支持删除" % self.name],
                "freed": 0, "freed_str": "0B", "purge": purge}

    def trash_list(self):
        return []

    def trash_restore(self, tokens):
        return {"restored": [], "errors": ["只读发现，不支持恢复"]}

    def trash_empty(self):
        return {"removed": 0, "freed": 0, "freed_str": "0B", "errors": []}

    def usage_entries(self, cache):
        self.last_paths = None
        return []

    def token_map(self, cache=None):
        return {}


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _fmt_mtime(ts):
    return time.strftime("%y-%m-%d %H:%M", time.localtime(ts))
