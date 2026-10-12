"""Question intent and shared generation policy for Answer Coach."""
from pathlib import Path


def infer_answer_mode(question: str) -> str:
    future = any(x in question.lower() for x in ("假设", "如果", "你会如何", "你会怎么", "如何设计", "迁移", "计划如何"))
    past = any(x in question.lower() for x in ("你曾", "你过去", "你的经历", "你在项目中", "你独立设计", "你是如何", "你如何从", "结合你的经历"))
    if future and past:
        return "mixed"
    return "hypothetical" if future else "experience"


def answer_policy_instructions(skill_name: str) -> str:
    if skill_name != "interview-answer-coach":
        return ""
    path = Path(__file__).resolve().parents[1] / "config" / "answer_coach_policy.md"
    return "\n\nANSWER COACH POLICY (overrides conflicting generic evidence-insufficient rules):\n" + path.read_text(encoding="utf-8")
