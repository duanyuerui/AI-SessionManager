#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 等价成本：把 token 用量折算成「按官方 API 价目要花多少钱」。

价目单位是「美元 / 百万 token」，字段与 bucket 对齐：
    input（缓存未命中的输入）、output（输出）、cache_write（缓存写）、cache_read（缓存读）

规则：
1. 内置一张常见模型的价目表（价目核对于 2026-10，来源见 README），
   按模型名里的关键字匹配，越具体的规则排在越前面。
2. 缓存目录下可以放 prices.json 覆盖或补价，键是模型名（精确或片段，忽略大小写）：
       {
         "_peak": "01:00-04:00,06:00-10:00",
         "deepseek-v4.1-flash": {
           "input": 0.30, "output": 1.20, "cache_write": 0.0, "cache_read": 0.006,
           "offpeak": {"input": 0.15, "output": 0.60,
                       "cache_write": 0.0, "cache_read": 0.003}
         }
       }
   下划线开头的是配置键；只写部分字段时其余沿用内置价；"offpeak": null 表示不分峰谷。
3. 认不出的模型（自建代理、内部代号）按 Sonnet 档兜底，避免整块显示为空。

峰价 / 谷时价：
    官方说的「错峰/空闲时段」是「高峰时段以外」的全部时间，高峰时段本身还可能不止一段，
    所以这里配置的是**高峰时段**（UTC，可多段，默认只对周一至周五生效）：
        "_peak": "01:00-04:00,06:00-10:00"     # DeepSeek：北京 9-12 点、14-18 点的 UTC 写法
        "_peak_weekdays": true                  # 只算工作日（默认）；法定节假日不单独处理
    解析会话时按每条记录的时间把 token 拆成「峰 / 谷」两份（bucket 里的 *_off 键），
    成本 = 峰时量 × 峰价 + 谷时量 × 谷价。

环境变量：
    AISM_PRICES            自定义价目文件路径（默认 <缓存目录>/prices.json）
    AISM_CNY_RATE          美元兑人民币汇率，默认 7.2
    AISM_PEAK_WINDOW       高峰时段（UTC），"01:00-04:00,06:00-10:00"，空字符串表示不分峰谷
    AISM_PEAK_WEEKDAYS     高峰时段是否只算工作日（1/0），默认 1
