from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from .models import ApplicationState


DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[1] / "config" / "model_pricing.json"


def write_run_reports(
    output_directory: str | Path,
    state: ApplicationState,
    run_result: dict[str, Any],
    *,
    run_id: str | None = None,
) -> dict[str, Path]:
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    pricing = load_pricing()
    calls = [
        item for item in state.llm_calls if run_id is None or item.get("run_id") == run_id
    ]
    summary = summarize_calls(calls, pricing)
    safe_run_result = json.loads(json.dumps(run_result, ensure_ascii=False))
    pending = safe_run_result.get("pending_question")
    if isinstance(pending, dict) and pending.get("reason"):
        pending["reason"] = "[redacted; see pending_human_review.json]"
    report = {
        "version": "1.8.4",
        "application_id": state.application_id,
        "run_id": run_id,
        "run_result": safe_run_result,
        "current_stage": state.current_stage,
        "summary": summary,
        "calls": calls,
        "pricing": {
            "currency": pricing.get("currency", "CNY"),
            "basis": pricing.get("basis"),
            "note": pricing.get("note"),
        },
    }
    run_report = directory / "run_report.json"
    run_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    execution_log = directory / "execution_log.jsonl"
    with execution_log.open("w", encoding="utf-8") as stream:
        for event in state.execution_history:
            safe_detail = {
                key: ("[redacted]" if key in ("error", "previous_answer", "review", "issues") else value)
                for key, value in event.detail.items()
            }
            stream.write(
                json.dumps(
                    {
                        "record_type": "state_event",
                        "event": event.event,
                        "detail": safe_detail,
                        "created_at": event.created_at,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        for call in state.llm_calls:
            stream.write(
                json.dumps(
                    {"record_type": "llm_call", **call},
                    ensure_ascii=False,
                )
                + "\n"
            )

    cost_report = directory / "cost_report.md"
    cost_report.write_text(render_cost_report(summary, pricing), encoding="utf-8")
    return {
        "run_report": run_report,
        "execution_log": execution_log,
        "cost_report": cost_report,
    }


def load_pricing(path: str | Path | None = None) -> dict[str, Any]:
    selected = Path(path or os.getenv("MODEL_PRICING_PATH") or DEFAULT_PRICING_PATH)
    if not selected.exists():
        return {"currency": "CNY", "models": {}, "note": "Pricing file not found."}
    return json.loads(selected.read_text(encoding="utf-8"))


def summarize_calls(calls: list[dict[str, Any]], pricing: dict[str, Any]) -> dict[str, Any]:
    completed = [item for item in calls if item.get("status") == "completed"]
    failed = [item for item in calls if item.get("status") == "failed"]
    retries = sum(1 for item in calls if int(item.get("attempt", 1)) > 1)
    input_tokens = sum(int(item.get("input_tokens") or 0) for item in completed)
    output_tokens = sum(int(item.get("output_tokens") or 0) for item in completed)
    total_tokens = sum(int(item.get("total_tokens") or 0) for item in completed)
    duration = round(sum(float(item.get("duration_seconds") or 0) for item in calls), 3)
    unknown_usage_calls = sum(
        1 for item in completed if item.get("total_tokens") is None
    )

    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "completed_calls": 0,
            "failed_attempts": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_cny": 0.0,
            "unpriced_calls": 0,
        }
    )
    total_cost = 0.0
    unpriced_calls = 0
    for item in calls:
        model = str(item.get("model") or "unknown")
        bucket = by_model[model]
        if item.get("status") == "failed":
            bucket["failed_attempts"] += 1
            continue
        bucket["completed_calls"] += 1
        bucket["input_tokens"] += int(item.get("input_tokens") or 0)
        bucket["output_tokens"] += int(item.get("output_tokens") or 0)
        bucket["total_tokens"] += int(item.get("total_tokens") or 0)
        cost = estimate_call_cost(item, pricing)
        if cost is None:
            bucket["unpriced_calls"] += 1
            unpriced_calls += 1
        else:
            bucket["estimated_cost_cny"] += cost
            total_cost += cost

    for bucket in by_model.values():
        bucket["estimated_cost_cny"] = round(bucket["estimated_cost_cny"], 6)

    return {
        "attempts": len(calls),
        "completed_calls": len(completed),
        "failed_attempts": len(failed),
        "retries": retries,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "unknown_usage_calls": unknown_usage_calls,
        "duration_seconds": duration,
        "estimated_cost_cny": round(total_cost, 6),
        "unpriced_calls": unpriced_calls,
        "by_model": dict(by_model),
    }


def estimate_call_cost(call: dict[str, Any], pricing: dict[str, Any]) -> float | None:
    input_tokens = call.get("input_tokens")
    output_tokens = call.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    model_config = pricing.get("models", {}).get(call.get("model"))
    if not model_config:
        return None
    tiers = model_config.get("tiers", [])
    tier = next(
        (
            item
            for item in tiers
            if input_tokens <= int(item.get("max_input_tokens", 0))
        ),
        None,
    )
    if tier is None:
        return None
    return (
        input_tokens * float(tier["input_cny_per_m_tokens"])
        + output_tokens * float(tier["output_cny_per_m_tokens"])
    ) / 1_000_000


def render_cost_report(summary: dict[str, Any], pricing: dict[str, Any]) -> str:
    lines = [
        "# Career Agent 成本与运行报告",
        "",
        f"- API 尝试次数：{summary['attempts']}",
        f"- 成功调用：{summary['completed_calls']}",
        f"- 失败尝试：{summary['failed_attempts']}",
        f"- 重试次数：{summary['retries']}",
        f"- 输入 Token：{summary['input_tokens']}",
        f"- 输出 Token：{summary['output_tokens']}",
        f"- 总 Token：{summary['total_tokens']}",
        f"- API 累计等待时间：{summary['duration_seconds']:.3f} 秒",
        f"- 目录价估算：¥{summary['estimated_cost_cny']:.6f}",
        "",
        "## 按模型统计",
        "",
        "| 模型 | 成功调用 | 失败尝试 | 输入 Token | 输出 Token | 估算费用 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, values in sorted(summary["by_model"].items()):
        lines.append(
            f"| {model} | {values['completed_calls']} | {values['failed_attempts']} | "
            f"{values['input_tokens']} | {values['output_tokens']} | "
            f"¥{values['estimated_cost_cny']:.6f} |"
        )
    if not summary["by_model"]:
        lines.append("| 无真实模型调用 | 0 | 0 | 0 | 0 | ¥0.000000 |")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            f"- {pricing.get('basis', '费用根据本地价格配置估算。')}",
            f"- {pricing.get('note', '实际账单以模型服务控制台为准。')}",
            "- 免费额度、限时折扣、缓存折扣和 Token Plan 不会自动从估算中扣除。",
            "- 日志不保存 Prompt、简历正文、作品集正文或 API Key。",
        ]
    )
    if summary["unknown_usage_calls"]:
        lines.append(
            f"- 有 {summary['unknown_usage_calls']} 次成功调用未返回 Token usage，费用可能被低估。"
        )
    if summary["unpriced_calls"]:
        lines.append(
            f"- 有 {summary['unpriced_calls']} 次调用没有匹配价格配置，未计入费用。"
        )
    return "\n".join(lines) + "\n"
