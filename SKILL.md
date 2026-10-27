---
name: session-manager
description: 管理 Claude Code 会话历史记录（~/.claude/projects 下的 .jsonl 文件）：列出、查看、删除、按时间/大小批量清理、回收站恢复、占用统计，并提供本地网页界面（勾选删除、回收站恢复、可搜索的项目筛选）。当用户提到"会话历史 / 历史记录 / 会话列表 / 删除会话 / 清理会话 / 释放磁盘 / 找回误删会话 / 网页管理会话"时使用。Use when the user asks to list, inspect, delete, bulk-clean, or restore Claude Code session history files, or wants a GUI for it.
---

# Session Manager — 会话历史记录管理

每个 `.jsonl` 文件就是一次会话。本工具用一个零依赖的 Python 包管理它们，
与项目用什么语言、什么框架无关。

源码位置：`C:\Users\duany\Desktop\ClaudeSessionManager`（以下用 `$APP` 代指）

```powershell
$APP = "$HOME\Desktop\ClaudeSessionManager"
python "$APP\run.py"                 # 网页界面
python -m session_manager list       # 命令行（需先 pip install -e $APP，或在 $APP\src 下运行）
```

不想装 Python 就用 `$APP\dist\ClaudeSessionManager.exe`（双击即用，内置运行时）。

## 快速参考

| 命令 | 作用 |
| --- | --- |
| `dist\ClaudeSessionManager.exe` | **桌面版**：双击即用，免装 Python，内置网页界面 |
| `python run.py` | **网页界面**：浏览器里勾选删除、回收站恢复、批量清理（推荐给不想敲命令的人） |
| `python -m session_manager list` | 列出所有会话（默认最近 30 条，按最后活动倒序） |
| `python -m session_manager list -p HIS` | 只看项目路径/目录名含 "HIS" 的会话（支持 `*` 通配） |
| `python -m session_manager list -c` | 只看当前目录对应项目的会话 |
| `python -m session_manager list -s size` | 按大小排序（`time` / `size` / `lines`） |
| `python -m session_manager list -n 0` | 显示全部会话 |
| `python -m session_manager show <id>` | 会话详情：真实消息数、成本、时长、首末提问、恢复命令 |
| `python -m session_manager del <id>` | 删除指定会话（默认进回收站，可恢复） |
| `python -m session_manager del #3` | 按 list 显示的编号删除 |
| `python -m session_manager del <id> --purge` | 永久删除（不进回收站） |
| `python -m session_manager clean --older-than 30d` | 批量清理 30 天前的会话（列出后交互确认） |
| `python -m session_manager clean --older-than 7d --keep-last 3` | 每项目保留最近 3 个，其余超 7 天的清理 |
| `python -m session_manager clean --bigger-than 5m --dry-run` | 只预览大于 5M 的会话，不删除 |
| `python -m session_manager trash` | 查看回收站 |
| `python -m session_manager restore 1 3` | 按回收站编号恢复 |
| `python -m session_manager empty-trash` | 清空回收站（永久） |
| `python -m session_manager stats` | 各项目占用统计，找"谁最占地方" |
| `python -m session_manager`（无参数） | 交互模式：回车刷新 / `d 编号` 删除 / `s 编号` 详情 / `t` 回收站 / `q` 退出 |

在 `$APP` 目录下直接跑 `python run.py`；要全局用 `python -m session_manager`，先执行一次
`pip install -e $APP`（`pyproject.toml` 里已注册 `session-manager` / `claude-session-manager` 两个命令）。

**`<id>` 写法**：会话 ID 前缀（≥4 位更稳，如 `1a2d8e2b`），或 list 里的 `#编号`。

## 网页界面

```bash
python run.py                  # 启动并自动打开浏览器（默认 http://127.0.0.1:8765）
python run.py --port 8899      # 换端口（占用会自动往后找）
python run.py --no-browser     # 只起服务
```

页面里有四个标签：**会话**（搜索/筛选/排序，项目下拉框可以直接输入关键字搜索；
点整行或勾复选框选中多个（Shift 连选一段），顶部「批量删除」按钮或底部浮动条一键移入回收站 /
永久删除；点标题看详情）、**回收站**（逐个恢复、清空）、**批量清理**（按天数/大小/每项目保留数
预览后再执行）、**统计**（各项目占用条）。右上角可切换浅色/深色主题。
删除前弹出确认框，永久删除还要额外勾选确认。服务只监听 `127.0.0.1`，按 `Ctrl+C` 退出。

用户说"想要个界面 / 网页操作 / 不想敲命令"时，直接用这个命令启动，别另写 GUI。

## 目录与打包

```
$APP/
├── run.py                      # 入口（exe 也从这里启动）
├── ClaudeSessionManager.spec   # PyInstaller 配置
├── pyproject.toml              # src 布局、命令入口
├── src/session_manager/        # core.py / server.py / webui/
└── dist/ClaudeSessionManager.exe   # 打包产物
```

改完代码或界面后重打包：

```powershell
cd $APP
pip install -r requirements-dev.txt          # 只需要 pyinstaller
python -m PyInstaller --noconfirm --clean ClaudeSessionManager.spec
# 产物：dist\ClaudeSessionManager.exe（PyInstaller 默认输出到 dist）
```

exe 是单文件包（约 8.7 MB，内置 Python 运行时与 `webui/` 界面资源），目标机不用装 Python。
双击后：起本地服务 → 自动打开浏览器 → 黑色窗口留着别关，关掉窗口即退出。
支持 `--port 8899`、`--no-browser` 两个参数。

## 代理工作流程

1. 用户要"看会话 / 历史记录 / 哪些会话占空间" → 跑 `list`（可用 `-p 关键词`、`-s size`、`stats`），把结果整理后转述给用户。
2. 用户说"删掉第 N 个 / 某个会话" → `del <id 或 #N>`；确认输出里的标题、时间、项目与用户所指一致再执行。
3. 用户说"清理旧会话 / 释放空间" → 先 `clean --older-than 30d --dry-run` 预览，跟用户确认后再去掉 `--dry-run` 执行（或加 `-y`）。
4. 用户说"误删了 / 删错了" → `trash` 找到编号 → `restore <编号>`。
5. 删完用 `stats` 报告释放了多少空间。

不要替用户"猜"要删哪些会话：先列出、再确认、后删除。批量删除一律先 `--dry-run`。
用户在网页上勾选删除时，删除动作由用户自己点确认，不要代为调用接口删除。

## 安全规则

- **默认删除 = 移入回收站** `~/.claude/session-trash/`，只有 `--purge` 才永久删除。
- **当前正在运行的会话受保护**（通过 `CLAUDE_CODE_SESSION_ID` 识别），需 `--force` 才能删。
- 交互确认支持 `1,3-5` 选段、`a` 全部、回车取消；`-y` 跳过确认。
- 文件正被其他 Claude 窗口占用时删除会失败，提示用户先关闭对应窗口。
- 读取是只读操作；删除任何文件前都会先展示文件名、时间、大小、标题。

## 实现说明

- 环境要求：Python 3.8+，只用标准库，Windows / macOS / Linux 通用。
- `list` 是快速视图（只读文件头尾 64K/16K，不解析整份记录），上千个会话也秒出。
- `show` 和 `list --deep` 才全文解析，统计真实用户/助手消息数与成本。
- `--root <目录>` 可指向另一个 `.claude` 根目录（备份盘排查、测试用）。
- 想继续某个会话：`claude --resume <session-id>`。
