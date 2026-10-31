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
    from . import pricing
    from .providers.base import (BUCKET_KEYS, add_bucket, bucket_total,
                                 new_bucket)
except ImportError:  # 直接跑脚本
    import pricing  # type: ignore
    from providers.base import (BUCKET_KEYS, add_bucket, bucket_total,  # type: ignore
                                new_bucket)

# 5 → 6：bucket 增加 input_off 等峰谷拆分键，旧缓存没有这些键
# 6 → 7：Claude 记录按 message.id 区分中转（OpenAI 口径）与官方口径，
#        中转的 input_tokens 要减掉缓存读，旧缓存里是没减过的数
CACHE_VERSION = 7
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
        cfg = pricing.load(cache_dir())
        prices = cfg["models"]
        cache = {"version": CACHE_VERSION, "files": {}} if force else load_cache()
        # 高峰时段变了，峰谷拆分要重算，缓存直接作废
        if cache.get("peak") != cfg["peak"]:
            cache = {"version": CACHE_VERSION, "files": {}}
        cache["peak"] = cfg["peak"]
        tools = []
        grand = new_bucket()
        all_days = {}
        all_hours = {}
        live_keys = set()
        for p in providers:
            row = {
                "key": p.key, "name": p.name, "color": p.color,
                "installed": p.available(), "root": p.root,
                "totals": new_bucket(), "days": {}, "models": {},
                "model_days": {}, "model_hours": {},
                "model_cost_days": {}, "model_cost_hours": {},
                "cost_days": {}, "cost_hours": {}, "cost": 0.0,
                "tracks_errors": bool(getattr(p, "tracks_errors", False)),
                "tracks_timing": bool(getattr(p, "tracks_timing", False)),
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
                    # 按日 × 模型只保留总量，工作台的模型趋势图用；
                    # 顺手按模型价目折算成「API 等价」成本，按天存一份
                    for day, by_model in (e.get("mdays") or {}).items():
                        slot = row["model_days"].setdefault(day, {})
                        cslot = row["model_cost_days"].setdefault(day, {})
                        for model, b in by_model.items():
                            slot[model] = slot.get(model, 0) + bucket_total(b)
                            amt = pricing.cost_bucket(model, b, prices)
                            cslot[model] = cslot.get(model, 0.0) + amt
                            row["cost_days"][day] = (
                                row["cost_days"].get(day, 0.0) + amt)
                    # 最近几天的「小时」明细：工作台选「当天」时按小时拆
                    for hour, b in (e.get("hours") or {}).items():
                        add_bucket(all_hours.setdefault(hour, {}).setdefault(
                            p.key, new_bucket()), b)
                    for hour, by_model in (e.get("mhours") or {}).items():
                        slot = row["model_hours"].setdefault(hour, {})
                        cslot = row["model_cost_hours"].setdefault(hour, {})
                        for model, b in by_model.items():
                            slot[model] = slot.get(model, 0) + bucket_total(b)
                            # 工作台选「价格」且粒度是小时时用（只覆盖最近两三天）
                            amt = pricing.cost_bucket(model, b, prices)
                            cslot[model] = cslot.get(model, 0.0) + amt
                            row["cost_hours"][hour] = (
                                row["cost_hours"].get(hour, 0.0) + amt)
                # 清理该工具下已经不存在的文件的缓存（provider 报告了扫描清单才做）
                last = getattr(p, "last_paths", None)
                if last is not None:
                    prefix = p.key + "|"
                    for k in [k for k in cache["files"] if k.startswith(prefix)]:
                        if k[len(prefix):] not in last:
                            cache["files"].pop(k, None)
            if not row["installed"]:
                row["approximate"] = False
            row["cost"] = sum(row["cost_days"].values())
            add_bucket(grand, row["totals"])
            tools.append(row)
        save_cache(cache)
        cutoff = recent_hours_cutoff()
        return {
            "generated": time.time(),
            "totals": grand,
            "days": all_days,
            # 小时明细只给最近两天，够「当天」视图用，响应也不至于变大
            "hours": {k: v for k, v in all_hours.items() if k[:10] >= cutoff},
            "tools": [tool_json(t, cutoff, prices) for t in tools],
            "cache_file": cache_path(),
            "prices_file": pricing.prices_path(cache_dir()),
            "peak": cfg["peak"],
            "cny_rate": pricing.cny_rate(),
        }


def recent_hours_cutoff():
    return time.strftime("%Y-%m-%d",
                         time.localtime(time.time() - 2 * 86400))


def tool_json(row, hours_cutoff=None, prices=None):
    models = [{"model": m, "tokens": bucket_total(b), "bucket": b,
               "cost": round(pricing.cost_bucket(m, b, prices), 6),
               "price": pricing.price_of(m, prices),
               "price_cny": pricing.price_cny(m, prices),
               "price_known": pricing.known(m, prices)}
              for m, b in row["models"].items()]
    models.sort(key=lambda x: -x["tokens"])
    return {
        "key": row["key"], "name": row["name"], "color": row["color"],
        "installed": row["installed"], "root": row["root"],
        "totals": row["totals"],
        "total": bucket_total(row["totals"]),
        "days": row["days"],
        "models": models,
        "model_days": row["model_days"],
        "model_hours": {k: v for k, v in row["model_hours"].items()
                        if hours_cutoff is None or k[:10] >= hours_cutoff},
        "model_cost_days": row.get("model_cost_days") or {},
        "model_cost_hours": {k: v for k, v in (row.get("model_cost_hours") or {}).items()
                             if hours_cutoff is None or k[:10] >= hours_cutoff},
        "cost": round(row.get("cost") or 0.0, 6),
        "cost_days": row.get("cost_days") or {},
        "cost_hours": {k: v for k, v in (row.get("cost_hours") or {}).items()
                       if hours_cutoff is None or k[:10] >= hours_cutoff},
        "tracks_errors": row.get("tracks_errors", False),
        "tracks_timing": row.get("tracks_timing", False),
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