"""
from __future__ import annotations

import json
import os
import re
import time

KEYS = ("input", "output", "cache_write", "cache_read")

# 未识别模型的兜底价（Sonnet 5.5 档）
FALLBACK = {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.1}

_DEFAULT_CNY_RATE = 7.2
# DeepSeek 高峰：北京周一至周五 9-12 点、14-18 点 → UTC 01-04、06-10 点
_DEFAULT_PEAK = "01:00-04:00,06:00-10:00"

# (模型名关键字, 输入, 输出, 缓存写, 缓存读, 谷时价 or None)，按顺序匹配，先命中先用。
# 价目来源（2026-10 核对）：
#   claude.com/pricing、api-docs.deepseek.com/quick_start/pricing、
#   ai.google.dev/gemini-api/docs/pricing；国内厂商 / OpenAI 按用户提供的价目表
#   （人民币价按 7.2 折算成美元；缓存价官方没单列的按「写=输入、读=输入 10%」估算）。
# 只有「高峰时段以外打折」的模型才填谷时价；Qwen 夜间 4 折、豆包长度阶梯、
# Gemini/OpenAI 的 Batch/Flex 折扣都跟时间窗对不上，暂按标准价算。
_TABLE = (
    # Anthropic（USD / MTok）
    ("fable", 10.0, 50.0, 12.5, 0.25, None),
    ("opus-5-5", 4.0, 20.0, 5.0, 0.20, None),
    ("opus-5.5", 4.0, 20.0, 5.0, 0.20, None),
    ("opus", 5.0, 25.0, 6.25, 0.50, None),
    ("sonnet-5-5", 2.0, 10.0, 2.5, 0.10, None),
    ("sonnet-5.5", 2.0, 10.0, 2.5, 0.10, None),
    ("sonnet-5", 2.0, 10.0, 2.5, 0.20, None),
    ("sonnet", 3.0, 15.0, 3.75, 0.30, None),
    ("haiku-5", 0.10, 0.50, 0.125, 0.01, None),
    ("haiku", 1.0, 5.0, 1.25, 0.10, None),
    ("claude", 2.0, 10.0, 2.5, 0.10, None),
    # DeepSeek（空闲时段 = 高峰价 5 折）
    ("deepseek-v4-pro", 1.32, 3.96, 0.0, 0.044, (0.66, 1.98, 0.0, 0.022)),
    ("deepseek", 0.30, 1.20, 0.0, 0.006, (0.15, 0.60, 0.0, 0.003)),
    # OpenAI（GPT-6 系列：Astra / Sol / Luna）
    ("gpt-6-astra", 10.0, 50.0, 10.0, 1.0, None),
    ("gpt-6-luna", 0.10, 0.50, 0.10, 0.01, None),
    ("gpt-6-sol", 2.0, 10.0, 2.0, 0.20, None),
    ("gpt-6", 2.0, 10.0, 2.0, 0.20, None),
    ("gpt-4o-mini", 0.15, 0.6, 0.15, 0.075, None),
    ("gpt-4o", 2.5, 10.0, 2.5, 1.25, None),
    ("gpt-4.1", 2.0, 8.0, 2.0, 0.5, None),
    ("gpt-5", 1.25, 10.0, 1.25, 0.125, None),
    ("o4-mini", 1.1, 4.4, 1.1, 0.275, None),
    ("o3", 2.0, 8.0, 2.0, 0.5, None),
    ("gpt", 2.5, 10.0, 2.5, 1.25, None),
    # Google Gemini（Standard 付费层，取 ≤200k 档）
    ("2.5-flash-lite", 0.10, 0.40, 0.10, 0.01, None),
    ("2.5-flash", 0.30, 2.50, 0.30, 0.03, None),
    ("2.5-pro", 1.25, 10.0, 1.25, 0.125, None),
    ("3.1-pro", 2.0, 12.0, 2.0, 0.20, None),
    ("flash-lite", 0.25, 1.50, 0.25, 0.03, None),
    ("gemini", 0.75, 3.75, 0.75, 0.075, None),
    # 国内其它（人民币价 ÷ 7.2）
    ("qwen3.8-max", 1.67, 5.00, 1.67, 0.167, None),
    ("qwen", 0.11, 0.38, 0.11, 0.011, None),
    ("glm-5.3-flash", 0.11, 0.39, 0.11, 0.011, None),
    ("glm-5.3", 1.11, 3.89, 1.11, 0.111, None),
    ("glm", 0.11, 0.39, 0.11, 0.011, None),
    ("k2.7", 0.90, 3.75, 0.90, 0.09, None),
    ("k3", 2.78, 13.89, 2.78, 0.278, None),
    ("kimi", 2.78, 13.89, 2.78, 0.278, None),
    ("moonshot", 2.78, 13.89, 2.78, 0.278, None),
    ("hy3", 0.14, 0.56, 0.14, 0.014, None),
    ("hunyuan", 0.14, 0.56, 0.14, 0.014, None),
    ("doubao", 0.83, 4.17, 0.83, 0.083, None),
    ("llama-4", 0.09, 0.30, 0.09, 0.009, None),
    ("llama-3", 0.10, 0.32, 0.10, 0.01, None),
    ("llama", 0.09, 0.30, 0.09, 0.009, None),
    ("minimax", 0.30, 1.20, 0.30, 0.03, None),
    ("ernie", 0.40, 1.20, 0.40, 0.04, None),
)

_RANGE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")

# 部分厂商官网同时给出人民币价，而且和美元价并不是严格按汇率换算（DeepSeek 就是
# $0.30 vs ¥2）。价目页切到人民币时优先用这里的原生人民币价，没有再按汇率换算。
_CNY_TABLE = {
    # DeepSeek 官方人民币价（高峰 / 空闲 5 折）
    "deepseek-v4-pro": {"input": 9.0, "output": 27.0, "cache_write": 0.0,
                        "cache_read": 0.30,
                        "offpeak": {"input": 4.5, "output": 13.5,
                                    "cache_write": 0.0, "cache_read": 0.15}},
    "deepseek": {"input": 2.0, "output": 8.0, "cache_write": 0.0,
                 "cache_read": 0.04,
                 "offpeak": {"input": 1.0, "output": 4.0,
                             "cache_write": 0.0, "cache_read": 0.02}},
    # 国内其它（缓存价按「写 = 输入、读 = 输入 10%」估算）
    "qwen3.8-max": {"input": 12.0, "output": 36.0, "cache_write": 12.0, "cache_read": 1.2},
    "qwen": {"input": 0.8, "output": 2.7, "cache_write": 0.8, "cache_read": 0.08},
    "glm-5.3-flash": {"input": 0.8, "output": 2.8, "cache_write": 0.8, "cache_read": 0.08},
    "glm-5.3": {"input": 8.0, "output": 28.0, "cache_write": 8.0, "cache_read": 0.8},
    "glm": {"input": 0.8, "output": 2.8, "cache_write": 0.8, "cache_read": 0.08},
    "k2.7": {"input": 6.5, "output": 27.0, "cache_write": 6.5, "cache_read": 0.65},
    "k3": {"input": 20.0, "output": 100.0, "cache_write": 20.0, "cache_read": 2.0},
    "kimi": {"input": 20.0, "output": 100.0, "cache_write": 20.0, "cache_read": 2.0},
    "moonshot": {"input": 20.0, "output": 100.0, "cache_write": 20.0, "cache_read": 2.0},
    "hy3": {"input": 1.0, "output": 4.0, "cache_write": 1.0, "cache_read": 0.1},
    "hunyuan": {"input": 1.0, "output": 4.0, "cache_write": 1.0, "cache_read": 0.1},
    "doubao": {"input": 6.0, "output": 30.0, "cache_write": 6.0, "cache_read": 0.6},
}

# 当前生效的高峰时段：[(开始分钟, 结束分钟), ...] 或 None（不分峰谷）
_PEAK = [None]
_PEAK_WEEKDAYS = [True]


# ---------- 高峰 / 谷时 ----------

def _parse_range(s):
    m = _RANGE_RE.match(str(s or ""))
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    a, b = h1 * 60 + m1, h2 * 60 + m2
    return (a, b) if a != b else None


def _parse_peak(s):
    """'01:00-04:00,06:00-10:00' → [(60,240),(360,600)]；解析失败返回 None。"""
    raw = str(s or "").strip()
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        if not part.strip():
            continue
        r = _parse_range(part)
        if r is None:
            return None
        out.append(r)
    return out


def _fmt_min(m):
    return "%02d:%02d" % (m // 60, m % 60)


def set_peak(s, weekdays=True):
    """设置高峰时段；空串 = 不分峰谷。返回规范化后的串。"""
    parsed = _parse_peak(s)
    _PEAK[0] = parsed or None
    _PEAK_WEEKDAYS[0] = bool(weekdays)
    return peak_key()


def peak_key():
    if not _PEAK[0]:
        return ""
    return ",".join("%s-%s" % (_fmt_min(a), _fmt_min(b)) for a, b in _PEAK[0]) \
        + ("|wd" if _PEAK_WEEKDAYS[0] else "")


def is_peak(ts):
    """这条记录是不是落在高峰时段里（UTC，可限定工作日）。"""
    win = _PEAK[0]
    if not win or ts is None:
        return False
    try:
        t = time.gmtime(ts)
    except (ValueError, OSError, OverflowError):
        return False
    if _PEAK_WEEKDAYS[0] and t.tm_wday >= 5:
        return False
    cur = t.tm_hour * 60 + t.tm_min
    for a, b in win:
        if (a <= cur < b) if a <= b else (cur >= a or cur < b):
            return True
    return False


def is_offpeak(ts):
    """高峰期以外的时间就是谷时（只有配置了高峰时段才拆）。"""
    return bool(_PEAK[0]) and not is_peak(ts)


# ---------- 价目 ----------

def cny_rate():
    try:
        v = float(os.environ.get("AISM_CNY_RATE") or _DEFAULT_CNY_RATE)
        return v if v > 0 else _DEFAULT_CNY_RATE
    except (TypeError, ValueError):
        return _DEFAULT_CNY_RATE


def prices_path(cache_dir):
    override = os.environ.get("AISM_PRICES")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(cache_dir, "prices.json")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rate_row(val):
    """{input/output/cache_write/cache_read} → 只保留合法数字。"""
    out = {}
    for k in KEYS:
        n = _num(val.get(k))
        if n is not None and n >= 0:
            out[k] = n
    return out


def load(cache_dir):
    """读自定义价目，顺便把高峰时段设置成当前生效值。

    返回 {"models": {小写模型名: {四个价, 可选 "offpeak": {...} 或 None}},
          "peak": "01:00-04:00,06:00-10:00|wd"}
    """
    path = prices_path(cache_dir)
    models = {}
    peak = os.environ.get("AISM_PEAK_WINDOW")
    wd_env = os.environ.get("AISM_PEAK_WEEKDAYS")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if peak is None:
        peak = raw.get("_peak") if isinstance(raw.get("_peak"), str) else None
    if peak is None:
        peak = _DEFAULT_PEAK
    weekdays = True
    if isinstance(raw.get("_peak_weekdays"), bool):
        weekdays = raw["_peak_weekdays"]
    if wd_env is not None:
        weekdays = str(wd_env).strip().lower() not in ("0", "false", "no", "")
    for name, val in raw.items():
        if not isinstance(name, str) or name.startswith("_") or not isinstance(val, dict):
            continue
        row = _rate_row(val)
        if "offpeak" in val:
            off = val["offpeak"]
            row["offpeak"] = _rate_row(off) if isinstance(off, dict) and off else None
        if row:
            models[name.strip().lower()] = row
    set_peak(peak, weekdays)
    return {"models": models, "peak": peak_key()}


def _same_as_builtin(name, row, off):
    """和内置价（含兜底价）完全一致时不必写进 prices.json。"""
    base = price_of(name, None)
    if any(abs(row[k] - base[k]) > 1e-9 for k in KEYS):
        return False
    boff = base.get("offpeak")
    if off is None:
        return boff is None
    if not boff:
        return False
    return all(abs(off[k] - boff[k]) <= 1e-9 for k in KEYS)


def save(cache_dir, models, peak, weekdays=True):
    """写回 prices.json。

    models = {模型名: {四个价, 可选 "offpeak": {...} 或 None}}
    peak   = 高峰时段（UTC，逗号分隔），空串表示不分峰谷
    """
    win = set_peak(peak, weekdays)
    if peak and not win:
        raise ValueError("高峰时段格式应为 01:00-04:00,06:00-10:00（UTC）")
    data = {}
    if win:
        data["_peak"] = win.split("|")[0]
        if not weekdays:
            data["_peak_weekdays"] = False
    for name, row in (models or {}).items():
        name = str(name or "").strip().lower()
        if not name or name.startswith("_") or len(name) > 200:
            raise ValueError("模型名不合法：%s" % name)
        if not isinstance(row, dict):
            continue
        out = {}
        for k in KEYS:
            n = _num(row.get(k))
            if n is None or n < 0 or n > 100000:
                raise ValueError("%s 的 %s 价目不是合法数字" % (name, k))
            out[k] = round(n, 6)
        off = None
        if "offpeak" in row:
            off = row["offpeak"]
            if off:
                o = {}
                for k in KEYS:
                    n = _num(off.get(k))
                    if n is None:      # 没填的字段按峰价算
                        n = out[k]
                    if n < 0 or n > 100000:
                        raise ValueError("%s 的谷时 %s 价目不是合法数字" % (name, k))
                    o[k] = round(n, 6)
                off = o
            out["offpeak"] = off
        # 和内置价一致的就不用写进文件，将来内置价调整还能跟着走
        if _same_as_builtin(name, out, off):
            continue
        data[name] = out
    path = prices_path(cache_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        raise ValueError("写价目文件失败：%s" % e)
    return path


def _match(model, overrides):
    """按精确 → 片段的顺序找自定义价目。"""
    name = str(model or "").strip().lower()
    if not overrides:
        return None
    if name in overrides:
        return overrides[name]
    for key, val in overrides.items():
        if key and key in name:
            return val
    return None


def price_of(model, overrides=None):
    """模型 → {input, output, cache_write, cache_read, 可选 offpeak}（美元 / 百万 token）。"""
    name = str(model or "").strip().lower()
    row = dict(FALLBACK)
    for key, inp, outp, cw, cr, off in _TABLE:
        if key in name:
            row = {"input": inp, "output": outp,
                   "cache_write": cw, "cache_read": cr}
            if off:
                row["offpeak"] = dict(zip(KEYS, off))
            break
    extra = _match(model, overrides)
    if extra:
        for k in KEYS:
            if k in extra:
                row[k] = extra[k]
        if "offpeak" in extra:
            if extra["offpeak"]:
                # 只写了部分谷时价时，其余字段沿用峰价
                row["offpeak"] = {k: extra["offpeak"].get(k, row[k]) for k in KEYS}
            else:
                row.pop("offpeak", None)
    return row


def known(model, overrides=None):
    """这个模型是不是认得出的（认不出就按 FALLBACK 兜底）。"""
    name = str(model or "").strip().lower()
    if _match(model, overrides):
        return True
    return any(key in name for key, *_ in _TABLE)


def _scaled(p, rate):
    out = {k: round(p[k] * rate, 6) for k in KEYS}
    if p.get("offpeak"):
        out["offpeak"] = {k: round(p["offpeak"][k] * rate, 6) for k in KEYS}
    return out


def price_cny(model, overrides=None):
    """人民币显示价：内置有原生人民币价的直接用，其余按汇率换算。

    用户改过的模型一律按「美元价 × 汇率」，这样人民币下看到的就是自己填的数。
    """
    if _match(model, overrides):
        return _scaled(price_of(model, overrides), cny_rate())
    name = str(model or "").strip().lower()
    for key, *_ in _TABLE:
        if key in name:
            if key in _CNY_TABLE:
                row = dict(_CNY_TABLE[key])
                if "offpeak" in row:
                    row["offpeak"] = dict(row["offpeak"])
                return row
            break
    return _scaled(price_of(model, None), cny_rate())


def cost_bucket(model, bucket, overrides=None):
    """一个 bucket 按该模型的价目折成美元；有谷时价时按 *_off 拆分。"""
    if not bucket:
        return 0.0
    p = price_of(model, overrides)
    off = p.get("offpeak")
    total = 0.0
    for k in KEYS:
        v = float(bucket.get(k) or 0)
        if off:
            o = float(bucket.get(k + "_off") or 0)
            if o > v:
                o = v
            total += (v - o) * p[k] + o * off[k]
        else:
            total += v * p[k]
    return total / 1000000.0


def builtin_rows():
    """内置价目表（「价目」页展示用）。"""
    out = []
    for key, inp, outp, cw, cr, off in _TABLE:
        row = {"match": key, "input": inp, "output": outp,
               "cache_write": cw, "cache_read": cr}
        row["offpeak"] = dict(zip(KEYS, off)) if off else None
        cny = _CNY_TABLE.get(key)
        row["cny"] = dict(cny) if cny else None
        out.append(row)
    return out


# 进程启动时先按默认 / 环境变量把高峰时段定下来，parse_usage 单独跑也不会错
if os.environ.get("AISM_PEAK_WINDOW") is not None:
    _wd = str(os.environ.get("AISM_PEAK_WEEKDAYS") or "1").strip().lower() \
        not in ("0", "false", "no", "")
    set_peak(os.environ.get("AISM_PEAK_WINDOW"), _wd)
else:
    set_peak(_DEFAULT_PEAK, True)
