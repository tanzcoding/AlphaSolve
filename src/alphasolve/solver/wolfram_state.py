class AlphaSolveConfig:
    WOLFRAM_AVAILABLE = True
    WOLFRAM_STATUS = "not_checked"
    CHECK_IS_THEOREM_TIMES = 5

    @classmethod
    def configure_wolfram_availability(cls, available: bool, reason: str = "") -> None:
        cls.WOLFRAM_AVAILABLE = bool(available)
        cls.WOLFRAM_STATUS = reason or ("available" if available else "unavailable")
