"""Transport-facing publication outcomes, independent of model orchestration."""


class PlatformRejected(ValueError):
    def __init__(self, code, message, *, display_message=None, correctable=None):
        super().__init__(display_message or message)
        self.code, self.message, self.correctable = code, message, correctable

    def feedback(self):
        value = {"kind": "platform_rejection", "code": self.code, "message": self.message}
        if self.correctable is not None:
            value["correctable_hint"] = self.correctable
        return value


class PublishUncertain(ValueError):
    """A final publication may have reached the platform."""


class UnknownPublication(PublishUncertain):
    def __init__(self, message, token=None):
        super().__init__(message)
        self.write_token = token


def platform_rejection(payload, *, fallback="平台明确拒绝了本次提交"):
    message = payload.get("message") or payload.get("msg") or fallback
    message = message if isinstance(message, str) else fallback
    return PlatformRejected(payload["code"], message, display_message=fallback)
