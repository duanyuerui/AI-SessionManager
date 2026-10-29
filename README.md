# AISessionManager — 扫描本机 AI，会话与 Token 一个工作台

不再写死工具清单：启动时扫描本机，**有什么 AI 客户端就显示什么**。
固定的 5 个（Claude Code / Codex / DSH / WorkBuddy / ZCode）做完整适配，
其它能找到会话数据的客户端通过通用适配器自动接进来（AtomCode、OpenSquilla、Gemini CLI、
Qwen Code、Cline / Roo Code / Kilo、GitHub Copilot Chat、Continue、OpenCode、
Goose、Amp、Crush……），格式还没吃透的先以「只读发现」展示。

左侧菜单随时切换，能搜索、看详情、删除（先进回收站）、批量清理、看磁盘占用；
「工作台」把所有工具的 Token 用量按天 / 周 / 月 / 年汇总，支持按工具、按模型拆分。

零第三方依赖，只用 Python 3.8+ 标准库；也可以直接双击 exe，不用装 Python：

- `dist\AISessionManager.exe` — **客户端版**：没有黑色控制台窗口，
  用 Edge/Chrome 的 `--app` 模式打开一个独立窗口（无地址栏 / 标签栏），
  关掉窗口服务就退出，不留后台进程。
- `dist\ClaudeSessionManager.exe` — **控制台版**：保留黑色窗口，启动后打开默认浏览器。

没装的主工具会在左侧显示「未安装」，点进去给出它应该出现的数据目录。

## 扫描与发现

启动时（以及点左下角雷达按钮 / `tools --scan` 时）做三件事：

1. **固定的 5 个主工具**：Claude Code、Codex、DSH、WorkBuddy、ZCode，永远出现在菜单里，
   没装显示「未安装」。
2. **已知客户端清单**（`providers/catalog.py`）：约 20 个常见 AI 客户端的数据目录，
   路径存在且有会话才加入。
3. **启发式扫描**：家目录隐藏目录、`%APPDATA%`、`%LOCALAPPDATA%`、
   `~/Library/Application Support`、`~/.config`、`~/.local/share` 里名字像 AI 的目录
   （含 npm 风格的 `@scope` 目录），以及 VS Code / Cursor / Windsurf / Trae 等编辑器的
   `globalStorage` 扩展目录；找到会话文件或 `sessions` 表才加入。
4. **会话库签名扫描**：不再只看目录名 —— 在上述位置（AppData / 家目录等，浅 5 层）
   直接找 `sessions.db` / `chat.db` / `session-store.db` 这类库，只读验证里面确实有
   `sessions` 表或「会话 id + 角色 + 正文」的消息表才认。名字完全陌生的客户端也能发现
   （OpenSquilla 就是这样自动接进来的），代价是有 3 秒的扫描预算和 30 秒缓存。

通用适配器覆盖三类格式：

- **文件型**：一个 `.jsonl` / `.json` 文件一次会话（宽松解析 role/type、timestamp、
  usage/tokens，兼容 Claude 系、Gemini CLI、Continue、OpenCode 等）
- **任务目录型**：`tasks/<id>/` 一次会话（Cline / Roo Code / Kilo Code）
- **SQLite 型**：`sessions` + `turns` 表（GitHub Copilot Chat 等），只读展示

发现的客户端在左侧「已发现」分组里；能确定删除方式的可以正常删除，数据库结构不确定的
标为「只读」，只浏览不改动。

## 各工具的数据从哪来

