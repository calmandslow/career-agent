from __future__ import annotations

from career_agent.model_router import CORE_SKILLS, FAST_SKILLS, ModelRouter


def main() -> None:
    router = ModelRouter.from_qwen_env()
    print("Qwen model routes:")
    print(f"- answer-semantic-verifier: {router.model_for('answer-semantic-verifier')} [review]")
    for skill in sorted(FAST_SKILLS):
        print(f"- {skill}: {router.model_for(skill)} [fast]")
    for skill in sorted(CORE_SKILLS):
        print(f"- {skill}: {router.model_for(skill)} [core]")


if __name__ == "__main__":
    main()
