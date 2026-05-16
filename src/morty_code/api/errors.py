from __future__ import annotations


class ModelProviderError(RuntimeError):
    """模型 provider 调用失败的结构化错误。"""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        detail: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """初始化对象状态。"""
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """处理该方法负责的业务逻辑。"""
        if self.status is None:
            return True
        if self.status in {408, 409, 429}:
            return True
        return self.status >= 500