| 工具 | 数据位置 | 会话正文 | 删除方式 |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/projects/<项目>/<id>.jsonl` | 每行一条 JSON | 文件移入 `~/.claude/session-trash/` |
| Codex | `~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl` | rollout JSONL | 移入 `~/.codex/session-trash/` |
| DSH | `~/.dsh/sessions/<工作区>/session-*/session.v4.jsonl.zstd` | 多帧 zstd 压缩 JSONL | 整个会话目录移入 `~/.dsh/session-trash/` |
| WorkBuddy | 索引 `~/.workbuddy/workbuddy.db`，正文 `~/.workbuddy/projects/<项目>/<id>.jsonl` | JSONL | 写 `sessions.deleted_at`（应用自己的软删除） |
| ZCode | `~/.zcode/cli/db/db.sqlite`（session / message / part / model_usage） | SQLite | 写 `session.time_deleted`（应用自己的软删除） |
| AtomCode | `~/.atomcode/sessions/<项目>/<id>.meta` + `.snapshot` | JSON | 整组文件移入 `~/.ai-session-manager/trash/atomcode/` |
| OpenSquilla | `%APPDATA%\@opensquilla\desktop-electron\opensquilla\state\sessions.db`（session / transcript_entries / usage_events） | SQLite | 只读浏览（对方库没有删除标记，不直接改） |
| 其它发现的客户端 | 见 `providers/catalog.py` / 扫描结果 | JSONL / JSON / tasks / SQLite | 文件型移入 `~/.ai-session-manager/trash/<key>/`；数据库型只读 |

自动发现的客户端回收站统一放在 `~/.ai-session-manager/trash/<key>/`，不往对方的数据目录里塞文件。
每个客户端的数据目录都可以用环境变量 `AISM_<KEY>_ROOT` 覆盖（key 里的 `-` 写成 `_`），
方便数据放在非默认位置或做测试。

- DSH 的 `.zstd` 是**首尾相接的多个 zstd 帧**，普通解压只出第一帧。本工具按帧切开解压，
  解压能力按「Python 3.14 `compression.zstd` → `pyzstd`/`zstandard` → 本机 node」依次回退；
  都不可用时会退回 `~/.dsh/storages/session_projcache` 里的标题和 token 总量，仍能列出/管理会话，
  工作台会标记「估算」。
- WorkBuddy / ZCode 是数据库软删除，恢复后重新打开对应应用即可看到。

## 运行

```bash
python run.py                   # 起网页界面并自动打开浏览器（默认 http://127.0.0.1:8765）
python run.py --app             # 以客户端窗口打开（无地址栏，关窗即退出）
python run.py --port 8899       # 换端口（被占用会自动往后找）
python run.py --no-browser      # 只起服务
```

装成包之后也可以用命令入口：

```bash
pip install -e .
python -m session_manager web           # 网页界面（等同 python run.py）
python -m session_manager tools         # 看本机装了哪些客户端
python -m session_manager usage         # 终端里的 Token 用量看板
```

`dist\ClaudeSessionManager.exe` 与 `python run.py` 一致，`dist\AISessionManager.exe` 与
`python run.py --app` 一致，双击即用。服务只监听 `127.0.0.1`。客户端版关窗口即退出；
控制台版关掉黑色窗口即退出。

## 命令行

```bash
# 扫描本机：会话数、占用、Token、数据目录（--scan 强制重扫）
python -m session_manager tools --scan

# Token 用量：按天 / 周 / 月 / 年；--refresh 忽略缓存重扫
python -m session_manager usage --period week -n 8

# Claude 的详细命令（默认 --tool claude）
python -m session_manager list                 # 列出会话
python -m session_manager show <id>            # 会话详情
python -m session_manager del <id>             # 删除（默认移入回收站）
python -m session_manager trash                # 看回收站
python -m session_manager restore 1 3          # 恢复
python -m session_manager clean --older-than 30d --dry-run
python -m session_manager stats

