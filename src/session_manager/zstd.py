#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zstd 解压：DSH 的会话是「多个 zstd 帧首尾相接」的 .jsonl.zstd。

Python 3.14 起标准库有 compression.zstd（取决于编译时的 libzstd），
老版本可以装 pyzstd / zstandard；都没有时退回调用 node（Node 22.15+ 自带 zstd）。
都不行就返回 None —— 调用方用未压缩的元数据兜底。
"""
from __future__ import annotations

import os
import shutil
import subprocess

_CACHE = {}


_MAGIC = b"\x28\xb5\x2f\xfd"


def _frames(data):
    """把首尾相接的 zstd 帧切开（DSH 的会话文件就是这样写的）。"""
    pos = []
    i = 0
    while True:
        j = data.find(_MAGIC, i)
        if j < 0:
            break
        pos.append(j)
        i = j + 4
    if len(pos) <= 1:
        return [data]
    out = []
    for k, start in enumerate(pos):
        end = pos[k + 1] if k + 1 < len(pos) else len(data)
        out.append(data[start:end])
    return out


def _python_decoder():
    try:
        from compression import zstd as _z  # Python 3.14+
        return _z.decompress
    except Exception:  # noqa: BLE001
        pass
    try:
        import pyzstd
        return pyzstd.decompress
    except ImportError:
        pass
    try:
        import zstandard
        dec = zstandard.ZstdDecompressor()
        return lambda b: dec.decompress(b)
    except ImportError:
        return None


def _python_decompress(data):
    dec = _python_decoder()
    if dec is None:
        return None
    try:
        chunks = [dec(f) for f in _frames(data)]
    except Exception:  # noqa: BLE001
        return None
    out = b"".join(chunks)
    return out or None


# DSH 文件是首尾相接的多个 zstd 帧，node 的 zstdDecompressSync 只解第一帧，
# 所以按 magic 切开逐帧解。
_NODE_JS = r"""
const fs = require('fs'), z = require('zlib');
const b = fs.readFileSync(process.argv[1]);
const mag = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
let pos = [], i = 0;
while (true) { const j = b.indexOf(mag, i); if (j < 0) break; pos.push(j); i = j + 4; }
if (!pos.length) { process.exit(3); }
const out = [];
for (let k = 0; k < pos.length; k++) {
  const end = k + 1 < pos.length ? pos[k + 1] : b.length;
  try { out.push(z.zstdDecompressSync(b.subarray(pos[k], end))); } catch (e) {}
}
process.stdout.write(Buffer.concat(out));
"""


def _node_bins():
    bins = []
    exe = shutil.which("node")
    if exe:
        bins.append(exe)
    home = os.path.expanduser("~")
    for rel in (
        ".dsh/dsh-runtimes/dsh-next-primary-runtime/dependencies/node/bin/node.exe",
        ".dsh/dsh-runtimes/dsh-next-primary-runtime/dependencies/node/bin/node",
        ".workbuddy/binaries/node/node.exe",
    ):
        p = os.path.join(home, *rel.split("/"))
        if os.path.isfile(p) and p not in bins:
            bins.append(p)
    return bins


def _node_decompress(path):
    for exe in _node_bins():
        try:
            r = subprocess.run(
                [exe, "-e", _NODE_JS, path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout:
            return r.stdout
    return None


def decompress_file(path, whole=False):
    """读文件并解压。whole=True 表示单帧（普通 .zst）。失败返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if not whole:
        out = _python_decompress(data)
        if out:
            return out
    else:
        out = _python_decompress(data)
        if out:
            return out
    return _node_decompress(path)


def available():
    """快速探测：能不能解压（只判断依赖是否存在，不实际跑）。"""
    if "ok" in _CACHE:
        return _CACHE["ok"]
    ok = _python_decoder() is not None or bool(_node_bins())
    _CACHE["ok"] = ok
    return ok
