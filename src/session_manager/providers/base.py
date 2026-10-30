#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各家 AI 客户端的适配层：统一接口、公共小工具、文件回收站。

每个 provider 负责把自己的会话数据翻译成统一结构：

    {
      "tool": "codex",             # provider key
      "id": "...",                 # 会话 id（全局唯一由 tool 前缀区分）
      "title": "...",              # 标题
      "project": "C:/x/y",         # 项目路径（或数据目录里的项目名）
      "cwd": "C:/x/y",
      "size": 1234,                # 磁盘占用
      "mtime": 1790000000.0,       # 最后活动时间（epoch 秒）
      "path": "...",               # 数据位置
      "live": False,               # 是否是正在运行的会话
      "kind": "file" | "soft",     # 删除方式：移文件 / 数据库软删除
    }

Token 用量统一成 bucket：

    {"input": 非缓存输入, "cache_read": 缓存读, "cache_write": 缓存写,
     "output": 输出, "reasoning": 推理（已含在 output 里，仅展示用）,
     "requests": 成功请求数, "errors": 失败请求数,
     "gen_ms": 有耗时记录的请求总耗时（毫秒）,
     "gen_tokens": 这些请求的输出 token,
     "input_off"/"cache_read_off"/"cache_write_off"/"output_off":
         落在谷时窗口里的那部分 token（是上面四项的子集，只用来算峰谷价）}

后三项只给能解析出来的 provider 填（见 Provider.tracks_errors /
tracks_timing），解析不出来的保持 0，工作台会显示「—」而不是假装 0。
"""
from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import time

try:
    from .. import pricing
except ImportError:  # 直接跑脚本
    import pricing  # type: ignore

HOME = os.path.expanduser("~")
META_SUFFIX = ".meta.json"
TRASH_NAME = "session-trash"
APP_TRASH = ".ai-session-manager"

BUCKET_KEYS = ("input", "cache_read", "cache_write", "output",
               "reasoning", "requests", "errors", "gen_ms", "gen_tokens",
               "input_off", "cache_read_off", "cache_write_off", "output_off")

_TOKEN_KEYS = ("input", "cache_read", "cache_write", "output")


# ---------- token 计数 ----------

def new_bucket():
    return {k: 0 for k in BUCKET_KEYS}


def add_bucket(dst, src=None, **kw):
    """把 src 累加进 dst；也可用关键字直接加。"""
    if src:
        for k in BUCKET_KEYS:
            dst[k] = dst.get(k, 0) + (src.get(k, 0) or 0)
    for k, v in kw.items():
        if k in dst:
            dst[k] += v or 0
    return dst


def merge_bucket(dst, src):
    return add_bucket(dst, src)


def add_usage_buckets(days, models, mdays, ts, model, b,
                      hours=None, mhours=None):
    """按日 / 按模型 / 按「日 × 模型」三份汇总一起累加。

    工作台的模型趋势图需要知道每个模型每天用了多少，所以解析时一并记下来；
    三处口径保持一致，避免各 provider 自己拼出不同的结果。

    落在谷时窗口里的记录，除了照常累加，还会多记一份 *_off 键，成本按
    峰价 / 谷价分别算（见 pricing.cost_bucket）。

    hours / mhours 只保留最近几天的「小时」明细：工作台选「当天」时要把一天
    拆成 24 段，太久以前的小时数据既用不上，也没必要留在缓存里。
    """
    name = model or "unknown"
    off = _off_bucket(ts, b)
    _merge_bucket(models.setdefault(name, new_bucket()), b, off)
    day = day_key(ts) if ts is not None else ""
    if not day:
        return
    _merge_bucket(days.setdefault(day, new_bucket()), b, off)
    _merge_bucket(mdays.setdefault(day, {}).setdefault(name, new_bucket()), b, off)
    if hours is not None and day >= recent_day_threshold():
        hk = day + "T" + time.strftime("%H", time.localtime(ts))
        _merge_bucket(hours.setdefault(hk, new_bucket()), b, off)
        if mhours is not None:
            _merge_bucket(mhours.setdefault(hk, {}).setdefault(name, new_bucket()),
                          b, off)


def _merge_bucket(dst, b, off):
    add_bucket(dst, b)
    if off:
        add_bucket(dst, off)


def _off_bucket(ts, b):
    """谷时窗口内的记录 → 额外的 *_off 桶；不在窗口内返回 None。"""
    if ts is None or not pricing.is_offpeak(ts):
        return None
    out = {k + "_off": b.get(k) for k in _TOKEN_KEYS if b.get(k)}
    return out or None


_RECENT_HOURS_DAYS = 3
_RECENT_THRESHOLD = [None, ""]


def recent_day_threshold():
    """小时明细的保留窗口（YYYY-MM-DD），按天刷新，避免每条记录都算一次。"""
    today = time.strftime("%Y-%m-%d")
    if _RECENT_THRESHOLD[0] != today:
        _RECENT_THRESHOLD[0] = today
        _RECENT_THRESHOLD[1] = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - _RECENT_HOURS_DAYS * 86400))
    return _RECENT_THRESHOLD[1]


def bucket_total(b):
    return (b.get("input", 0) + b.get("cache_read", 0)
            + b.get("cache_write", 0) + b.get("output", 0))


def bucket_empty(b):
    return not any(b.get(k) for k in BUCKET_KEYS)


# ---------- 时间 ----------

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def iso_to_epoch(s):
    """ISO8601（UTC，带 Z）→ epoch 秒；解析失败返回 None。"""
    if not isinstance(s, str):
        return None
    m = _ISO_RE.match(s)
    if not m:
        return None
    try:
        y, mo, d, h, mi, se = (int(x) for x in m.groups())
        return float(calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


def to_epoch(v):
    """兼容 ISO 字符串 / 毫秒 / 秒时间戳。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        # 13 位是毫秒
        return float(v) / 1000.0 if v > 100000000000 else float(v)
    return iso_to_epoch(v)