# 其它工具用 --tool 指定（list/show/del/clean/stats 都支持）
python -m session_manager list --tool codex -n 0
python -m session_manager show --tool dsh <id>
python -m session_manager del --tool codex <id>
python -m session_manager clean --tool workbuddy --older-than 30d --dry-run
python -m session_manager stats --tool zcode
python -m session_manager list --tool atomcode -n 0
python -m session_manager show --tool gemini <id>
```

完整用法见 `SKILL.md`。

## 界面

左侧菜单：**工作台** + 「主工具」5 个 + 「已发现」的其它客户端。
主工具没装显示「未安装」；已发现的显示会话数，只读的带「只读」标记；
数据目录存在但还没读到会话的收进底部可展开的「已检测到 · 暂无会话」分组。
左下角「自动扫描」可选 10 秒 / 15 秒 / 30 秒 / 1 分钟（默认 30 秒）自动重新扫描本机，
选择记在 localStorage；旁边的雷达按钮随时立即扫描，刷新按钮重拉当前页面数据。

- **工作台** — Token 总量、输入 / 输出 / 缓存读取与命中率；堆叠柱状图按工具拆分，
  可切换日 / 周 / 月 / 年与时间跨度；5 个主工具各自一色，自动发现的客户端聚合为
  「其他 AI」（逐工具数字在下方「按工具」列表里）；工具筛选 chips、按工具占比、
  按模型排行；图表可切「表格」视图。颜色经过色相 / 对比度校验，浅色与深色各有一套取值。
- **会话** — 搜索标题/项目/会话 ID，按项目筛选（可输入关键字的 Tom Select 下拉），
  按最后活动 / 大小 / Token / 项目 / 标题排序；勾选多行（Shift 连选）批量删除，
  点标题看详情（消息数、Token 明细、首末提问、恢复命令）。
- **回收站** — 逐个恢复、清空。文件型是移动目录，数据库型是撤销应用的软删除标记。
- **批量清理** — 按天数 / 大小 / 每项目保留数预览，确认后再执行；支持永久删除。
- **占用** — 按项目汇总磁盘占用，找「谁最占地方」。

右上角主题按钮在「跟随系统 → 浅色 → 深色」之间切换，选择记在 localStorage。
删除前弹确认框，永久删除还要额外勾选确认。

### Token 统计口径

| 工具 | 来源 | 说明 |
| --- | --- | --- |
| Claude Code | assistant 消息的 `message.usage`，按 `message.id` 去重 | `input_tokens` 不含缓存 |
| Codex | `event_msg/token_count` 的累计值差分 | `input_tokens` 含缓存，已减去 `cached_input_tokens` |
| DSH | 会话事件里的 `assistant/message.data.usage` | `inputTokens` 不含缓存 |
| WorkBuddy | 消息行里的 `message.usage` / `providerData.rawUsage` | 已减去 `cached_tokens` |
| ZCode | `model_usage` 表 | `input_tokens` 含缓存，已减去 `cache_read_input_tokens` |
| OpenSquilla | `usage_events` / `usage_event_items` 表 | `input_tokens` 含缓存，已减去 `cache_read_tokens` |
| 自动发现的客户端 | 通用解析 `usage` / `tokens` / `tokenUsage` / `rawUsage` 等字段 | 有 `total` 时按 total 判断 input 是否含缓存；没有时按字段风格推断 |

合计 = 非缓存输入 + 缓存读 + 缓存写 + 输出；推理 token 单列展示、不重复相加。
日期按本机时区切分。解析结果按 `(文件大小, 修改时间)` 缓存在
`%LOCALAPPDATA%\AISessionManager\usage-cache.json`（macOS：`~/Library/Caches/AISessionManager/`，
Linux：`~/.cache/ai-session-manager/`），只重扫有变化的文件，可用「重新扫描」强制刷新。

## 目录结构

```
AI-SessionManager/
├── run.py                      # 开发入口（exe 也从这里启动）
├── ClaudeSessionManager.spec   # PyInstaller 打包配置
├── pyproject.toml              # src 布局 + 命令入口
├── requirements-dev.txt        # 仅打包需要的 pyinstaller
├── src/session_manager/
│   ├── core.py                 # 会话管理 + 命令行
│   ├── server.py               # 本地 HTTP 服务
│   ├── usage.py                # Token 解析缓存与汇总
│   ├── zstd.py                 # DSH 多帧 zstd 解压（多级回退）
│   ├── providers/              # 各家客户端适配
│   │   ├── base.py             # 统一接口、文件回收站、缓存工具
│   │   ├── claude.py / codex.py / dsh.py
│   │   ├── workbuddy.py / zcode.py
│   │   ├── atomcode.py / opensquilla.py   # 单独适配的发现型客户端
│   │   ├── generic.py          # 通用适配器（文件 / 任务目录 / SQLite）
│   │   ├── catalog.py          # 已知客户端路径清单
│   │   └── discovery.py        # 固定 + 清单 + 启发式 + 会话库签名扫描
│   └── webui/
│       ├── index.html          # 单页界面（左侧菜单 + 工作台）
│       ├── tailwind.css        # 源码样式：设计变量 + 组件层（编译输入）
│       ├── tailwind.config.js  # Tailwind 配置
│       └── vendor/             # 编译好的 tailwind.css + Tom Select
└── dist/
    ├── AISessionManager.exe      # 客户端版（无控制台，独立窗口）
    └── ClaudeSessionManager.exe  # 控制台版（黑色窗口 + 默认浏览器）
