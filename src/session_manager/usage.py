#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 用量：解析缓存 + 汇总。

会话文件动辄几百 MB，全量重扫很慢，所以按 (size, mtime) 缓存每个文件的
解析结果（分日 / 分模型 / 会话总量），只重扫变化的文件。

缓存落在用户缓存目录：
    Windows  %LOCALAPPDATA%\\AISessionManager\\usage-cache.json
    macOS    ~/Library/Caches/AISessionManager/usage-cache.json
    Linux    ~/.cache/ai-session-manager/usage-cache.json
可用环境变量 AISM_CACHE_DIR 覆盖（测试用）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

try:
    from .providers.base import (BUCKET_KEYS, add_bucket, bucket_total,
                                 new_bucket)
except ImportError:  # 直接跑脚本
    from providers.base import (BUCKET_KEYS, add_bucket, bucket_total,  # type: ignore
                                new_bucket)

CACHE_VERSION = 2
_CACHE_NAME = "usage-cache.json"
_LOCK = threading.Lock()


def cache_dir():
    override = os.environ.get("AISM_CACHE_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    home = os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "AISessionManager")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", "AISessionManager")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    return os.path.join(base, "ai-session-manager")


def cache_path():
    return os.path.join(cache_dir(), _CACHE_NAME)


def load_cache():
    try:
        with open(cache_path(), encoding="utf-8") as f:
            c = json.load(f)
        if isinstance(c, dict) and c.get("version") == CACHE_VERSION:
            c.setdefault("files", {})
            return c
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "files": {}}


def save_cache(cache):
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


def _model_bucket(models, model, src):
    b = models.setdefault(model or "unknown", new_bucket())
    add_bucket(b, src)


def collect(providers, force=False):
    """扫描全部 provider 的 token 用量。返回可直接 JSON 化的结果。"""
    with _LOCK:
        cache = {"version": CACHE_VERSION, "files": {}} if force else load_cache()
        tools = []
        grand = new_bucket()
        all_days = {}
        live_keys = set()
        for p in providers:
            row = {
                "key": p.key, "name": p.name, "color": p.color,
                "installed": p.available(), "root": p.root,
                "totals": new_bucket(), "days": {}, "models": {},
                "sessions": 0, "approximate": False, "scanned_at": time.time(),
            }
            if row["installed"]:
                try:
                    entries = p.usage_entries(cache) or []
                except Exception:  # noqa: BLE001
                    entries = []
                row["approximate"] = bool(getattr(p, "approximate_usage", False))
                for e in entries:
                    if not e:
                        continue
                    sid = e.get("sid")
                    if sid:
                        row["sessions"] += 1
                    add_bucket(row["totals"], e.get("totals"))
                    for day, b in (e.get("days") or {}).items():
                        add_bucket(row["days"].setdefault(day, new_bucket()), b)
                        add_bucket(all_days.setdefault(day, {}).setdefault(
                            p.key, new_bucket()), b)
                    for model, b in (e.get("models") or {}).items():
                        _model_bucket(row["models"], model, b)
                # 清理该工具下已经不存在的文件的缓存（provider 报告了扫描清单才做）
                last = getattr(p, "last_paths", None)
                if last is not None:
                    prefix = p.key + "|"
                    for k in [k for k in cache["files"] if k.startswith(prefix)]:
                        if k[len(prefix):] not in last:
                            cache["files"].pop(k, None)
            if not row["installed"]:
                row["approximate"] = False
            add_bucket(grand, row["totals"])
            tools.append(row)
        save_cache(cache)
        return {
            "generated": time.time(),
            "totals": grand,
            "days": all_days,
            "tools": [tool_json(t) for t in tools],
            "cache_file": cache_path(),
        }


def tool_json(row):
    models = [{"model": m, "tokens": bucket_total(b), "bucket": b}
              for m, b in row["models"].items()]
    models.sort(key=lambda x: -x["tokens"])
    return {
        "key": row["key"], "name": row["name"], "color": row["color"],
        "installed": row["installed"], "root": row["root"],
        "totals": row["totals"],
        "total": bucket_total(row["totals"]),
        "days": row["days"],
        "models": models,
        "sessions": row["sessions"],
        "approximate": row["approximate"],
    }


def cached_token_map(cache, tool):
    """从缓存里取 {sid: bucket}；不触发扫描。"""
    out = {}
    prefix = tool + "|"
    for key, entry in (cache.get("files") or {}).items():
        if not key.startswith(prefix):
            continue
        sid = entry.get("sid")
        if not sid:
            continue
        b = out.setdefault(sid, new_bucket())
        add_bucket(b, entry.get("totals"))
    return out


def entry_for(cache, tool, path):
    return (cache.get("files") or {}).get(tool + "|" + path)


def cached_tool_summary(cache, tool):
    """侧边栏用：从缓存汇总某个工具的 token（不触发扫描）。"""
    totals = new_bucket()
    models = {}
    days = {}
    for key, entry in (cache.get("files") or {}).items():
        if not key.startswith(tool + "|"):
            continue
        add_bucket(totals, entry.get("totals"))
        for m, b in (entry.get("models") or {}).items():
            add_bucket(models.setdefault(m, new_bucket()), b)
        for d, b in (entry.get("days") or {}).items():
            add_bucket(days.setdefault(d, new_bucket()), b)
    return {"totals": totals, "total": bucket_total(totals),
            "models": models, "days": days}


def clear_cache():
    try:
        os.remove(cache_path())
        return True
    except OSError:
        return False


def temp_root():
    """测试用：给一个空的缓存目录。"""
    return tempfile.mkdtemp(prefix="aism-cache-")