def day_key(epoch):
    if epoch is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(epoch))
    except (ValueError, OSError, OverflowError):
        return ""


def fmt_mtime(ts):
    return time.strftime("%y-%m-%d %H:%M", time.localtime(ts))


# ---------- 文件小工具 ----------

def dir_size(path):
    try:
        if not os.path.isdir(path):
            return os.path.getsize(path)
    except OSError:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def app_trash_root(key):
    """自动发现的客户端：回收站统一放到 ~/.ai-session-manager/trash/<key>，
    不往对方的数据目录里塞东西。"""
    return os.path.join(HOME, APP_TRASH, "trash", key)


def safe_token(token):
    """回收站 token 必须是纯文件名，防止 ../ 逃逸。"""
    token = str(token or "")
    if not token or token != os.path.basename(token) or token.endswith(META_SUFFIX):
        raise ValueError("非法的回收站条目: %s" % token)
    return token


def short_path(p, max_components=2):
    if not p:
        return ""
    norm = p.replace("\\", "/")
    home = HOME.replace("\\", "/").rstrip("/")
    if norm.lower().startswith(home.lower()):
        norm = "~" + norm[len(home):]
    parts = [x for x in norm.split("/") if x]
    if len(parts) > max_components:
        norm = "…/" + "/".join(parts[-max_components:])
    return norm.replace("/", "\\") if os.name == "nt" else norm


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024
    return "%.1fT" % n


# ---------- 文件回收站 ----------

