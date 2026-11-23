"""Transport-facing publication and context-window outcomes, independent of orchestration."""


class ContextWindowExceeded(ValueError):
    pass


class IncompleteCompaction(ValueError):
    pass


class PlatformRejected(ValueError):
    def __init__(self, code, message, *, display_message=None, correctable=None):
        super().__init__(display_message or message)
        self.code, self.message, self.correctable = code, message, correctable

    @property
    def wait_for(self):
        return platform_recovery_condition(self.code)

    def feedback(self):
        value = {"kind": "platform_rejection", "code": self.code, "message": self.message}
        if self.correctable is not None:
            value["correctable_hint"] = self.correctable
        return value


def platform_recovery_condition(code):
    if code in {-101, -111}:
        return "login"
    if code in {-509, 12014}:
        return "rate_limit"
    return None


class PublishUncertain(ValueError):
    """A final publication may have reached the platform."""


class SendLimitReached(ValueError):
    kind = "rate_limited"

    def __init__(self, message, next_at):
        super().__init__(message)
        self.next_at = next_at


class UnknownPublication(PublishUncertain):
    def __init__(self, message, token=None):
        super().__init__(message)
        self.write_token = token


def platform_rejection(payload, *, fallback="平台明确拒绝了本次提交"):
    code = payload["code"]
    raw_message = payload.get("message") or payload.get("msg") or fallback
    message = raw_message.strip() if isinstance(raw_message, str) and raw_message.strip() else fallback
    if message != fallback:
        display = f"平台拒绝（code {code}）：{message}"
    else:
        display = f"{fallback}（code {code}）"
    return PlatformRejected(code, message, display_message=display)