```

界面资源在运行时按「开发目录 → PyInstaller 解包目录（`sys._MEIPASS`）」查找。

客户端窗口用的是本机已装的 Edge / Chrome 的 `--app` 模式，不需要额外依赖：
窗口用独立 profile（`%LOCALAPPDATA%\AISessionManager\app-profile-<端口>`，不碰日常浏览数据），
页面关闭时发一个 `/api/bye` 信标，3 秒内没有新请求服务就退出；刷新页面不会误关。
机器上没有 Chromium 内核浏览器时退回默认浏览器，长时间没人访问（30 分钟）自动退出。

### 加一个新工具

- **常见格式**：在 `providers/catalog.py` 的 `SPECS` 加一条（root + glob + adapter），
  重新扫描就会出现在「已发现」里。
- **特殊格式**：在 `providers/` 写一个 `Provider` 子类（实现 `sessions()`、`detail()`、
  `delete()`、回收站三件套、`usage_entries()`），加进 `providers/discovery.py` 的
  `PRIMARY`（固定显示）或 `_spec_provider`（按路径发现）。
  左侧菜单、工作台统计、`--tool` 参数会自动带上。

## 样式构建

样式用 Tailwind CSS，但**不在页面里引 CDN**（CSP 只允许本站资源，也为了断网可用）：
`tailwind.css` 编译成 `vendor/tailwind.css` 后随包发布，运行时不需要 node。改完界面后执行：

```bash
cd src/session_manager/webui
npx --yes tailwindcss@3.4.17 -c tailwind.config.js -i tailwind.css -o vendor/tailwind.css --minify
```

Tom Select 的 `.ts-*` 样式必须写在 `@layer` 之外：那些类名是运行时才加到 DOM 上的，
放进 layer 会被 Tailwind 当成未使用的类裁掉。

## 安全

- 删除默认进回收站；只有「永久删除」才真正删掉。固定工具沿用各自的 `session-trash/`，
  自动发现的客户端统一移到 `~/.ai-session-manager/trash/<key>/`；数据库型工具写软删除标记
  （ZCode 的 `time_deleted`、WorkBuddy 的 `deleted_at`），未知结构只读。
- 当前正在运行的 Claude 会话（`CLAUDE_CODE_SESSION_ID`）受保护。
- 批量清理必须先预览，且至少给一个条件。
- 读取是只读操作；删除前都会展示标题、时间、大小。
- 启发式扫描只读取目录结构和会话文件头，不写入对方目录；发现的客户端只有「文件型」
  且确认可删时才允许删除。
- 网页服务只绑定 `127.0.0.1`，校验 Host 与 Origin，拒绝跨站 POST；静态资源只放行
  `/webui/vendor/` 下的白名单文件（不允许 `..`）。
- 数据库只读查询用 `mode=ro` 打开；写操作（软删除 / 恢复）才用读写连接。

## 重新打包 exe

```bash
pip install -r requirements-dev.txt
python -m PyInstaller --noconfirm --clean ClaudeSessionManager.spec
# 产物：dist\AISessionManager.exe（客户端版）+ dist\ClaudeSessionManager.exe（控制台版）
```

spec 里已经把 `src/session_manager/webui` 作为数据目录打进去，并把新增的 provider 模块
写进 `hiddenimports`；两个 exe 共用同一份代码，只是 `console=` 不同。
改完代码或界面重新执行上面这条命令即可。打包时如果旧 exe 正在运行，
Windows 会锁住文件，先关掉再打包。
