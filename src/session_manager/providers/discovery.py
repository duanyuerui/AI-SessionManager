#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发现本机有哪些 AI 客户端留下了会话数据。

三路来源：
    1. 固定 provider：Claude Code / Codex / DSH / WorkBuddy / ZCode（每台机器都列，
       没装显示「未安装」）
    2. 已知客户端清单（catalog.SPECS）：路径存在且有会话才加入
    3. 启发式扫描：家目录隐藏目录 + AppData / Application Support / .config /
       .local/share 里名字像 AI 的目录，找到会话文件或 sessions 表才加入

结果缓存起来；/api/tools?scan=1 或 CLI 的 tools 会强制重扫。
"""
from __future__ import annotations

import json
import os
import re
import threading

from . import catalog
from .atomcode import AtomCodeProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .dsh import DshProvider
from .generic import (GenericFileProvider, SqliteSessionsProvider,
                      TaskDirProvider)
from .opensquilla import OpenSquillaProvider
from .workbuddy import WorkbuddyProvider
from .zcode import ZcodeProvider

PRIMARY = [ClaudeProvider, CodexProvider, DshProvider,
           WorkbuddyProvider, ZcodeProvider]
PRIMARY_KEYS = [c.key for c in PRIMARY]

_LOCK = threading.RLock()
_CACHE = {"items": None, "at": 0.0}
_SCAN_TTL = 30.0

_PRIMARY_NAMES = {"claude", "codex", "dsh", "workbuddy", "zcode"}


def _root_override(key):
    env = "AISM_%s_ROOT" % re.sub(r"[^A-Za-z0-9]", "_", key).upper()
    v = os.environ.get(env)
    return os.path.expanduser(v) if v else None


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "x"


def _spec_provider(spec):
    key = spec["key"]
    root = _root_override(key) or catalog.p(spec["root"])
    adapter = spec.get("adapter")
    if adapter == "files":
        return GenericFileProvider(key, spec["name"], root, spec["globs"])
    if adapter == "taskdir":
        return TaskDirProvider(key, spec["name"], root)
    if adapter == "sqlite":
        return SqliteSessionsProvider(key, spec["name"], root,
                                      spec.get("db") or "session-store.db")
    if adapter == "atomcode":
        return AtomCodeProvider(root)
    if adapter == "opensquilla":
        return OpenSquillaProvider(root)
    return None


def _vscode_global_storage(app):
    bases = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        bases.append(os.path.join(appdata, app, "User", "globalStorage"))
    home = os.path.expanduser("~")
    bases.append(os.path.join(home, "Library", "Application Support", app,
                              "User", "globalStorage"))
    bases.append(os.path.join(home, ".config", app, "User", "globalStorage"))
    return bases


def _detect_extension(key, name, path):
    """在 VS Code 扩展数据目录里找会话存储。"""
    db = os.path.join(path, "session-store.db")
    if os.path.isfile(db):
        return SqliteSessionsProvider(key, name, path, "session-store.db")

    tasks = os.path.join(path, "tasks")
    if os.path.isdir(tasks):
        try:
            dirs = [d for d in os.listdir(tasks)
                    if os.path.isdir(os.path.join(tasks, d))]
        except OSError:
            dirs = []
        if dirs:
            return TaskDirProvider(key, name, tasks)

    # 通用 JSON / JSONL 会话文件
    files = _find_session_files(path)
    if files:
        globs = _globs_for(files, path)
        if globs:
            return GenericFileProvider(key, name, path, globs)
    return None


def _vscode_providers(seen_roots, seen_keys):
    out = []
    for app in catalog.VSCODE_APPS:
        for gs in _vscode_global_storage(app):
            if not os.path.isdir(gs):
                continue
            try:
                entries = sorted(os.listdir(gs))
            except OSError:
                continue
            for ext in entries:
                d = os.path.join(gs, ext)
                if not os.path.isdir(d):
                    continue
                known = catalog.VSCODE_EXT.get(ext)
                if known is None and not any(
                        k in ext.lower() for k in catalog.AI_KEYWORDS):
                    continue
                key, name = known or ("x-" + _slug(ext), ext)
                override = _root_override(key)
                if key in _PRIMARY_NAMES:
                    continue
                target = override or d
                if target in seen_roots:
                    continue
                if key in seen_keys:
                    key = "%s-%s" % (key, _slug(app))
                p = _detect_extension(key, name, target)
                if p is None:
                    continue
                if p.root in seen_roots:
                    continue
                try:
                    n, _ = p.quick_stats()
                except Exception:  # noqa: BLE001
                    n = 0
                if n <= 0:
                    continue
                p.via = "vscode:%s" % app
                out.append(p)
                seen_roots.add(p.root)
                seen_keys.add(p.key)
    return out


def _looks_like_session(path):
    """读文件头，看像不像一段对话。"""
    try:
        if os.path.getsize(path) <= 0:
            return False
        with open(path, "rb") as f:
            head = f.read(32768).decode("utf-8", errors="replace")
    except OSError:
        return False
    low = head.lower()
    if '"role"' in low and ('"user"' in low or '"assistant"' in low):
        return True
    if '"type"' in low and ('"user"' in low or '"human"' in low):
        return True
    if '"messages"' in low and '"content"' in low:
        return True
    return False


def _find_session_files(root, limit=400):
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [d for d in dirs
                   if d.lower() not in catalog.SKIP_DIRS and not d.startswith(".")]
        if depth >= 5:
            dirs[:] = []
        for fn in files:
            if not fn.endswith((".jsonl", ".json")):
                continue
            p = os.path.join(dirpath, fn)
            if _looks_like_session(p):
                out.append(p)
                if len(out) >= limit:
                    return out
    return out


def _globs_for(files, root):
    """把命中文件反推成相对 glob，避免扫到同一批配置。"""
    rels = [os.path.relpath(f, root).replace("\\", "/") for f in files]
    globs = []
    exts = {r.rsplit(".", 1)[-1] for r in rels}
    if "jsonl" in exts:
        globs.append("**/*.jsonl")
    for d in ("sessions", "chats", "threads", "history"):
        if any(("/%s/" % d) in ("/" + r) or r.startswith(d + "/") for r in rels):
            globs.append("**/%s/*.json" % d)
    if not globs:
        if "json" in exts:
            globs.append("**/*.json")
    return globs


def _db_has_sessions(path):
    """只读检查这个 SQLite 像不像会话库：有 sessions 表，或有「会话 id + 角色 + 正文」的消息表。
    库被别的程序独占（database is locked）时当作「不像」，绝不能让扫描整个失败。"""
    import sqlite3
    try:
        con = sqlite3.connect("file:///" + path.replace("\\", "/") + "?mode=ro",
                              uri=True, timeout=1)
    except sqlite3.Error:
        return None
    try:
        try:
            names = {r[0] for r in con.execute(
                "select name from sqlite_master where type='table'")}
        except sqlite3.Error:
            return None
        if "sessions" in names:
            try:
                n = con.execute("select count(*) from sessions").fetchone()[0]
            except sqlite3.Error:
                n = 0
            if n > 0:
                return "sessions"
        id_cols = {"session_id", "session_key", "thread_id", "conversation_id",
                   "chat_id"}
        role_cols = {"role", "type", "sender", "author"}
        text_cols = {"content", "text", "message", "body", "data"}
        for t in sorted(names):
            low = t.lower()
            if low.startswith(("sqlite_", "_yoyo", "search_index", "transcript_fts")):
                continue
            try:
                cols = {r[1] for r in con.execute('pragma table_info("%s")' % t)}
            except sqlite3.Error:
                continue
            if (cols & id_cols) and (cols & role_cols) and (cols & text_cols):
                try:
                    n = con.execute('select count(*) from "%s"' % t).fetchone()[0]
                except sqlite3.Error:
                    continue
                if n > 0:
                    return "messages"
        return None
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def _sqlite_sessions_db(root, limit=60, max_depth=3):
    """找带会话数据的 SQLite 库。只看浅几层，避免全盘扫。"""
    checked = 0
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [d for d in dirs
                   if d.lower() not in catalog.SKIP_DIRS and not d.startswith(".")]
        if depth >= max_depth:
            dirs[:] = []
        for fn in files:
            if not fn.endswith((".db", ".sqlite", ".sqlite3")):
                continue
            p = os.path.join(dirpath, fn)
            checked += 1
            if checked > limit:
                return None
            if _db_has_sessions(p):
                return p
    return None


def _heuristic_provider(root, key, name):
    # AtomCode 风格
    sessions = os.path.join(root, "sessions")
    if os.path.isdir(sessions):
        try:
            for d in os.listdir(sessions):
                sub = os.path.join(sessions, d)
                if os.path.isdir(sub) and any(
                        f.endswith(".meta") for f in os.listdir(sub)):
                    return AtomCodeProvider(root)
        except OSError:
            pass
    # Cline / Roo 风格
    tasks = os.path.join(root, "tasks")
    if os.path.isdir(tasks):
        try:
            if any(os.path.isdir(os.path.join(tasks, d)) for d in os.listdir(tasks)):
                return TaskDirProvider(key, name, tasks)
        except OSError:
            pass
    # SQLite
    db = _sqlite_sessions_db(root)
    if db:
        return SqliteSessionsProvider(key, name, os.path.dirname(db),
                                      os.path.basename(db))
    # JSON / JSONL
    files = _find_session_files(root)
    if files:
        globs = _globs_for(files, root)
        if globs:
            return GenericFileProvider(key, name, root, globs)
    return None


def _covered(d, seen_roots):
    """d 是否已被某个 provider 的根目录覆盖（父子关系都算）。"""
    ld = os.path.abspath(d).rstrip("\\/").lower()
    for r in seen_roots:
        lr = os.path.abspath(r).rstrip("\\/").lower()
        if ld == lr or ld.startswith(lr + os.sep) or lr.startswith(ld + os.sep):
            return True
    return False


def _inside(d, roots):
    """d 是否在某个 provider 根目录之下（只判断「在里面」，不判断「包含」）。"""
    ld = os.path.abspath(d).rstrip("\\/").lower()
    for r in roots:
        lr = os.path.abspath(r).rstrip("\\/").lower()
        if ld == lr or ld.startswith(lr + os.sep):
            return True
    return False


def _is_own_dir(d):
    """自己的缓存 / 回收站 / 客户端窗口的浏览器 profile，不是 AI 客户端。"""
    ld = os.path.abspath(d).rstrip("\\/").lower()
    own = [os.path.join(os.path.expanduser("~"), ".ai-session-manager")]
    try:
        from .. import usage as usage_mod
        own.append(usage_mod.cache_dir())
    except Exception:  # noqa: BLE001
        pass
    for p in own:
        lp = os.path.abspath(p).rstrip("\\/").lower()
        if ld == lp or ld.startswith(lp + os.sep):
            return True
    return False


def _heuristic(seen_roots):
    home = os.path.expanduser("~")
    roots = []
    for base in (home, os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
                 os.path.join(home, ".config"), os.path.join(home, ".local", "share"),
                 os.path.join(home, "Library", "Application Support")):
        if not base or not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                d = os.path.join(base, name)
                if not os.path.isdir(d) or _covered(d, seen_roots) or _is_own_dir(d):
                    continue
                low = name.lower().lstrip(".")
                if low in _PRIMARY_NAMES:
                    continue
                if name.startswith("@"):
                    # npm 风格的 @scope 目录，真正的应用数据在下一层
                    try:
                        for sub in sorted(os.listdir(d)):
                            sd = os.path.join(d, sub)
                            if os.path.isdir(sd) and not _covered(sd, seen_roots):
                                roots.append((sd, "%s %s" % (name[1:], sub)))
                    except OSError:
                        pass
                    continue
                if not any(k in low for k in catalog.AI_KEYWORDS):
                    continue
                roots.append((d, name))
        except OSError:
            continue
    out = []
    for d, name in roots[:60]:
        if _covered(d, seen_roots):
            continue
        disp = name.lstrip(".").replace("-", " ").replace("_", " ") or name
        key = "x-" + _slug(disp)
        seen_roots.add(d)
        p = _heuristic_provider(d, key, disp)
        if p is None:
            continue
        try:
            n, _ = p.quick_stats()
        except Exception:  # noqa: BLE001
            n = 0
        if n <= 0:
            continue
        p.via = "扫描: %s" % d
        out.append(p)
    return out


# 典型的会话库文件名。名字对得上 + 库里有会话数据，才认。
_DB_SIGNATURES = {
    "sessions.db", "session.db", "chat.db", "conversations.db", "threads.db",
    "history.db", "sessions.sqlite", "sessions.sqlite3", "session-store.db",
}

# 整机签名扫描时跳过的大目录（系统 / 缓存 / 依赖）
_SIGNATURE_SKIP = set(catalog.SKIP_DIRS) | {
    "microsoft", "packages", "programs", "windows", "mozilla", "google",
    "nvidia", "intel", "amd", "diagnostics", "crashdumps",
    "connecteddevicesplatform", "comms", "placeholdertilelogofolder",
    "temporary internet files", "application data", ".git", "venv", ".venv",
    "__pycache__", "site-packages", "dist-info", "build", "dist", "target",
    "obj", "pods", "vendor", "pnpm-store", ".npm", "wheels", "history",
}
_GENERIC_DIRS = {
    "state", "data", "db", "database", "storage", "user data", "userdata",
    "app", "sessions", "session", "sqlite", "profile", "profiles",
    "desktop-electron", "electron", "local storage", "users", "default",
}


def _app_name_from_db(db, base):
    """从库的路径反推应用名：取最外层的非通用目录。"""
    cur = os.path.dirname(db)
    base = os.path.abspath(base)
    names = []
    while cur and os.path.abspath(cur) != base:
        n = os.path.basename(cur)
        if n and n.lower() not in _GENERIC_DIRS and not n.startswith("."):
            names.append(n)
        parent = os.path.dirname(cur.rstrip("\\/"))
        if parent == cur:
            break
        cur = parent
    name = names[-1] if names else os.path.basename(os.path.dirname(db))
    return name.lstrip("@")


def _signature_providers(seen_roots, seen_keys, budget=3.0):
    """不靠目录名猜：直接找本机任意位置的「会话库」，把未知客户端也接进来。"""
    import time as _t
    t0 = _t.time()
    home = os.path.expanduser("~")
    out = []
    # 先扫 AppData/Roaming 和家目录：应用数据大多在这
    for base in (os.environ.get("APPDATA"), home,
                 os.path.join(home, ".config"), os.path.join(home, ".local", "share"),
                 os.environ.get("LOCALAPPDATA"),
                 os.path.join(home, "Library", "Application Support")):
        if not base or not os.path.isdir(base):
            continue
        try:
            walker = os.walk(base)
            for dirpath, dirs, files in walker:
                if _t.time() - t0 > budget:
                    return out
                rel = os.path.relpath(dirpath, base)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                dirs[:] = [d for d in dirs
                           if d.lower() not in _SIGNATURE_SKIP
                           and "cache" not in d.lower()
                           and not d.startswith(".")]
                if depth >= 5:
                    dirs[:] = []
                if _inside(dirpath, seen_roots) or _is_own_dir(dirpath):
                    dirs[:] = []
                    continue
                for fn in files:
                    if fn.lower() not in _DB_SIGNATURES:
                        continue
                    p = os.path.join(dirpath, fn)
                    if not _db_has_sessions(p):
                        continue
                    name = _app_name_from_db(p, base)
                    disp = name.replace("-", " ").replace("_", " ") or name
                    key = "x-" + _slug(disp)
                    if key in seen_keys:
                        key = "%s-%s" % (key, _slug(os.path.basename(base))[:6])
                    prov = SqliteSessionsProvider(key, disp, dirpath, fn)
                    try:
                        n, _sz = prov.quick_stats()
                    except Exception:  # noqa: BLE001
                        n = 0
                    if n <= 0:
                        continue
                    prov.via = "扫描到会话库: %s" % p
                    seen_roots.add(dirpath)
                    seen_keys.add(key)
                    out.append(prov)
        except OSError:
            continue
    return out


def scan(force=False):
    """返回本机所有 provider。带 30 秒缓存；force=True 立即重扫。"""
    import time as _t
    with _LOCK:
        if not force and _CACHE["items"] is not None and \
                _t.time() - _CACHE["at"] < _SCAN_TTL:
            return list(_CACHE["items"])
        items = [cls(_root_override(cls.key)) for cls in PRIMARY]
        seen = {p.root for p in items}
        seen_keys = {p.key for p in items}

        for spec in catalog.SPECS:
            if spec["key"] in seen_keys:
                continue
            p = _spec_provider(spec)
            if p is None or not p.available() or p.root in seen:
                continue
            try:
                n, _ = p.quick_stats()
            except Exception:  # noqa: BLE001
                n = 0
            p.empty = n <= 0
            p.via = "已知路径"
            items.append(p)
            seen.add(p.root)
            seen_keys.add(p.key)

        for p in _vscode_providers(seen, seen_keys):
            items.append(p)

        for p in _heuristic(seen):
            if p.key in seen_keys:
                p.key = p.key + "-" + _slug(os.path.basename(p.root))[:6]
            items.append(p)
            seen_keys.add(p.key)

        for p in _signature_providers(seen, seen_keys):
            items.append(p)

        _CACHE["items"] = items
        _CACHE["at"] = _t.time()
        return list(items)


def all_providers(force=False):
    return scan(force)


def get_provider(key):
    for p in scan():
        if p.key == key:
            return p
    raise KeyError("未知的工具: %s" % key)


def keys(force=False):
    return [p.key for p in scan(force)]


def invalidate():
    with _LOCK:
        _CACHE["items"] = None
        _CACHE["at"] = 0.0
