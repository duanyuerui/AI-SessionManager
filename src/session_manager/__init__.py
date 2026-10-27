"""ClaudeSessionManager — Claude Code 会话历史管理。

包内三个模块：
    core    扫描 / 查看 / 删除 / 回收站 / 批量清理 / 统计 + 命令行入口
    server  本地 HTTP 服务（网页界面）
    webui   前端页面与第三方静态资源（Bootstrap、Tom Select）
"""

__version__ = "1.1.0"
__all__ = ["core", "server"]
