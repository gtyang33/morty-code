class AutoCompactDecider:
    """第一阶段只做阈值判断。"""

    def __init__(self, token_threshold: int) -> None:
        self.token_threshold = token_threshold

    def should_compact(self, total_tokens: int) -> bool:
        return total_tokens > self.token_threshold
