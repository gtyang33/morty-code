class AutoCompactDecider:
    """控制自动 compact 阈值，并在连续失败后熔断。"""

    def __init__(self, token_threshold: int, max_failures: int = 2) -> None:
        """初始化对象状态。"""
        self.token_threshold = token_threshold
        self.max_failures = max_failures
        self.failure_count = 0

    def should_compact(self, total_tokens: int) -> bool:
        """判断是否需要执行后续动作。"""
        return self.failure_count < self.max_failures and total_tokens > self.token_threshold

    def record_success(self) -> None:
        """记录运行状态或诊断事件。"""
        self.failure_count = 0

    def record_failure(self) -> None:
        """记录运行状态或诊断事件。"""
        self.failure_count += 1
