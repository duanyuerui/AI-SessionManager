#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code 会话历史记录管理：列出 / 查看 / 删除 / 批量清理 / 回收站恢复。

会话记录在 ~/.claude/projects/<项目>/<session-id>.jsonl，一个文件就是一次会话。
默认删除是移入回收站 ~/.claude/session-trash/，可随时 restore。
仅用标准库，Python 3.8+，Windows / macOS / Linux 通用。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import sys
import time
import unicodedata

TRASH_NAME = "session-trash"
META_SUFFIX = ".meta.json"
HOME = os.path.expanduser("~")

JUNK_PREFIX = (
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command", "<system-reminder", "<task-notification",
    "<bash-input>", "Caveat:",
)


class UserError(Exception):
    pass


# ---------- 输出与显示宽度（CJK 对齐） ----------

def setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def disp_w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def trunc(s: str, w: int) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    if disp_w(s) <= w:
        return s
    out, used = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if used + cw > w - 1:
            out.append("…")
            break
        out.append(c)
        used += cw
    return "".join(out)


def pad(s: str, w: int) -> str:
    t = trunc(s, w)
    return t + " " * max(0, w - disp_w(t))


def rpad(s: str, w: int) -> str:
    t = trunc(s, w)
    return " " * max(0, w - disp_w(t)) + t


