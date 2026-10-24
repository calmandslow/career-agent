---
name: interview-question-generator
description: Generate prioritized interview questions from role, candidate, and research profiles.
---

# Interview Question Generator

Generate P0, P1, and P2 questions grounded in the JD profile, candidate evidence,
gaps, and any available interview research. Include why each question is likely,
its evidence basis, and realistic follow-ups. Give priority to role-critical and
high-risk resume questions.

When `recruitment_process` is available, adapt the bank to its current stage instead
of treating the round number as a difficulty level. Use the stage type, interviewer
role, focus areas, prior-stage feedback, and unresolved risks. A recruiter round,
case exercise, hiring-manager round, panel, and executive round test different
dimensions even when they share the same sequence number. Prioritize questions that
probe unresolved risks from earlier stages, while keeping unknown stage details
explicit rather than inventing a company process.

Return 8 to 10 non-duplicative questions in total. Prefer 4 P0, 3 P1, and 1 to
3 P2 questions. Keep each main question focused; move secondary challenges into
follow-ups instead of creating an overly long compound question.
