#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已知 AI 客户端的目录清单。

这里只列「有会话历史」的存储位置；路径不存在就跳过，用户装了才会出现。
新增客户端时优先用通用适配器（files / taskdir / sqlite），
格式实在特殊再单独写 Provider。
"""
from __future__ import annotations

import os

HOME = os.path.expanduser("~")


def p(path):
    return os.path.expanduser(os.path.expandvars(path))


# adapter:
#   files    一个文件一次会话（.jsonl 或 .json）
#   taskdir  一个目录一次会话（tasks/<id>/）
#   sqlite   数据库里的 sessions / turns
#   atomcode ~/.atomcode/sessions/<项目>/<id>.meta
SPECS = [
    # ---- 命令行 AI ----
    {"key": "gemini", "name": "Gemini CLI", "adapter": "files",
     "root": "~/.gemini", "globs": ["tmp/*/chats/*.json", "**/chats/*.json"]},
    {"key": "qwen", "name": "Qwen Code", "adapter": "files",
     "root": "~/.qwen", "globs": ["projects/*/*.jsonl", "tmp/*/chats/*.json"]},
    {"key": "codebuddy", "name": "CodeBuddy", "adapter": "files",
     "root": "~/.codebuddy", "globs": ["projects/*/*.jsonl"]},
    {"key": "iflow", "name": "iFlow CLI", "adapter": "files",
     "root": "~/.iflow", "globs": ["projects/*/*.jsonl"]},
    {"key": "qoder", "name": "Qoder", "adapter": "files",
     "root": "~/.qoder", "globs": ["projects/*/*.jsonl"]},
    {"key": "copilot-cli", "name": "GitHub Copilot CLI", "adapter": "files",
     "root": "~/.copilot", "globs": ["session-state/*/events.jsonl",
                                     "sessions/*/*.jsonl"]},
    {"key": "continue", "name": "Continue", "adapter": "files",
     "root": "~/.continue", "globs": ["sessions/*.json", "sessions/*/*.json"]},
    {"key": "goose", "name": "Goose", "adapter": "files",
     "root": "~/.local/share/goose", "globs": ["sessions/*.jsonl"]},
    {"key": "goose-win", "name": "Goose", "adapter": "files",
     "root": "%APPDATA%/Block/goose", "globs": ["sessions/*.jsonl"]},
    {"key": "opencode", "name": "OpenCode", "adapter": "files",
     "root": "~/.local/share/opencode",
     "globs": ["storage/session/*/*.json"]},
    {"key": "opencode-win", "name": "OpenCode", "adapter": "files",
     "root": "%APPDATA%/opencode", "globs": ["**/storage/session/*/*.json"]},
    {"key": "crush", "name": "Crush", "adapter": "files",
     "root": "~/.local/share/crush", "globs": ["sessions/*.json"]},
    {"key": "amp", "name": "Amp", "adapter": "files",
     "root": "~/.amp", "globs": ["threads/*.jsonl", "threads/*/*.jsonl"]},
    {"key": "aider", "name": "Aider", "adapter": "files",
     "root": "~/.aider", "globs": ["**/*.jsonl"]},
    {"key": "atomcode", "name": "AtomCode", "adapter": "atomcode",
     "root": "~/.atomcode"},
    {"key": "opensquilla", "name": "OpenSquilla", "adapter": "opensquilla",
     "root": "%APPDATA%/@opensquilla/desktop-electron"},

    # ---- VS Code 系扩展（默认 Code 安装位置；discovery 还会扫其它编辑器） ----
    {"key": "cline", "name": "Cline", "adapter": "taskdir",
     "root": "%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/tasks"},
    {"key": "roo", "name": "Roo Code", "adapter": "taskdir",
     "root": "%APPDATA%/Code/User/globalStorage/rooveterinaryinc.roo-cline/tasks"},
    {"key": "kilo", "name": "Kilo Code", "adapter": "taskdir",
     "root": "%APPDATA%/Code/User/globalStorage/kilocode.kilo-code/tasks"},
    {"key": "copilot-chat", "name": "GitHub Copilot Chat", "adapter": "sqlite",
     "root": "%APPDATA%/Code/User/globalStorage/github.copilot-chat",
     "db": "session-store.db"},
    {"key": "codegeex-ext", "name": "CodeGeeX", "adapter": "files",
     "root": "%APPDATA%/Code/User/globalStorage/aminer.codegeex",
     "globs": ["**/chats/*.json", "**/sessions/*.jsonl"]},
    {"key": "continue-ext", "name": "Continue", "adapter": "files",
     "root": "%APPDATA%/Code/User/globalStorage/continue.continue",
     "globs": ["sessions/*.json", "**/sessions/*.json"]},

    # ---- 其它编辑器（有数据才出现） ----
    {"key": "cursor", "name": "Cursor", "adapter": "files",
     "root": "~/.cursor", "globs": ["**/chats/*.json", "**/sessions/*.jsonl"]},
    {"key": "kiro", "name": "Kiro", "adapter": "files",
     "root": "~/.kiro", "globs": ["sessions/*.json", "**/chats/*.json"]},
]

# 名称里带这些词的目录，会进启发式扫描（大小写不敏感）
AI_KEYWORDS = (
    "ai", "agent", "copilot", "gemini", "cursor", "cline", "roo", "continue",
    "aider", "windsurf", "trae", "lingma", "comate", "codegeex", "kiro",
    "qwen", "opencode", "goose", "crush", "amp", "zcode", "dsh", "workbuddy",
    "claude", "chat", "llm", "atomcode", "codex", "deepseek", "qoder", "iflow",
    "opensquilla", "squilla",
)

VSCODE_APPS = ("Code", "Code - Insiders", "Cursor", "Windsurf", "Trae",
               "VSCodium", "Positron", "Kiro")

VSCODE_EXT = {
    "github.copilot-chat": ("copilot-chat", "GitHub Copilot Chat"),
    "saoudrizwan.claude-dev": ("cline", "Cline"),
    "rooveterinaryinc.roo-cline": ("roo", "Roo Code"),
    "kilocode.kilo-code": ("kilo", "Kilo Code"),
    "continue.continue": ("continue-ext", "Continue"),
    "aminer.codegeex": ("codegeex-ext", "CodeGeeX"),
    "alibaba-cloud.tongyi-lingma": ("lingma", "通义灵码"),
    "baiducomate.comate": ("comate", "Baidu Comate"),
    "augment.vscode-augment": ("augment", "Augment Code"),
    "sourcegraph.cody-ai": ("cody", "Sourcegraph Cody"),
    "amazonwebservices.amazon-q-vscode": ("amazonq", "Amazon Q"),
    "tabnine.tabnine-vscode": ("tabnine", "Tabnine"),
    "google.gemini-cli-vscode-ide-companion": ("gemini-ext", "Gemini CLI"),
}

# 启发式扫描时跳过的大目录
SKIP_DIRS = {
    "cache", "caches", "logs", "log", "code cache", "gpucache", "shadercache",
    "node_modules", "blob_storage", "service worker", "crashpad", "temp", "tmp",
    "electron-user-data", "binaries", "packages", "package cache", "extensions",
    "d3dscache", "iconcache", "backup", "backups", "recovery", "local storage",
    "session storage", "indexeddb", "dawncache", "dawnwebgpucache",
    # 自己的缓存 / 客户端窗口的浏览器 profile，别当成 AI 客户端
    "aisessionmanager", "ai-session-manager", ".ai-session-manager",
    "app-profile",
}
