# ClaudeSessionManager — Claude Code 会话历史管理

管理 `~/.claude/projects/**/*.jsonl` 会话记录的本地工具：列出、搜索、删除（默认进回收站）、
批量清理、回收站恢复、占用统计，并带一个本地网页界面（Tailwind CSS + 可搜索的项目下拉）。

零第三方依赖，只用 Python 3.8+ 标准库；也可以直接双击 `dist\ClaudeSessionManager.exe`，不用装 Python。

## 运行

```bash
python run.py                   # 起网页界面并自动打开浏览器（默认 http://127.0.0.1:8765）
python run.py --port 8899       # 换端口（被占用会自动往后找）
python run.py --no-browser      # 只起服务
```

装成包之后也可以用命令入口：

```bash
pip install -e .
python -m session_manager web           # 网页界面（等同 python run.py）
python -m session_manager list          # 命令行模式
```

`dist\ClaudeSessionManager.exe` 与 `python run.py` 一致，双击即用。服务只监听 `127.0.0.1`，关掉窗口即退出。

## 命令行

```bash
python -m session_manager list            # 列出会话
python -m session_manager show <id>       # 会话详情
python -m session_manager del <id>        # 删除（默认移入回收站）
python -m session_manager trash           # 看回收站
python -m session_manager restore 1 3     # 恢复
python -m session_manager clean --older-than 30d --dry-run
python -m session_manager stats
python -m session_manager web             # 网页界面
```

完整用法见 `SKILL.md`。

## 目录结构

```
ClaudeSessionManager/
├── run.py                      # 开发入口（exe 也从这里启动）
├── ClaudeSessionManager.spec   # PyInstaller 打包配置
├── pyproject.toml              # src 布局 + 命令入口
├── requirements-dev.txt        # 仅打包需要的 pyinstaller
├── src/session_manager/        # 源码
│   ├── __init__.py
│   ├── __main__.py             # python -m session_manager
│   ├── core.py                 # 核心：扫描 / 删除 / 回收站 / 恢复 / 批量清理 / 统计 + CLI
│   ├── server.py               # 本地 HTTP 服务，接口复用 core
│   └── webui/
│       ├── index.html          # 单页界面（Tailwind 工具类 + 自己的脚本）
│       ├── tailwind.css        # 源码样式：设计变量 + 组件层（编译输入）
│       ├── tailwind.config.js  # Tailwind 配置：色板 / 字体 / 动画
│       └── vendor/             # 编译好的 tailwind.css + Tom Select（本地内置，离线可用）
└── dist/
    └── ClaudeSessionManager.exe  # 打包产物（PyInstaller 默认输出目录）
```

界面资源在运行时按「开发目录 → PyInstaller 解包目录（`sys._MEIPASS`）」查找，
所以同一份代码既能直接跑、也能装成包、也能打包成 exe。

## 界面

四个标签：

- **会话** — 搜索标题/项目/会话 ID，按项目筛选（**可输入关键字搜索**的下拉框）、只看无标题；
  **点表头即可按该列排序**（最后活动 / 大小 / 行数 / 项目 / 标题，再点一次反向，箭头在表头上），
  右上角下拉也同步显示当前排序字段；勾选多个后底部浮动条「批量删除」，或顶部按钮；点标题看详情（消息数、成本、时长、恢复命令）。
- **回收站** — 逐个恢复、清空。
- **批量清理** — 按天数 / 大小 / 每项目保留数预览，确认后再执行。
- **统计** — 各项目占用条形图。

右上角主题按钮在「跟随系统 → 浅色 → 深色」之间切换，选择记在 localStorage。
删除前弹确认框，永久删除还要额外勾选确认。

样式用 Tailwind CSS，但**不在页面里引 CDN**（页面 CSP 只允许加载本站资源，也为了断网可用）：
`tailwind.css` 编译成 `vendor/tailwind.css` 后随包发布，运行时不需要 node。改完 `index.html`、
`tailwind.css` 或 `tailwind.config.js` 后执行：

```bash
cd src/session_manager/webui
npx --yes tailwindcss@3.4.17 -c tailwind.config.js -i tailwind.css -o vendor/tailwind.css --minify
```

注意 Tom Select 的 `.ts-*` 样式必须写在 `@layer` 之外：那些类名是运行时才加到 DOM 上的，
放进 layer 会被 Tailwind 当成未使用的类裁掉。

## 安全

- 删除默认移入 `~/.claude/session-trash/`，可 `restore` 恢复；只有「永久删除」才真正删掉。
- 当前正在运行的会话（`CLAUDE_CODE_SESSION_ID`）受保护。
- 批量清理必须先预览，且至少给一个条件。
- 网页服务只绑定 `127.0.0.1`，校验 Host 与 Origin，拒绝跨站 POST；静态资源只放行
  `/webui/vendor/` 下的白名单文件（不允许 `..`）。

## 重新打包 exe

```bash
pip install -r requirements-dev.txt
python -m PyInstaller --noconfirm --clean ClaudeSessionManager.spec
# 产物：dist\ClaudeSessionManager.exe
```

spec 里已经把 `src/session_manager/webui` 作为数据目录打进去（包内路径 `webui/`），
改了界面或源码重新执行上面这条命令即可，不用再改命令行参数。`build/`、`dist/` 是
PyInstaller 的工作目录和输出目录，已在 `.gitignore` 里排除，随时可以删掉重来。
