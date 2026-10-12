from typing import Final


SKILL_REGISTRY: Final = {
    "jd-analyzer": {
        "intent": "analyze_jd",
        "requires": ["jd"],
        "optional": ["company"],
        "produces": ["jd_profile"],
    },
    "resume-tailor": {
        "intent": "tailor_resume",
        "requires": ["jd_profile", "base_resume"],
        "optional": ["portfolio"],
        "produces": ["tailored_resume", "candidate_role_profile"],
    },
    "interview-research": {
        "intent": "research_interview",
        "requires": ["jd_profile"],
        "optional": ["company", "role"],
        "produces": ["interview_research_profile"],
    },
    "interview-question-generator": {
        "intent": "generate_interview_questions",
        "requires": ["jd_profile", "candidate_role_profile"],
        "optional": ["interview_research_profile"],
        "produces": ["interview_prep_profile"],
    },
    "interview-answer-coach": {
        "intent": "prepare_answer",
        "requires": ["interview_prep_profile"],
        "optional": ["candidate_role_profile", "jd_profile"],
        "produces": ["answer_prep_profile"],
    },
    "mock-interview": {
        "intent": "mock_interview",
        "requires": ["interview_prep_profile"],
        "optional": ["answer_prep_profile"],
        "produces": ["mock_interview_profile"],
    },
    "interview-review": {
        "intent": "review_interview",
        "requires": ["interview_record"],
        "optional": ["jd_profile", "interview_prep_profile"],
        "produces": ["interview_review_profile"],
    },
}
