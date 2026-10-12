from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, TextIO
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class LLMCallRecord:
    call_id: str
    run_id: str | None
    skill: str
    call_type: str
    model: str
    attempt: int
    status: str
    started_at: str
    duration_seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    error_type: str | None = None
    error_category: str | None = None
    retryable: bool | None = None
    status_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProgressReporter:
    """Small CLI progress surface; it never prints prompts or candidate data."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        stream: TextIO | None = None,
        heartbeat_seconds: float | None = None,
    ) -> None:
        self.enabled = enabled
        self.stream = stream or sys.stdout
        self.heartbeat_seconds = (
            heartbeat_seconds
            if heartbeat_seconds is not None
            else float(os.getenv("PROGRESS_HEARTBEAT_SECONDS", "15"))
        )
        self._lock = threading.Lock()

    def emit(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            print(message, file=self.stream, flush=True)

    @contextmanager
    def heartbeat(self, label: str) -> Iterator[None]:
        if not self.enabled or self.heartbeat_seconds <= 0:
            yield
            return

        stopped = threading.Event()
        started = time.monotonic()

        def report_waiting() -> None:
            while not stopped.wait(self.heartbeat_seconds):
                elapsed = round(time.monotonic() - started)
                self.emit(f"[LLM] {label} still waiting · {elapsed}s")

        thread = threading.Thread(target=report_waiting, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=0.2)


class ObservableCalls:
    """Retry, progress, and usage capture shared by real-model executors."""

    def __init__(self, *, progress: ProgressReporter | None = None) -> None:
        self.progress = progress or ProgressReporter()
        self._call_records: list[dict[str, Any]] = []
        self._run_id: str | None = None

    def set_run_id(self, run_id: str) -> None:
        self._run_id = run_id

    def drain_call_records(self) -> list[dict[str, Any]]:
        records = self._call_records
        self._call_records = []
        return records

    def _call_with_retry(
        self,
        operation,
        *,
        skill_name: str,
        call_type: str,
        model: str,
    ):
        max_retries = max(0, int(os.getenv("LLM_MAX_RETRIES", "2")))
        base_delay = max(0.0, float(os.getenv("LLM_RETRY_BASE_SECONDS", "2")))
        max_delay = max(base_delay, float(os.getenv("LLM_RETRY_MAX_SECONDS", "8")))
        logical_call_id = uuid4().hex

        for attempt_index in range(max_retries + 1):
            attempt = attempt_index + 1
            started_at = utc_now()
            started = time.monotonic()
            self.progress.emit(
                f"[LLM] {skill_name} started · model={model} · attempt {attempt}/{max_retries + 1}"
            )
            try:
                with self.progress.heartbeat(skill_name):
                    response = operation()
                duration = round(time.monotonic() - started, 3)
                input_tokens, output_tokens, total_tokens = extract_usage(response)
                self._call_records.append(
                    LLMCallRecord(
                        call_id=logical_call_id,
                        run_id=self._run_id,
                        skill=skill_name,
                        call_type=call_type,
                        model=model,
                        attempt=attempt,
                        status="completed",
                        started_at=started_at,
                        duration_seconds=duration,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                    ).to_dict()
                )
                token_text = (
                    f" · tokens={total_tokens}"
                    if total_tokens is not None
                    else " · tokens=unavailable"
                )
                self.progress.emit(
                    f"[LLM] {skill_name} completed · {duration:.1f}s{token_text}"
                )
                return response
            except Exception as exc:
                duration = round(time.monotonic() - started, 3)
                status_code = getattr(exc, "status_code", None)
                error_category = classify_error(exc)
                retryable = is_retryable_error(exc)
                self._call_records.append(
                    LLMCallRecord(
                        call_id=logical_call_id,
                        run_id=self._run_id,
                        skill=skill_name,
                        call_type=call_type,
                        model=model,
                        attempt=attempt,
                        status="failed",
                        started_at=started_at,
                        duration_seconds=duration,
                        error_type=type(exc).__name__,
                        error_category=error_category,
                        retryable=retryable,
                        status_code=status_code if isinstance(status_code, int) else None,
                    ).to_dict()
                )
                if not retryable or attempt_index == max_retries:
                    self.progress.emit(friendly_error_message(exc, skill_name=skill_name))
                    raise
                delay = min(max_delay, base_delay * (2**attempt_index))
                self.progress.emit(
                    f"[LLM] {skill_name} {error_category} error · retrying in {delay:g}s · "
                    f"next attempt {attempt + 1}/{max_retries + 1}"
                )
                if delay:
                    time.sleep(delay)

        raise RuntimeError("Unreachable retry state")


def is_retryable_error(exc: Exception) -> bool:
    return classify_error(exc) in {
        "dns",
        "connection",
        "timeout",
        "rate_limit",
        "server",
        "transient_http",
    }


def classify_error(exc: Exception) -> str:
    """Return a safe operational category without persisting provider messages."""
    error_name = type(exc).__name__.lower()
    status_code = getattr(exc, "status_code", None)
    signal = _exception_signal(exc)

    if any(
        marker in signal
        for marker in (
            "allocationquota",
            "arrearage",
            "overdue-payment",
            "free quota",
            "free tier",
            "quota exhausted",
            "insufficient quota",
            "余额不足",
            "额度不足",
            "额度已耗尽",
        )
    ):
        return "quota_exhausted"
    if _has_dns_cause(exc) or "getaddrinfo" in signal or "name resolution" in signal:
        return "dns"
    if "timeout" in error_name or status_code == 408:
        return "timeout"
    if "ratelimit" in error_name or status_code == 429:
        return "rate_limit"
    if "authentication" in error_name or status_code == 401:
        return "authentication"
    if "permission" in error_name or status_code == 403:
        return "permission"
    if "connection" in error_name or "connecterror" in error_name:
        return "connection"
    if "internalserver" in error_name or (
        isinstance(status_code, int) and status_code >= 500
    ):
        return "server"
    if status_code == 409:
        return "transient_http"
    if "badrequest" in error_name or status_code in {400, 404, 405, 422}:
        return "invalid_request"
    return "unknown"


def friendly_error_message(exc: Exception, *, skill_name: str | None = None) -> str:
    category = classify_error(exc)
    prefix = f"[错误] {skill_name}: " if skill_name else "[错误] "
    messages = {
        "dns": "DNS 解析失败；请检查网络、代理或 DNS。State 已保留，可稍后从失败 Skill 继续。",
        "connection": "模型服务连接失败；请检查网络或代理。State 已保留，可稍后继续。",
        "timeout": "模型请求超时；已停止本次尝试。State 已保留，可稍后继续。",
        "rate_limit": "模型服务限流；自动重试次数已用完。请稍后继续。",
        "server": "模型服务暂时异常；自动重试次数已用完。请稍后继续。",
        "transient_http": "模型服务返回临时错误；自动重试次数已用完。请稍后继续。",
        "authentication": "API Key 无效或已过期；请检查 DASHSCOPE_API_KEY。本错误不会自动重试。",
        "permission": "当前账号或 API Key 没有模型访问权限。本错误不会自动重试。",
        "quota_exhausted": (
            "模型免费额度或账户额度已耗尽；本错误不会自动重试。请更换仍有额度的模型、"
            "关闭“仅使用免费额度”或充值后，再使用原 application_id 继续。"
        ),
        "invalid_request": "请求参数或模型配置无效；请检查模型名称和输入。本错误不会自动重试。",
        "unknown": "模型调用失败且无法安全归类；State 已保留，请查看错误类型后再继续。",
    }
    return prefix + messages[category]


def _exception_signal(exc: Exception) -> str:
    values = [str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        values.append(str(body))
    response = getattr(exc, "response", None)
    if response is not None:
        values.append(str(getattr(response, "text", "")))
    return " ".join(values).lower()


def _has_dns_cause(exc: Exception) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__.lower() == "gaierror":
            return True
        current = current.__cause__ or current.__context__
    return False


def extract_usage(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None, None

    def value(*names: str) -> int | None:
        for name in names:
            found = getattr(usage, name, None)
            if isinstance(found, int):
                return found
            if isinstance(usage, dict) and isinstance(usage.get(name), int):
                return usage[name]
        return None

    input_tokens = value("prompt_tokens", "input_tokens")
    output_tokens = value("completion_tokens", "output_tokens")
    total_tokens = value("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens
