#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""provider 注册表：固定的 5 个 + 本机发现的其它 AI 客户端。

    all_providers()   -> 本次扫描到的全部 provider（带 30 秒缓存）
    all_providers(force=True) -> 强制重扫
    get_provider(key) -> 按 key 取
    keys()            -> 全部 key

新增一个「固定」工具：写 Provider 子类加进 discovery.PRIMARY。
新增一个「通用格式」客户端：在 catalog.SPECS 加一条即可。
"""
from __future__ import annotations

from . import discovery
from .claude import ClaudeProvider
from .codex import CodexProvider
from .dsh import DshProvider
from .workbuddy import WorkbuddyProvider
from .zcode import ZcodeProvider

PRIMARY_KEYS = list(discovery.PRIMARY_KEYS)


def all_providers(force=False):
    return discovery.all_providers(force)


def get_provider(key):
    return discovery.get_provider(key)


def keys(force=False):
    return discovery.keys(force)


def invalidate():
    discovery.invalidate()


__all__ = ["ClaudeProvider", "CodexProvider", "DshProvider",
           "WorkbuddyProvider", "ZcodeProvider", "all_providers",
           "get_provider", "keys", "invalidate", "PRIMARY_KEYS"]
