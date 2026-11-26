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
    def kind(self):
        return platform_rejection_kind(self.code)

    @property
    def wait_for(self):
        return platform_recovery_condition(self.code)

    def feedback(self):
        value = {
            "kind": "platform_rejection",
            "rejection_kind": self.kind,
            "code": self.code,
            "message": self.message,
        }
        if self.correctable is not None:
            value["correctable_hint"] = self.correctable
        return value


def platform_rejection_kind(code):
    if code in {-101, -111}:
        return "login"
    if code in {-509, -412, -352, 12014, 12015}:
        return "rate_limit"
    if code == 49044:
        return "gif_not_permitted"
    if code == 49045:
        return "image_not_permitted"
    if code in {-404, 62002, 62004, 62012, 12002, 12006, 12009, 12022, 12061}:
        return "target_unavailable"
    if code in {-102, 12035, 12045, 12046}:
        return "account_restricted"
    if code in {12016, 12025, 12051, 12052}:
        return "text_violation"
    return "other"


def is_media_permission_rejection(code):
    return platform_rejection_kind(code) in {"gif_not_permitted", "image_not_permitted"}


def is_terminal_target_rejection(code):
    return platform_rejection_kind(code) in {"target_unavailable", "account_restricted"}


def platform_recovery_condition(code):
    if code in {-101, -111}:
        return "login"
    if code in {-509, -412, -352, 12014, 12015}:
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


def platform_rejection(payload, *, fallback="平台明确拒绝了本次提交", secret_values=()):
    code = payload["code"]
    raw_message = payload.get("message") or payload.get("msg") or fallback
    message = raw_message.strip() if isinstance(raw_message, str) and raw_message.strip() else fallback
    if any(secret and secret in message for secret in secret_values):
        message = fallback
    if message != fallback:
        display = f"平台拒绝（code {code}）：{message}"
    else:
        display = f"{fallback}（code {code}）"
    return PlatformRejected(code, message, display_message=display)
