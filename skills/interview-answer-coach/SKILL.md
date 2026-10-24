---
name: interview-answer-coach
description: Build truthful answer structures for prioritized interview questions.
---

# Interview Answer Coach

Prepare P0 questions first. Use only `candidate_evidence`, `tailored_resume`, and
`resume_claim_audit` as factual proof. `jd_profile` and the question bank explain
what matters but never prove that the candidate has done something.

Use `recruitment_process` only to understand the current stage, interviewer focus,
and unresolved feedback from earlier stages. It changes prioritization and depth,
but is not candidate evidence and must never be cited as proof of experience.

For each question, identify the real evaluation dimension, select the best
evidence IDs, classify evidence as `sufficient`, `partial`, or `insufficient`,
choose a question-appropriate structure, create a factual skeleton, and then a
natural 60–90 second spoken answer. Lead with the conclusion.

If evidence is partial, keep the answer bounded and list the missing facts. If
evidence is insufficient, leave `recommended_answer` empty and list the minimum
clarifications required. Never invent metrics, users, outcomes, employment,
ownership, collaboration, conflict, technical implementation, or product launch.

Prepare realistic follow-ups that stress-test evidence, personal contribution,
trade-offs, results, and remaining uncertainty. Preserve designed/planned versus
completed distinctions. Do not turn a personal design project into company-product
experience. The output becomes the factual input for Mock Interview.

Never invent a delivery or adaptation deadline such as "两周内适应". A time estimate
may appear only when that exact commitment is supported by candidate evidence;
otherwise describe the learning method and validation checkpoints without promising
a duration.

For a hypothetical business scenario, label it as a proposal and begin with discovery:
clarify the business goal, target user, current workflow/pain point, constraints, and
available evidence before suggesting an Agent, feature, or solution. Do not jump from
the prompt directly to a product concept.

For an ABTest design answer, make the reasoning operational rather than textbook-like.
Cover the randomization unit and exposure definition, primary and guardrail metrics,
sample-ratio mismatch (SRM), cross-group contamination, experiment duration/stopping
rule, confidence interval/statistical significance, practical significance, and the
decision that follows each possible result. Mark hypothetical choices as assumptions.

When `repair_instruction` asks for one `PreparedAnswer`, return only that answer. Keep
the source question and supported evidence IDs unchanged and repair only the reported
quality defect.