class FileTrash:
    """把会话文件/目录移到 <root> 下，旁边放一份 .meta.json 记录原路径。"""

    def __init__(self, root):
        self.root = root

    def entries(self):
        out = []
        if not os.path.isdir(self.root):
            return out
        for fn in sorted(os.listdir(self.root), reverse=True):
            if fn.endswith(META_SUFFIX):
                continue
            p = os.path.join(self.root, fn)
            meta = {}
            mp = p + META_SUFFIX
            if os.path.exists(mp):
                try:
                    with open(mp, encoding="utf-8") as f:
                        meta = json.load(f) or {}
                except (OSError, ValueError):
                    meta = {}
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({
                "path": p, "token": fn, "meta": meta,
                "size": dir_size(p), "mtime": st.st_mtime,
            })
        return out

    def move(self, src, token, meta):
        os.makedirs(self.root, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(self.root, "%s_%s" % (stamp, token))
        i = 1
        while os.path.exists(dest) or os.path.exists(dest + META_SUFFIX):
            i += 1
            dest = os.path.join(self.root, "%s_%d_%s" % (stamp, i, token))
        shutil.move(src, dest)
        with open(dest + META_SUFFIX, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return dest

    def restore(self, token):
        token = safe_token(token)
        src = os.path.join(self.root, token)
        meta_path = src + META_SUFFIX
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f) or {}
            except (OSError, ValueError):
                meta = {}
        dest = meta.get("original_path")
        if not dest:
            return None, "缺少原始路径信息，无法恢复"
        if os.path.exists(dest):
            stem, ext = os.path.splitext(dest)
            dest = "%s_restored%s" % (stem, ext)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            if os.path.exists(meta_path):
                os.remove(meta_path)
        except OSError as e:
            return None, "%s: %s" % (token, e)
        return dest, None

    def empty(self):
        out = {"removed": 0, "freed": 0, "errors": []}
        for e in self.entries():
            for p in (e["path"], e["path"] + META_SUFFIX):
                if not os.path.exists(p):
                    continue
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                    out["removed"] += 1
                    if p == e["path"]:
                        out["freed"] += e["size"]
                except OSError as ex:
                    out["errors"].append("%s: %s" % (os.path.basename(p), ex))
        out["freed_str"] = human_size(out["freed"])
        return out


# ---------- 解析缓存 ----------

def cached_entry(cache, tool, path, parser):
    """按 (size, mtime) 缓存单个文件的解析结果；变了才重新解析。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = tool + "|" + path
    cur = cache.get("files", {}).get(key)
    if (cur and cur.get("size") == st.st_size
            and cur.get("mtime") == int(st.st_mtime)):
        return cur
    entry = parser(path)
    if entry is None:
        return None
    entry["size"] = st.st_size
    entry["mtime"] = int(st.st_mtime)
    cache.setdefault("files", {})[key] = entry
    return entry


# ---------- Provider 基类 ----------

class Provider:
    key = ""
    name = ""
    color = "#b95435"
    hint = ""
    kind = "file"
    # 能否从会话记录里看出失败请求 / 单次请求耗时（工作台据此决定显示数字还是「—」）
    tracks_errors = False
    tracks_timing = False

    def __init__(self, root=None):
        self._root = os.path.abspath(os.path.expanduser(root)) if root else None

    @property
    def root(self):
        return self._root or os.path.join(HOME, "." + self.key)

    # -- 元信息 --
    def available(self):
        return os.path.isdir(self.root)

    def live_id(self):
        return ""

    def session_count(self):
        try:
            return len(self.sessions())
        except Exception:  # noqa: BLE001
            return 0

    def storage_size(self):
        try:
            return sum(s.get("size") or 0 for s in self.sessions())
        except Exception:  # noqa: BLE001
            return 0

    def quick_stats(self):
        """侧边栏用：尽量不解析正文，只数文件/查行数。"""
        return self.session_count(), self.storage_size()

    # -- 会话 --
    def sessions(self):
        return []

    def get(self, sid):
        for s in self.sessions():
            if s["id"] == sid:
                return s
        return None

    def detail(self, session):
        return {}

    def resume_cmd(self, session):
        return ""

    # -- 删除 --
    def delete(self, ids, purge=False, force=False):
        raise NotImplementedError

    def trash_list(self):
        return []

    def trash_restore(self, tokens):
        return {"restored": [], "errors": []}

    def trash_empty(self):
        return {"removed": 0, "freed": 0, "freed_str": "0B", "errors": []}

    # -- token 用量 --
    def quick_usage(self, cache=None):
        """侧边栏用：能只靠缓存/一次轻查询得到的 token 概览。"""
        from .. import usage
        if cache is None:
            cache = usage.load_cache()
        s = usage.cached_tool_summary(cache, self.key)
        return {
            "totals": s["totals"], "total": s["total"],
            "models": {m: bucket_total(b) for m, b in s["models"].items()},
        }

    def usage_entries(self, cache):
        """返回 [{sid, totals, days, models}]，由 usage.collect 汇总。"""
        return []

    def token_map(self, cache=None):
        """{会话 id: bucket}，只读缓存，不触发扫描。"""
        from .. import usage
        if cache is None:
            cache = usage.load_cache()
        return usage.cached_token_map(cache, self.key)


def session_json(s, extra=None):
    """统一会话 → JSON（前端用）。"""
    d = {
        "id": s.get("id", ""),
        "tool": s.get("tool", ""),
        "title": s.get("title") or "",
        "project": s.get("project") or "",
        "project_short": short_path(s.get("project") or "", 2),
        "cwd": s.get("cwd") or "",
        "size": s.get("size") or 0,
        "size_str": human_size(s.get("size") or 0),
        "mtime": s.get("mtime") or 0,
        "mtime_str": fmt_mtime(s.get("mtime") or 0) if s.get("mtime") else "",
        "path": s.get("path") or "",
        "live": bool(s.get("live")),
        "kind": s.get("kind") or "file",
        "messages": s.get("messages"),
        "turns": s.get("turns"),
        "models": s.get("models") or [],
        "tokens": s.get("tokens"),
        "note": s.get("note") or "",
        "untitled": not s.get("title"),
    }
    if extra:
        d.update(extra)
    return d
