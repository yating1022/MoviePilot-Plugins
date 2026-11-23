"""Google Drive 目录树插件的异常类型。

本模块不依赖 MoviePilot 宿主，便于在无宿主环境中单独测试。
"""

from __future__ import annotations


class GDriveTreeError(Exception):
    """目录树抓取/建树的通用错误。"""

    def __init__(self, message: str = "", status_code: int = 0) -> None:
        """
        保存错误信息与可选的 HTTP 状态码。

        :param message: 可读错误
        :param status_code: Google 返回的 HTTP 状态码（0 表示非 HTTP 错误）
        """
        super().__init__(message)
        self.status_code = int(status_code or 0)


class GDriveTreeAuthError(GDriveTreeError):
    """OAuth 配置缺失、令牌刷新失败或授权失效。"""


class GDriveTreeCancelled(GDriveTreeError):
    """刷新被插件停用/重载主动中止。"""


class GDriveTreeBuildError(GDriveTreeError):
    """文件元数据无法构建为合法的目录树。"""
