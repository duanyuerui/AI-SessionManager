---
name: session-manager
description: 扫描本机所有留下会话数据的 AI 编程客户端（固定适配 Claude Code、Codex、DSH、WorkBuddy、ZCode；自动发现 AtomCode、Gemini CLI、Qwen Code、Cline/Roo/Kilo、GitHub Copilot Chat、Continue、OpenCode 等），统一列出、查看、删除、按时间/大小批量清理、回收站恢复、占用统计，以及跨工具 Token 工作台（按天/周/月/年），并提供本地网页界面（左侧菜单切换工具、扫描本机、勾选删除、回收站恢复、可搜索的项目筛选）。当用户提到"会话历史 / 历史记录 / 会话列表 / 删除会话 / 清理会话 / 释放磁盘 / 找回误删会话 / token 用量 / 用了多少 token / 各工具用量 / 本机有哪些 AI / 网页管理会话"时使用。Use when the user asks to list, inspect, delete, bulk-clean, restore AI coding session history, discover which AI clients exist on the machine, or wants a GUI / token usage dashboard.
---

# Session Manager — AI 会话与 Token 管理

一个零依赖的 Python 包，统一管理本机几家 AI 客户端的会话：

固定主工具（完整适配）：

| 工具 | 数据位置 | 删除方式 |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/<项目>/<id>.jsonl` | 移入 `~/.claude/session-trash/` |
| Codex | `~/.codex/sessions/…/rollout-*.jsonl` | 移入 `~/.codex/session-trash/` |
| DSH | `~/.dsh/sessions/…/session.v4.jsonl.zstd` | 会话目录移入 `~/.dsh/session-trash/` |
| WorkBuddy | `~/.workbuddy/workbuddy.db` + `projects/*.jsonl` | 软删除 `sessions.deleted_at` |
| ZCode | `~/.zcode/cli/db/db.sqlite` | 软删除 `session.time_deleted` |
| OpenSquilla | `%APPDATA%\@opensquilla\desktop-electron\opensquilla\state\sessions.db` | 关联行备份成 JSON 后删行（需先退出 OpenSquilla） |

自动发现：启动时扫描家目录 / AppData / Application Support / .config / .local/share
和 VS Code 系编辑器的 globalStorage，用通用适配器接入 AtomCode、Gemini CLI、Qwen Code、
Cline / Roo / Kilo、GitHub Copilot Chat、Continue、OpenCode、Goose、Amp、Crush 等。
另外会按「会话库签名」直接找 `sessions.db` / `chat.db` / `session-store.db` 这类 SQLite
（浅 5 层、3 秒预算），名字完全陌生的客户端也能发现；OpenSquilla 就是这样接进来的。
文件型可删除；SQLite 结构不确定的标为「只读」；OpenSquilla 例外——它的库结构已经摸清，
删除时先把关联行备份成 JSON 再删，回收站可恢复（删除前要先退出 OpenSquilla）。
自动发现的回收站在 `~/.ai-session-manager/trash/<key>/`。

源码位置：`C:\Users\duany\Desktop\AI-SessionManager`（以下用 `$APP` 代指）

```powershell
$APP = "$HOME\Desktop\AI-SessionManager"
python "$APP\run.py"                 # 网页界面
python -m session_manager tools      # 命令行（需先 pip install -e $APP，或在 $APP\src 下运行）
```

不想装 Python 就用 `$APP\dist\AISessionManager.exe`（客户端窗口，双击即用，内置运行时）；
需要看到控制台输出就用 `$APP\dist\ClaudeSessionManager.exe`。

## 快速参考

| 命令 | 作用 |
| --- | --- |
| `dist\AISessionManager.exe` | **客户端版**：无控制台，独立窗口，关窗即退出 |
| `dist\ClaudeSessionManager.exe` | **控制台版**：黑色窗口 + 默认浏览器 |
| `python run.py` | **网页界面**：左侧菜单切换工具，工作台看 Token，勾选删除、回收站恢复 |
| `python run.py --app` | 同客户端版（浏览器 `--app` 模式，无地址栏） |
| `python -m session_manager tools` | 扫描本机：各工具是否安装、会话数、占用、Token |
| `python -m session_manager tools --scan` | 强制重新扫描（默认 30 秒缓存） |
| `python -m session_manager usage --period week` | Token 用量看板（`day` / `week` / `month` / `year`） |
| `python -m session_manager usage --refresh` | 忽略解析缓存，重扫所有会话文件 |
| `python -m session_manager list` | 列出 Claude 会话（`--tool codex` 看别的工具） |
| `python -m session_manager list --tool codex -n 0` | 列出 Codex 全部会话 |
| `python -m session_manager list -p HIS` | 只看项目路径/目录名含 "HIS" 的会话 |
| `python -m session_manager list -c` | 只看当前目录对应项目的会话 |
| `python -m session_manager list -s size` | 排序：`time` / `size` / `lines` / `tokens` |
| `python -m session_manager show <id> [--tool dsh]` | 会话详情：消息数、Token 明细、首末提问、恢复命令 |
| `python -m session_manager del <id>` | 删除（默认进回收站，可恢复） |
| `python -m session_manager del #3` | 按 list 显示的编号删除 |
| `python -m session_manager del <id> --purge` | 永久删除（不进回收站） |
| `python -m session_manager del --tool codex <id>` | 删除 Codex 会话 |
| `python -m session_manager clean --older-than 30d` | 批量清理 30 天前的会话（列出后交互确认） |
| `python -m session_manager clean --older-than 7d --keep-last 3` | 每个项目保留最近 3 个 |
| `python -m session_manager clean --bigger-than 5m --dry-run` | 只预览大于 5M 的会话 |
| `python -m session_manager clean --tool workbuddy --older-than 30d` | 清理 WorkBuddy（软删除） |
| `python -m session_manager trash` | 查看回收站 |
| `python -m session_manager restore 1 3` | 按回收站编号恢复 |
| `python -m session_manager empty-trash` | 清空回收站（永久） |
| `python -m session_manager stats [--tool zcode]` | 各项目占用统计 |
| `python -m session_manager`（无参数） | 交互模式：回车刷新 / `d 编号` 删除 / `s 编号` 详情 / `t` 回收站 / `q` 退出 |

`--tool` 可选：`claude`（默认）/ `codex` / `dsh` / `workbuddy` / `zcode`，
以及 `tools` 扫描出来的任意 key（如 `atomcode`、`gemini`、`copilot-chat`）。
`<id>` 写法：会话 ID 前缀，或 list 里的 `#编号`。

## 网页界面

```bash
python run.py                  # 启动并自动打开浏览器（默认 http://127.0.0.1:8765）
python run.py --app            # 以客户端窗口打开（Edge/Chrome --app，关窗即退出）
python run.py --port 8899      # 换端口（占用会自动往后找）
python run.py --no-browser     # 只起服务
```

- 左侧菜单：**工作台** + 「主工具」5 个 + 「已发现」的其它客户端；主工具没装显示「未安装」，
  已发现的显示会话数，只读的带「只读」标记；检测到目录但还没有会话的收进底部
  可展开的「已检测到 · 暂无会话」分组。左下角「自动扫描」可选 10 秒 / 15 秒 / 30 秒 /
  1 分钟（默认 30 秒）定时重扫，雷达按钮随时立即扫描。
- **工作台**：总 Token / 输入 / 输出 / 缓存读取与命中率；按工具堆叠的柱状图，
  可切换日/周/月/年与时间跨度，也可切成表格；下方是按工具、按模型的排行。
- **会话**：搜索、项目下拉（可输入关键字）、排序；勾选/Shift 连选批量删除；
  点标题看详情。**回收站**：恢复、清空。**批量清理**：先预览再执行。**占用**：按项目看磁盘。
- 右上角切浅色/深色；永久删除需额外勾选确认。服务只监听 `127.0.0.1`。

用户说"想要个界面 / 网页操作 / 不想敲命令 / 看 token 用量"时，直接用这个命令启动，别另写 GUI。

## 目录与打包

```
$APP/
├── run.py                      # 入口（两个 exe 都从这里启动）
├── ClaudeSessionManager.spec   # PyInstaller 配置（控制台版 + 客户端版）
├── pyproject.toml              # src 布局、命令入口
├── src/session_manager/        # core / server / usage / zstd / providers / webui
└── dist/
    ├── AISessionManager.exe        # 客户端版（console=False）
    └── ClaudeSessionManager.exe    # 控制台版（console=True）
```

改完代码或界面后重打包：

```powershell
cd $APP
pip install -r requirements-dev.txt          # 只需要 pyinstaller
python -m PyInstaller --noconfirm --clean ClaudeSessionManager.spec
# 产物：dist\AISessionManager.exe + dist\ClaudeSessionManager.exe
```

两个 exe 共用同一份代码，只有 `console=` 不同。打包前先关掉正在运行的旧 exe，
否则 Windows 会锁住文件导致覆盖失败。

exe 是单文件包（内置 Python 运行时与 `webui/` 界面资源），目标机不用装 Python。
双击后：起本地服务 → 自动打开浏览器 → 黑色窗口留着别关，关掉窗口即退出。
支持 `--port 8899`、`--no-browser` 两个参数。

## 代理工作流程

1. 用户问"本机有哪些 AI / 装了哪些 / 一共用了多少 token" → `tools`（必要时 `--scan`）；
   要看趋势 → `usage --period day|week|month|year`。发现的 key 可以直接用于 `--tool`。
2. 用户要"看会话 / 哪些会话占空间" → `list`（可加 `--tool`、`-p 关键词`、`-s size`）或 `stats --tool`。
3. 用户说"删掉第 N 个 / 某个会话" → `del <id 或 #N> [--tool]`；确认输出里的标题、时间、项目与用户所指一致。
4. 用户说"清理旧会话 / 释放空间" → 先 `clean … --dry-run` 预览，确认后再执行（或加 `-y`）。
5. 用户说"误删了" → `trash --tool …` 找到编号 → `restore <编号>`。
   WorkBuddy / ZCode 是数据库软删除，恢复后让用户重启对应应用。
6. 删完用 `stats` 报告释放了多少空间。

不要替用户"猜"要删哪些会话：先列出、再确认、后删除。批量删除一律先 `--dry-run`。
用户在网页上勾选删除时，删除动作由用户自己点确认，不要代为调用接口删除。

## 安全规则

- **默认删除 = 移入回收站**，只有 `--purge` 才永久删除；数据库型工具写软删除标记。
  自动发现的客户端回收站统一在 `~/.ai-session-manager/trash/<key>/`，只读发现不允许删除。
- **当前正在运行的 Claude 会话受保护**（`CLAUDE_CODE_SESSION_ID`），需 `--force` 才能删。
- 交互确认支持 `1,3-5` 选段、`a` 全部、回车取消；`-y` 跳过确认。
- 文件正被其他窗口占用时删除会失败，提示用户先关闭对应应用。
- 读取是只读操作（SQLite 用 `mode=ro`）；删除前都会展示标题、时间、大小。

## 实现说明

- 环境要求：Python 3.8+，只用标准库，Windows / macOS / Linux 通用。
- DSH 的 `.zstd` 是多帧拼接，按帧切开解压；解压依赖依次尝试
  Python 3.14 `compression.zstd` → `pyzstd`/`zstandard` → 本机 node，
  都不可用时用 `session_projcache` 的元数据兜底（Token 标记为估算）。
- 自动发现按「固定 5 个 → catalog 清单 → 启发式扫描 → 会话库签名扫描」四路进行，
  结果缓存 30 秒；`tools --scan`、界面雷达按钮、`/api/tools?scan=1` 强制重扫。
- Token 解析按 `(size, mtime)` 缓存在 `%LOCALAPPDATA%\AISessionManager\usage-cache.json`，
  只重扫变化的文件；`usage --refresh` 或界面「重新扫描」强制刷新。
- `list` 是快速视图（只读文件头尾），不解析整份记录；Token 来自缓存，首次看工作台会全量扫描一次。
- `--root <目录>` 可覆盖 Claude 根目录；环境变量 `AISM_<KEY>_ROOT` 可覆盖任意工具（包括自动发现的）
  根目录，key 里的 `-` 写成 `_`（测试/非默认安装位置用）。
- 想继续某个会话：详情里会给出各工具自己的恢复命令（如 `claude --resume <id>`）。
