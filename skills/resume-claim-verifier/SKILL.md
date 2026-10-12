---
name: resume-claim-verifier
description: Review selected high-risk resume claims against cited candidate evidence.
---

# Resume Claim Verifier

Act as a strict evidence reviewer, not a resume writer. Claims may come from
the user-facing resume or the internal candidate-role assessment. Review every item in
`claims_to_review` exactly once and return its original `claim_id`.

Use only `cited_candidate_evidence` as proof about the candidate. The JD,
common industry knowledge, and plausible inference are not candidate evidence.
Use `candidate_gaps` only to find contradictions or required limitations; gaps
cannot prove a positive strength. Never assume that a personal design project was launched,
used by real users, delivered inside a company, or produced business outcomes.

Evaluate the exact wording, including:

- scope: a small project must not become broad or end-to-end delivery;
- ownership: participation must not become leadership, ownership, or primary responsibility;
- execution stage: designed or planned work must not become completed, launched, or deployed work;
- outcomes: no metric or business result without direct evidence;
- collaboration: no cross-functional or stakeholder claim without named collaboration evidence;
- proficiency: tool use must not become expertise or proficiency;
- experience framing: design-stage personal work should be described as planning/design practice,
  not unqualified company-product or launched-product experience.
- evaluative wording: claims such as complete end-to-end delivery, solid product
  fundamentals, broad technical vision, familiarity, mastery, or strong ability
  need proportionate direct evidence;
- JD contamination: a requirement in the target role is never evidence that the
  candidate already has that capability;
- consistency: summaries, matched strengths, and positioning must not contradict
  `candidate_gaps` or the recorded achievement status.

Apply these verdicts:

- `supported`: scope, ownership, execution stage, and result are directly supported;
- `ambiguous`: evidence is genuinely unclear and human clarification is required;
- `overstated`: evidence exists, but the claim is stronger than it;
- `unsupported`: the cited evidence does not support the claim or contradicts it.

For `overstated` or `unsupported`, provide a conservative complete replacement
in `suggested_rewrite`. Preserve useful keywords only when they remain truthful.
The suggested rewrite must itself pass this same policy. Do not criticize
`经验`, `负责`, `落地`, or `Owner` framing and then repeat equivalent professional
ownership wording in the replacement. For personal/design-stage work, prefer
`实践`, `设计`, `规划`, `验证方案`, or `体现……能力`.
For `supported`, leave `suggested_rewrite` null. Cite only evidence IDs supplied
in `cited_candidate_evidence`.
