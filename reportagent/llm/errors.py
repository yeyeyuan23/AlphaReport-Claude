"""Recognize provider-confirmed context limits without guessing from character counts."""


class ContextWindowExceeded(ValueError):
    def __init__(self, model: str):
        self.model = model
        super().__init__(
            f"模型 {model} 明确返回上下文超限（正文、提示词与输出预算合计）；已跳过，未截断全文。"
        )


def context_window_error(error: Exception, model: str) -> ContextWindowExceeded | None:
    if getattr(error, "status_code", None) not in {400, 413, 422}:
        return None
    body = getattr(error, "body", None)
    details = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(details, dict):
        details = {}
    code = str(getattr(error, "code", None) or details.get("code", "")).lower()
    message = str(details.get("message", str(error))).lower()
    if code in {
        "context_length_exceeded",
        "context_window_exceeded",
        "max_context_length_exceeded",
        "prompt_too_long",
    } or any(
        phrase in message
        for phrase in (
            "maximum context length",
            "exceeds the context window",
            "exceed the context window",
            "prompt is too long",
            "input is too long",
            "input token count exceeds",
        )
    ):
        return ContextWindowExceeded(model)
    return None