def human_size(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def fmt_mtime(ts: float) -> str:
    return time.strftime("%y-%m-%d %H:%M", time.localtime(ts))


def short_ts(iso: str) -> str:
    return (iso or "").replace("T", " ")[:16]


def fmt_dur(ms) -> str:
    if not ms:
        return ""
    s = int(ms / 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


# ---------- 文件读取与解析 ----------

def parse_line(line: str):
    try:
        d = json.loads(line)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def read_chunk(path: str, nbytes: int, from_end: bool):
    """读文件头/尾若干字节，按行返回；截断的不完整行丢弃。"""
    size = os.path.getsize(path)
    seeked = from_end and size > nbytes
    with open(path, "rb") as f:
        if seeked:
            f.seek(-nbytes, os.SEEK_END)
        data = f.read(nbytes)
    lines = data.split(b"\n")
    if seeked:
        lines = lines[1:]
    elif not from_end:
        lines = lines[:-1]
    return [l.decode("utf-8", errors="replace") for l in lines]


def msg_text(d: dict) -> str:
    m = d.get("message")
    if not isinstance(m, dict):
        return ""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [it.get("text", "") for it in c
                 if isinstance(it, dict) and it.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def real_user_text(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    if not s:
        return ""
    for j in JUNK_PREFIX:
        if s.startswith(j):
            return ""
    return s


def count_lines(path: str) -> int:
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return n


def peek(fpath: str):
    """读文件头：cwd、标题（custom > ai > summary > 首条提问）、首个时间戳。"""
    cwd = title_custom = title_ai = title_sum = first_text = ""
    first_ts = ""
    for line in read_chunk(fpath, 65536, from_end=False):
        d = parse_line(line)
        if d is None:
            continue
        t = d.get("type")
        if not cwd and isinstance(d.get("cwd"), str) and d["cwd"]:
            cwd = d["cwd"]
        if t == "custom-title" and d.get("customTitle"):
            title_custom = str(d["customTitle"])
        elif t == "ai-title" and d.get("aiTitle"):
            title_ai = str(d["aiTitle"])
        elif t == "summary" and d.get("summary"):
            title_sum = str(d["summary"])
        if not first_ts and isinstance(d.get("timestamp"), str):
            first_ts = d["timestamp"]
        if not first_text and t == "user" and not d.get("isSidechain"):
            txt = real_user_text(msg_text(d))
            if txt:
                first_text = txt
    return cwd, (title_custom or title_ai or title_sum or first_text), first_ts


def peek_tail(fpath: str):
    last_ts = ""
    last_prompt = ""
    for line in read_chunk(fpath, 16384, from_end=True):
        d = parse_line(line)
        if d is None:
            continue
        if isinstance(d.get("timestamp"), str):
            last_ts = d["timestamp"]
        if d.get("type") == "last-prompt" and d.get("lastPrompt"):
            last_prompt = str(d["lastPrompt"])
    return last_ts, last_prompt


def make_session(fpath: str, pdir: str, st) -> dict:
    sid = os.path.splitext(os.path.basename(fpath))[0]
    cwd, title, first_ts = peek(fpath)
    last_ts, last_prompt = peek_tail(fpath)
    return {
        "path": fpath, "project_dir": pdir, "cwd": cwd,
        "session_id": sid, "title": title,
        "size": st.st_size, "mtime": st.st_mtime,
        "first_ts": first_ts, "last_ts": last_ts, "last_prompt": last_prompt,
        "lines": None,
    }


def match_project(s: dict, q: str) -> bool:
    q = q.replace("\\", "/").lower().rstrip("/")
    names = [os.path.basename(s["project_dir"]).lower(),
             (s["cwd"] or "").replace("\\", "/").lower().rstrip("/")]
    if any(ch in q for ch in "*?["):
        return any(fnmatch.fnmatch(n, q) for n in names)
    return any(q in n for n in names)


def current_sid() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def scan(args, project=None):
    proot = projects_root(args)
    rows = []
    if not os.path.isdir(proot):
        return rows
    for pdir in sorted(os.listdir(proot)):
        ppath = os.path.join(proot, pdir)
        if not os.path.isdir(ppath):
            continue
        for fn in os.listdir(ppath):
            if not fn.endswith(".jsonl"):
                continue
            fpath = os.path.join(ppath, fn)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            s = make_session(fpath, pdir, st)
            if project and not match_project(s, project):
                continue
            rows.append(s)
    rows.sort(key=lambda s: (-s["mtime"], s["session_id"]))
    return rows


def here_filter(rows):
    cwdn = os.getcwd().replace("\\", "/").rstrip("/").lower()
    return [s for s in rows
            if (s["cwd"] or "").replace("\\", "/").lower().startswith(cwdn)]


# ---------- 展示 ----------

def short_path(p: str, max_components: int = 2) -> str:
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


W_TIME, W_SIZE, W_LINES, W_PROJ, W_TITLE = 14, 7, 6, 34, 44


def print_row_brief(i, s):
    print(f"  {rpad(str(i), 4)}  {pad(fmt_mtime(s['mtime']), W_TIME)}  "
          f"{rpad(human_size(s['size']), W_SIZE)}  "
          f"{pad(short_path(s['cwd'] or s['project_dir']), W_PROJ)}  "
          f"{trunc(s['title'] or '(无标题)', W_TITLE)}")


def render_rows(rows, start=1):
    cur = current_sid()
    head = (f"{'#':>4}  {pad('最后活动', W_TIME)}  {rpad('大小', W_SIZE)}  "
            f"{rpad('行数', W_LINES)}  {pad('项目', W_PROJ)}  标题")
    print(head)
    print("-" * disp_w(head))
    for i, s in enumerate(rows, start):
        mark = "▶" if s["session_id"] == cur else " "
        print(f"{mark}{rpad(str(i), 3)}  {pad(fmt_mtime(s['mtime']), W_TIME)}  "
              f"{rpad(human_size(s['size']), W_SIZE)}  "
              f"{rpad(str(s['lines']), W_LINES)}  "
              f"{pad(short_path(s['cwd'] or s['project_dir']), W_PROJ)}  "
              f"{trunc(s['title'] or '(无标题)', W_TITLE)}")


def deep_info(s: dict) -> dict:
    users = asst = 0
    cost = None
    dur_ms = None
    first_text = last_text = ""
    start_ts = end_ts = ""
    with open(s["path"], encoding="utf-8", errors="replace") as f:
        for line in f:
            d = parse_line(line)
            if d is None or d.get("isSidechain"):
                continue
            t = d.get("type")
            ts = d.get("timestamp")
            if isinstance(ts, str):
                if not start_ts:
                    start_ts = ts
                end_ts = ts
            if t == "user":
                txt = real_user_text(msg_text(d))
                if txt:
                    users += 1
                    if not first_text:
                        first_text = txt
                    last_text = txt
            elif t == "assistant":
                asst += 1
            elif t == "cost-state":
                if d.get("totalCostUSD"):
                    cost = d["totalCostUSD"]
                if d.get("totalDuration"):
                    dur_ms = d["totalDuration"]
    return {"users": users, "asst": asst, "cost": cost, "dur_ms": dur_ms,
            "first_text": first_text, "last_text": last_text,
            "start_ts": start_ts, "end_ts": end_ts}


def show_one(s: dict):
    s["lines"] = count_lines(s["path"])
    info = deep_info(s)
    cur = "（当前会话）" if s["session_id"] == current_sid() else ""
    print(f"会话ID:   {s['session_id']} {cur}")
    print(f"标题:     {s['title'] or '(无标题)'}")
    print(f"项目:     {s['cwd'] or '(未知)'}")
    print(f"文件:     {s['path']}")
    print(f"大小:     {human_size(s['size'])}   行数: {s['lines']}")
    print(f"时间:     {short_ts(info['start_ts'])} → {short_ts(info['end_ts'])}")
    print(f"消息:     用户 {info['users']} 条 / 助手 {info['asst']} 条")
    if info["cost"] is not None:
        print(f"成本:     ${info['cost']:.4f}   时长: {fmt_dur(info['dur_ms'])}")
    if info["first_text"]:
        print(f"首条提问: {trunc(info['first_text'], 100)}")
    if info["last_text"]:
        print(f"末条提问: {trunc(info['last_text'], 100)}")
    if s["last_prompt"]:
        print(f"最后输入: {trunc(s['last_prompt'], 100)}")
    print(f"恢复:     claude --resume {s['session_id']}")


# ---------- id 解析与选择 ----------

def resolve(args, token: str, rows) -> dict:
    token = (token or "").strip()
    if not token:
        raise UserError("空的会话 id")
    if token.startswith("#"):
        try:
            idx = int(token[1:])
        except ValueError:
            raise UserError(f"编号格式不对: {token}")
        if not 1 <= idx <= len(rows):
            raise UserError(f"编号 {idx} 超出范围（当前共 {len(rows)} 个）")
        return rows[idx - 1]
    tok = token.lower()
    matches = []
    for s in rows:
        base = os.path.basename(s["path"]).lower()
        if (s["session_id"].lower().startswith(tok) or base.startswith(tok)
                or s["path"].lower() == tok
                or os.path.abspath(s["path"]).lower() == tok):
            matches.append(s)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UserError(f"未找到会话: {token}（先用 list 查看）")
    print(f"「{token}」匹配到 {len(matches)} 个会话，请用更长的前缀：")
    for i, s in enumerate(matches, 1):
        print_row_brief(i, s)
    raise UserError("")


def parse_selection(ans: str, rendered) -> list:
    ans = (ans or "").strip()
    if not ans:
        return []
    sel = set()
    for part in re.split(r"[,\s]+", ans):
        if not part:
            continue
        if part.lower() == "a":
            return list(rendered)
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            sel.update(range(a, b + 1))
        elif part.isdigit():
            sel.add(int(part))
        else:
            raise UserError(f"看不懂的选择: {part}")
    out, bad = [], []
    for i in sorted(sel):
        if 1 <= i <= len(rendered):
            out.append(rendered[i - 1])
        else:
            bad.append(i)
    if bad:
        print(f"忽略超范围编号: {', '.join(map(str, bad))}")
    return out


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""
    except KeyboardInterrupt:
        return ""


def confirm_select(cands, verb: str, args) -> list:
    if not cands:
        return []
    if args.y:
        print(f"将{verb}以下 {len(cands)} 个会话：")
        for i, s in enumerate(cands, 1):
            print_row_brief(i, s)
        return cands
    total = human_size(sum(s["size"] for s in cands))
    if len(cands) == 1:
        s = cands[0]
        print(f"\n准备{verb}:")
        print_row_brief(1, s)
        ans = ask(f"确认（{total}）? (y/N) ").lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return []
        return cands
    print(f"\n准备{verb}以下 {len(cands)} 个会话（共 {total}）：")
    for i, s in enumerate(cands, 1):
        print_row_brief(i, s)
    ans = ask("输入编号选择（如 1,3-5；a=全部；回车=取消）: ")
    try:
        return parse_selection(ans, cands)
    except UserError as e:
        print(f"已取消: {e}")
        return []


# ---------- 删除 / 回收站 ----------

def do_trash(targets, args, purge=False):
    troot = trash_root(args)
    if not purge:
        os.makedirs(troot, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    done, freed = 0, 0
    for s in targets:
        try:
            if purge:
                os.remove(s["path"])
            else:
                dest = os.path.join(troot, f"{stamp}_{s['session_id']}.jsonl")
                shutil.move(s["path"], dest)
                meta = {
                    "session_id": s["session_id"],
                    "original_path": s["path"],
                    "cwd": s["cwd"], "title": s["title"],
                    "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                with open(dest + META_SUFFIX, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            done += 1
            freed += s["size"]
            print(f"{'已永久删除' if purge else '已移入回收站'}: {s['path']}")
        except OSError as e:
            print(f"失败: {s['path']} — {e}"
                  f"（文件可能正被其他 Claude 窗口占用，关闭后重试）")
    print(f"\n共处理 {done} 个，释放 {human_size(freed)}")
    if not purge and done:
        print(f"恢复: python {__file__} trash 查看 → restore <编号>")


def filter_live(sel, args) -> list:
    cur = current_sid()
    if not cur:
        return sel
    live = [s for s in sel if s["session_id"] == cur]
    if live and not getattr(args, "force", False):
        for s in live:
            print(f"跳过当前正在运行的会话: {s['path']}（确要删除请加 --force）")
        return [s for s in sel if s["session_id"] != cur]
    return sel


# ---------- 子命令 ----------

def cmd_list(args):
    if getattr(args, "tool", "claude") != "claude" or args.sort == "tokens":
        return _prov_list(args)
    rows = scan(args, project=args.project)
    if args.here:
        rows = here_filter(rows)
    if args.sort == "lines":
        for s in rows:
            s["lines"] = count_lines(s["path"])
        rows.sort(key=lambda s: -s["lines"])
    else:
        rows.sort(key=lambda s: -s["mtime"] if args.sort == "time" else -s["size"])
    total = len(rows)
    if args.limit and args.limit > 0:
        rows = rows[:args.limit]
    if not rows:
        print("没有会话记录。")
        return
    for s in rows:
        if s["lines"] is None:
            s["lines"] = count_lines(s["path"])
    render_rows(rows)
    if args.sort != "time":
        print("（注意：#编号按默认时间倒序解释，与当前排序不一致；删除请用会话 ID 前缀更稳）")
    if total > len(rows):
        print(f"\n（共 {total} 个，仅显示 {len(rows)} 个；-n 0 查看全部）")


def cmd_show(args):
    if getattr(args, "tool", "claude") != "claude":
        return _prov_show(args)
    rows = scan(args)
    show_one(resolve(args, args.id, rows))


def cmd_del(args):
    if getattr(args, "tool", "claude") != "claude":
        return _prov_del(args)
    rows = scan(args, project=getattr(args, "project", None))
    targets, seen = [], set()
    for t in args.ids:
        s = resolve(args, t, rows)
        if s["path"] in seen:
            continue
        seen.add(s["path"])
        targets.append(s)
    targets = filter_live(targets, args)
    if not targets:
        return
    verb = "永久删除" if args.purge else "删除（移入回收站）"
    sel = confirm_select(targets, verb, args)
    if sel:
        do_trash(sel, args, purge=args.purge)


def parse_duration(s: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([smhdw])", (s or "").strip().lower())
    if not m:
        raise UserError("时长格式: 如 30d / 12h / 90m / 2w")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return int(m.group(1)) * mult


def parse_size(s: str) -> int:
    m = re.fullmatch(r"([\d.]+)\s*([bkmgt]?)", (s or "").strip().lower())
    if not m:
        raise UserError("大小格式: 如 5m / 500k / 1g / 100b")
    mult = {"b": 1, "": 1, "k": 1024, "m": 1024 ** 2,
            "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2)]
    return int(float(m.group(1)) * mult)


def cmd_clean(args):
    if getattr(args, "tool", "claude") != "claude":
        return _prov_clean(args)
    rows = scan(args, project=args.project)
    if args.here:
        rows = here_filter(rows)
    if not (args.older_than or args.bigger_than or args.keep_last):
        print("警告: 未指定条件（--older-than / --bigger-than / --keep-last），将匹配全部会话")
    now = time.time()
    cutoff = parse_duration(args.older_than) if args.older_than else None
    minsize = parse_size(args.bigger_than) if args.bigger_than else None
    keepn = args.keep_last or 0
    protected = {current_sid()}
    if keepn:
        byproj = {}
        for s in rows:
            byproj.setdefault(s["project_dir"], []).append(s)
        for lst in byproj.values():
            lst.sort(key=lambda s: -s["mtime"])
            for s in lst[:keepn]:
                protected.add(s["session_id"])
    cands = []
    for s in rows:
        if s["session_id"] in protected:
            continue
        if cutoff is not None and now - s["mtime"] < cutoff:
            continue
        if minsize is not None and s["size"] < minsize:
            continue
        cands.append(s)
    if not cands:
        print("没有满足条件的会话。")
        return
    if args.dry_run:
        total = human_size(sum(s["size"] for s in cands))
        print(f"[预览] 将清理以下 {len(cands)} 个会话（共 {total}）：")
        for i, s in enumerate(cands, 1):
            print_row_brief(i, s)
        return
    verb = "永久删除" if args.purge else "清理（移入回收站）"
    sel = confirm_select(cands, verb, args)
    if sel:
        do_trash(sel, args, purge=args.purge)


def trash_entries(args) -> list:
    troot = trash_root(args)
    out = []
    if not os.path.isdir(troot):
        return out
    for fn in sorted(os.listdir(troot), reverse=True):
        if not fn.endswith(".jsonl"):
            continue
        fpath = os.path.join(troot, fn)
        try:
            st = os.stat(fpath)
        except OSError:
            continue
        meta = {}
        if os.path.exists(fpath + META_SUFFIX):
            try:
                with open(fpath + META_SUFFIX, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        out.append({"path": fpath, "meta": meta,
                    "size": st.st_size, "mtime": st.st_mtime})
    return out


def cmd_trash(args):
    entries = trash_entries(args)
    if not entries:
        print("回收站是空的。")
        return
    total = sum(e["size"] for e in entries)
    print(f"回收站（{len(entries)} 个，共 {human_size(total)}）：")
    for i, e in enumerate(entries, 1):
        m = e["meta"]
        delat = m.get("deleted_at") or fmt_mtime(e["mtime"])
        title = m.get("title") or ""
        orig = m.get("original_path") or "?"
        print(f"  {rpad(str(i), 4)}  {pad(delat, 19)}  "
              f"{rpad(human_size(e['size']), 7)}  "
              f"{trunc(title or '(无标题)', 30)}  ← {trunc(short_path(os.path.dirname(orig), 2), 40)}")
    print("\n恢复: restore <编号>   清空: empty-trash")


def cmd_restore(args):
    entries = trash_entries(args)
    if not entries:
        print("回收站是空的。")
        return
    for tok in args.ids:
        digits = re.sub(r"[^\d]", "", tok)
        n = int(digits) if digits else 0
        if not 1 <= n <= len(entries):
            print(f"忽略无效编号: {tok}")
            continue
        e = entries[n - 1]
        dest = e["meta"].get("original_path")
        if not dest:
            print(f"#{n} 缺少原始路径信息，无法恢复")
            continue
        if os.path.exists(dest):
            stem, ext = os.path.splitext(dest)
            dest = f"{stem}_restored{ext}"
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(e["path"], dest)
            if os.path.exists(e["path"] + META_SUFFIX):
                os.remove(e["path"] + META_SUFFIX)
            print(f"已恢复: {dest}")
        except OSError as ex:
            print(f"恢复失败: {ex}")


def cmd_empty(args):
    entries = trash_entries(args)
    if not entries:
        print("回收站是空的。")
        return
    total = human_size(sum(e["size"] for e in entries))
    if not args.y:
        print(f"将永久清空回收站 {len(entries)} 个文件（{total}），不可恢复！")
        ans = ask("输入 y 确认: ").lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return
    n = 0
    for e in entries:
        for p in (e["path"], e["path"] + META_SUFFIX):
            if os.path.exists(p):
                try:
                    os.remove(p)
                    n += 1
                except OSError as ex:
                    print(f"删除失败 {p}: {ex}")
    print(f"已清空回收站（删除 {n} 个文件）")


def cmd_stats(args):
    if getattr(args, "tool", "claude") != "claude":
        return _prov_stats(args)
    rows = scan(args, project=args.project)
    if not rows:
        print("没有会话记录。")
        return
    total = sum(s["size"] for s in rows)
    print(f"共 {len(rows)} 个会话，{human_size(total)}\n")
    byproj = {}
    for s in rows:
        byproj.setdefault(s["cwd"] or s["project_dir"], []).append(s)
    for p, lst in sorted(byproj.items(),
                         key=lambda kv: -sum(x["size"] for x in kv[1])):
        sz = human_size(sum(x["size"] for x in lst))
        latest = max(x["mtime"] for x in lst)
        cur = " ▶当前" if any(x["session_id"] == current_sid() for x in lst) else ""
        print(f"  {rpad(sz, 8)} {rpad(str(len(lst)), 4)} 个  "
              f"最近 {fmt_mtime(latest)}  {trunc(short_path(p, 3), 46)}{cur}")
    troot = trash_root(args)
    if os.path.isdir(troot):
        files = [f for f in os.listdir(troot) if f.endswith(".jsonl")]
        if files:
            tsz = sum(os.path.getsize(os.path.join(troot, f)) for f in files)
            print(f"\n  回收站: {len(files)} 个，{human_size(tsz)}（empty-trash 清空）")


# ---------- 交互模式 ----------

def cmd_interactive(args):
    print("会话历史记录管理（交互模式，默认删除进回收站）\n")
    while True:
        rows = scan(args)
        shown = rows[:20]
        for s in shown:
            s["lines"] = count_lines(s["path"])
        if shown:
            render_rows(shown)
            print(f"\n（共 {len(rows)} 个，显示最近 {len(shown)} 个）")
        else:
            print("没有会话记录。")
        print("回车=刷新   d 编号=删除   s 编号=详情   t=回收站   q=退出")
        try:
            line = input("session-manager> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        if low in ("q", "quit", "exit"):
            break
        if low == "t":
            cmd_trash(args)
            continue
        if low.startswith(("s ", "d ")):
            action, rest = low[0], line[2:].strip()
            rows_all = scan(args)
            try:
                if re.fullmatch(r"[0-9][0-9,\-\s]*|a", rest):
                    pool = shown if action == "d" else rows_all
                    sel = parse_selection(rest, pool)
                else:
                    sel = [resolve(args, rest, rows_all)]
            except UserError as e:
                print(f"错误: {e}")
                continue
            if not sel:
                continue
            if action == "s":
                for s in sel:
                    print()
                    show_one(s)
                    print()
            else:
                sel = filter_live(sel, args)
                if not sel:
                    continue
                for s in sel:
                    print_row_brief("", s)
                if ask(f"确认移入回收站? (y/N) ").lower() in ("y", "yes"):
                    do_trash(sel, args)
            continue
        print(f"未知命令: {line}")


# ---------- 入口 ----------

# ---------- 多工具：list/show/del/clean/stats ----------

def _prov(args):
    providers = _load_providers()
    key = getattr(args, "tool", None) or "claude"
    try:
        p = providers.get_provider(key)
    except KeyError:
        raise UserError("未知的工具: %s（先用 tools 查看）" % key)
    if not p.available():
        raise UserError("%s 未安装（找不到 %s）" % (p.name, p.root))
    return p


def _prov_rows(args):
    p = _prov(args)
    rows = p.sessions()
    project = getattr(args, "project", None)
    if project:
        q = project.lower()
        rows = [s for s in rows
                if q in (s.get("project") or "").lower()
                or q in (s.get("cwd") or "").lower()]
    if getattr(args, "here", False):
        cwd = os.getcwd().replace("\\", "/").lower().rstrip("/")
        rows = [s for s in rows
                if (s.get("cwd") or "").replace("\\", "/").lower().startswith(cwd)]
    return p, rows


def _tok_total(b):
    if not b:
        return 0
    return (b.get("input", 0) + b.get("cache_read", 0)
            + b.get("cache_write", 0) + b.get("output", 0))


def _print_prov_row(i, s):
    tkt = _tok_total(s.get("tokens"))
    tk = _tok(tkt) if tkt else "-"
    print("  %s  %s  %s  %s  %s  %s" % (
        rpad(str(i), 4), pad(fmt_mtime(s["mtime"]), 14),
        rpad(human_size(s.get("size") or 0), 7), rpad(tk, 7),
        pad(short_path(s.get("project") or "", 2), W_PROJ),
        trunc(s.get("title") or "(无标题)", W_TITLE)))


def _prov_table(rows, start=1):
    head = (f"{'#':>4}  {pad('最后活动', 14)}  {rpad('大小', 7)}  "
            f"{rpad('Token', 7)}  {pad('项目', W_PROJ)}  标题")
    print(head)
    print("-" * disp_w(head))
    for i, s in enumerate(rows, start):
        _print_prov_row(i, s)


def _resolve_prov(rows, token):
    token = (token or "").strip()
    if token.startswith("#"):
        try:
            n = int(token[1:])
        except ValueError:
            raise UserError("编号格式不对: %s" % token)
        if not 1 <= n <= len(rows):
            raise UserError("编号超出范围")
        return rows[n - 1]
    tok = token.lower()
    hit = [s for s in rows if (s.get("id") or "").lower().startswith(tok)]
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise UserError("未找到会话: %s" % token)
    print("「%s」匹配到 %d 个，请写长一点：" % (token, len(hit)))
    for i, s in enumerate(hit, 1):
        _print_prov_row(i, s)
    raise UserError("")


def _prov_list(args):
    p, rows = _prov_rows(args)
    sort = getattr(args, "sort", "time")
    if sort == "size":
        rows.sort(key=lambda s: -(s.get("size") or 0))
    elif sort == "tokens":
        rows.sort(key=lambda s: -_tok_total(s.get("tokens")))
    else:
        rows.sort(key=lambda s: -(s.get("mtime") or 0))
    limit = getattr(args, "limit", 30) or 0
    total = len(rows)
    if limit > 0:
        rows = rows[:limit]
    if not rows:
        print("%s 没有会话记录。" % p.name)
        return
    _prov_table(rows)
    if total > len(rows):
        print("\n（共 %d 个，仅显示 %d 个；-n 0 查看全部）" % (total, len(rows)))


def _prov_show(args):
    p, rows = _prov_rows(args)
    s = _resolve_prov(rows, args.id)
    info = p.detail(s) or {}
    print("会话ID:   %s" % s["id"])
    print("工具:     %s（%s）" % (p.name, p.root))
    print("标题:     %s" % (s.get("title") or "(无标题)"))
    print("项目:     %s" % (s.get("cwd") or s.get("project") or "(未知)"))
    print("文件:     %s" % s.get("path"))
    print("大小:     %s" % human_size(s.get("size") or 0))
    if s.get("tokens"):
        b = s["tokens"]
        print("Token:    %s（输入 %s · 输出 %s · 缓存读 %s）" % (
            _tok(_tok_total(b)), _tok(b.get("input", 0)),
            _tok(b.get("output", 0)), _tok(b.get("cache_read", 0))))
    print("时间:     %s → %s" % (info.get("start_ts") or "-",
                                  info.get("end_ts") or "-"))
    if info.get("first_text"):
        print("首条提问: %s" % trunc(info["first_text"], 100))
    cmd = p.resume_cmd(s)
    if cmd:
        print("恢复:     %s" % cmd)


def _prov_del(args):
    p, rows = _prov_rows(args)
    targets, seen = [], set()
    for t in args.ids:
        s = _resolve_prov(rows, t)
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        targets.append(s)
    if not targets:
        return
    for i, s in enumerate(targets, 1):
        _print_prov_row(i, s)
    if not args.y:
        ans = ask("确认删除以上 %d 个? (y/N) " % len(targets)).lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return
    res = p.delete([s["id"] for s in targets], purge=args.purge,
                   force=getattr(args, "force", False))
    for d in res.get("deleted", []):
        print("%s: %s" % ("已永久删除" if args.purge else "已移入回收站",
                          d.get("title") or d["id"][:12]))
    for e in res.get("errors", []):
        print("失败: %s" % e)
    for s in res.get("skipped_live", []):
        print("跳过当前正在运行的会话: %s" % s)
    print("\n共处理 %d 个，释放 %s" % (len(res.get("deleted", [])),
                                     res.get("freed_str") or "0B"))


def _prov_clean(args):
    p, rows = _prov_rows(args)
    now = time.time()
    cutoff = parse_duration(args.older_than) if args.older_than else None
    minsize = parse_size(args.bigger_than) if args.bigger_than else None
    keepn = args.keep_last or 0
    protected = {p.live_id()}
    if keepn:
        byproj = {}
        for s in rows:
            byproj.setdefault(s.get("project") or "", []).append(s)
        for lst in byproj.values():
            lst.sort(key=lambda s: -(s.get("mtime") or 0))
            for s in lst[:keepn]:
                protected.add(s["id"])
    cands = []
    for s in rows:
        if s["id"] in protected:
            continue
        if cutoff is not None and now - (s.get("mtime") or 0) < cutoff:
            continue
        if minsize is not None and (s.get("size") or 0) < minsize:
            continue
        cands.append(s)
    if not cands:
        print("没有满足条件的会话。")
        return
    total = human_size(sum(s.get("size") or 0 for s in cands))
    print("%s将清理 %d 个会话（共 %s）：" % (
        "[预览] " if args.dry_run else "", len(cands), total))
    for i, s in enumerate(cands, 1):
        _print_prov_row(i, s)
    if args.dry_run:
        return
    if not args.y and ask("确认? (y/N) ").lower() not in ("y", "yes"):
        print("已取消")
        return
    res = p.delete([s["id"] for s in cands], purge=args.purge)
    print("\n共处理 %d 个，释放 %s" % (len(res.get("deleted", [])),
                                     res.get("freed_str") or "0B"))
    for e in res.get("errors", []):
        print("失败: %s" % e)


def _prov_stats(args):
    p, rows = _prov_rows(args)
    total = sum(s.get("size") or 0 for s in rows)
    print("%s：共 %d 个会话，%s\n" % (p.name, len(rows), human_size(total)))
    byproj = {}
    for s in rows:
        byproj.setdefault(s.get("project") or "(未知项目)", []).append(s)
    for name, lst in sorted(byproj.items(),
                            key=lambda kv: -sum(x.get("size") or 0 for x in kv[1])):
        sz = human_size(sum(x.get("size") or 0 for x in lst))
        tok = sum(_tok_total(x.get("tokens")) for x in lst)
        latest = max((x.get("mtime") or 0) for x in lst)
        print("  %s %s 个  %s tokens  最近 %s  %s" % (
            rpad(sz, 8), rpad(str(len(lst)), 4), rpad(_tok(tok), 8),
            fmt_mtime(latest) if latest else "-", trunc(short_path(name, 3), 46)))


def cmd_web(args):
    # 作为包运行时走相对导入；直接跑脚本时退回同级模块
    try:
        from . import server
    except ImportError:
        import server
    sys.exit(server.serve(args.root, args.port, not args.no_browser))


# ---------- 多工具：tools / usage ----------

def _tok(n):
    n = float(n or 0)
    for unit in ("", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%.0f%s" % (n, unit)) if unit == "" else ("%.1f%s" % (n, unit))
        n /= 1024
    return "%.1fT" % n


def _load_providers():
    try:
        from . import providers
    except ImportError:
        import providers
    return providers


def cmd_tools(args):
    from . import usage as usage_mod
    providers = _load_providers()
    if getattr(args, "scan", False):
        providers.all_providers(force=True)
    cache = usage_mod.load_cache()
    rows = []
    for key in providers.keys():
        p = providers.get_provider(key)
        installed = p.available()
        n = size = 0
        if installed:
            try:
                n, size = p.quick_stats()
            except Exception:  # noqa: BLE001
                pass
        qu = p.quick_usage(cache) if installed else {"total": 0}
        rows.append((p, installed, n, size, qu.get("total") or 0))
    head = "  %s  %s  %s  %s  %s" % (
        pad("工具", 16), rpad("会话", 6), rpad("占用", 9),
        rpad("Token", 9), "数据目录")
    print(head)
    print("-" * disp_w(head))
    for p, installed, n, size, tok in rows:
        status = p.root if installed else "未安装"
        name = p.name + ("（只读）" if installed and getattr(p, "readonly", False)
                         else "")
        print("  %s  %s  %s  %s  %s" % (
            pad(name, 16), rpad(str(n) if installed else "-", 6),
            rpad(human_size(size) if installed else "-", 9),
            rpad(_tok(tok) if tok else "-", 9), trunc(status, 60)))
    print("\n提示: 工作台看每日用量 → session-manager usage")


def _period_key(day, period):
    import datetime
    try:
        d = datetime.date(*[int(x) for x in day.split("-")])
    except ValueError:
        return day
    if period == "week":
        monday = d - datetime.timedelta(days=d.weekday())
        return monday.isoformat()
    if period == "month":
        return d.strftime("%Y-%m")
    if period == "year":
        return d.strftime("%Y")
    return day


def cmd_usage(args):
    from . import usage as usage_mod
    providers = _load_providers()
    provs = [providers.get_provider(k) for k in providers.keys()]
    provs = [p for p in provs if p.available()]
    data = usage_mod.collect(provs, force=args.refresh)
    tools = data["tools"]
    print("Token 用量（%s 生成）\n" % time.strftime("%Y-%m-%d %H:%M",
                                                   time.localtime(data["generated"])))
    head = "  %s  %s  %s  %s  %s  %s" % (
        pad("工具", 16), rpad("输入", 9), rpad("输出", 9),
        rpad("缓存读", 9), rpad("合计", 9), "会话")
    print(head)
    print("-" * disp_w(head))
    for t in tools:
        if not t["installed"]:
            print("  %s  %s" % (pad(t["name"], 12), "未安装"))
            continue
        b = t["totals"]
        print("  %s  %s  %s  %s  %s  %s%s" % (
            pad(t["name"], 12), rpad(_tok(b["input"]), 9),
            rpad(_tok(b["output"]), 9), rpad(_tok(b["cache_read"]), 9),
            rpad(_tok(t["total"]), 9), rpad(str(t["sessions"]), 4),
            "  估算" if t["approximate"] else ""))
    b = data["totals"]
    print("\n总计: 输入 %s · 输出 %s · 缓存读 %s · 合计 %s" % (
        _tok(b["input"]), _tok(b["output"]), _tok(b["cache_read"]),
        _tok(sum(x["total"] for x in tools))))

    period = args.period or "day"
    limit = args.limit or (14 if period == "day" else 12)
    agg = {}
    for day, per_tool in data["days"].items():
        key = _period_key(day, period)
        slot = agg.setdefault(key, {})
        for tk, tb in per_tool.items():
            cur = slot.setdefault(tk, {"input": 0, "output": 0, "cache_read": 0,
                                       "cache_write": 0, "reasoning": 0, "requests": 0})
            for f in cur:
                cur[f] += tb.get(f, 0)
    keys = sorted(agg)[-limit:]
    if keys:
        print("\n按%s（最近 %d 个）：" % (
            {"day": "天", "week": "周", "month": "月", "year": "年"}[period], len(keys)))
        mx = max(sum(v.get("input", 0) + v.get("output", 0)
                    + v.get("cache_read", 0) + v.get("cache_write", 0)
                    for v in agg[k].values()) for k in keys) or 1
        for k in keys:
            tot = sum(v.get("input", 0) + v.get("output", 0)
                      + v.get("cache_read", 0) + v.get("cache_write", 0)
                      for v in agg[k].values())
            bar = "█" * max(1, int(tot / mx * 24)) if tot else ""
            print("  %s  %s  %s" % (pad(k, 10), rpad(_tok(tot), 9), bar))
    approx = [t["name"] for t in tools if t.get("approximate")]
    if approx:
        print("\n注意: %s 的 token 是按会话最后活动时间估算的。" % "、".join(approx))


def projects_root(args) -> str:
    return os.path.join(base_dir(args), "projects")


def trash_root(args) -> str:
    return os.path.join(base_dir(args), TRASH_NAME)


def base_dir(args) -> str:
    root = getattr(args, "root", None)
    if root:
        return os.path.abspath(os.path.expanduser(root))
    return os.path.join(HOME, ".claude")


def build_parser():
    ap = argparse.ArgumentParser(
        prog="session-manager",
        description="Claude Code 会话历史记录管理：list/show/del/clean/trash/restore/stats",
    )
    ap.add_argument("--root", default=None,
                    help="覆盖默认根目录 ~/.claude（用于备份盘或测试）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", aliases=["ls"], help="列出会话")
    p.add_argument("--tool", default="claude", help="工具 key（见 tools 命令）")
    p.add_argument("-p", "--project", help="按项目路径/目录名过滤（支持 * 通配）")
    p.add_argument("-c", "--here", action="store_true", help="只看当前目录对应项目")
    p.add_argument("-s", "--sort", choices=["time", "size", "lines", "tokens"],
                   default="time", help="排序（默认按最后活动时间）")
    p.add_argument("-n", "--limit", type=int, default=30,
                   help="最多显示多少条（0=全部，默认 30）")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="查看单个会话详情")
    p.add_argument("id", help="会话 ID 前缀或 #编号")
    p.add_argument("--tool", default="claude", help="工具 key（见 tools 命令）")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("del", aliases=["delete", "rm"],
                       help="删除会话（默认移入回收站）")
    p.add_argument("ids", nargs="+", help="会话 ID 前缀或 #编号，可多个")
    p.add_argument("--tool", default="claude", help="工具 key（见 tools 命令）")
    p.add_argument("-y", action="store_true", help="跳过确认")
    p.add_argument("--purge", action="store_true", help="永久删除（不进回收站）")
    p.add_argument("--force", action="store_true",
                   help="允许删除当前正在运行的会话")
    p.set_defaults(func=cmd_del)

    p = sub.add_parser("clean", help="按条件批量清理")
    p.add_argument("--older-than", help="只清理多久之前的，如 30d / 12h / 2w")
    p.add_argument("--bigger-than", help="只清理大于该大小的，如 5m / 1g")
    p.add_argument("--keep-last", type=int, default=0,
                   help="每个项目保留最近 N 个")
    p.add_argument("-p", "--project", help="按项目路径/目录名过滤")
    p.add_argument("--tool", default="claude", help="工具 key（见 tools 命令）")
    p.add_argument("-c", "--here", action="store_true", help="只清当前目录对应项目")
    p.add_argument("-y", action="store_true", help="跳过确认")
    p.add_argument("--purge", action="store_true", help="永久删除（不进回收站）")
    p.add_argument("--dry-run", action="store_true", help="只预览不删除")
    p.set_defaults(func=cmd_clean)

    sub.add_parser("trash", help="查看回收站").set_defaults(func=cmd_trash)

    p = sub.add_parser("restore", help="从回收站恢复（按 trash 里的编号）")
    p.add_argument("ids", nargs="+", help="回收站编号，可多个")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("empty-trash", aliases=["empty"],
                       help="清空回收站（永久删除）")
    p.add_argument("-y", action="store_true", help="跳过确认")
    p.set_defaults(func=cmd_empty)

    p = sub.add_parser("stats", aliases=["du"], help="各项目占用统计")
    p.add_argument("-p", "--project", help="按项目路径/目录名过滤")
    p.add_argument("--tool", default="claude", help="工具 key（见 tools 命令）")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("web", help="启动本地网页界面（勾选删除、回收站恢复）")
    p.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("tools", help="扫描并查看本机的 AI 客户端")
    p.add_argument("--scan", action="store_true", help="强制重新扫描（不用 30 秒缓存）")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("usage", aliases=["token"],
                       help="Token 用量统计（按天/周/月/年）")
    p.add_argument("--period", choices=["day", "week", "month", "year"],
                   default="day", help="时间粒度（默认按天）")
    p.add_argument("-n", "--limit", type=int, default=0, help="显示几个时间段")
    p.add_argument("--refresh", action="store_true", help="忽略缓存重新扫描")
    p.set_defaults(func=cmd_usage)

    return ap


def main(argv=None):
    setup_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.cmd is None:
            cmd_interactive(args)
        else:
            args.func(args)
    except UserError as e:
        if str(e):
            print(f"错误: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n已取消")


if __name__ == "__main__":
    main()
