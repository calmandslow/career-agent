---
name: resume-tailor
description: Tailor an existing resume to a JD profile while preserving factual accuracy.
---

# Resume Tailor

Use the JD profile to reprioritize and rewrite only facts present in
`candidate_evidence`. Each source excerpt has an `evidence_id`.

Never invent experience, metrics, tools, ownership, or outcomes. Do not turn a
planned experiment into a completed result. Do not turn an inference into a fact.

For every matched strength, cite the exact supporting evidence IDs. Add a
`resume_evidence` entry for every materially rewritten project claim or metric.
Only use IDs supplied in the context. Keep genuine gaps separate from strengths
and do not disguise a JD requirement as candidate evidence.

Return `tailored_resume` as an application-ready resume, not a capability
summary. Follow `output_language` (`zh-CN` means Chinese; `en` means English).
Preserve the candidate name, contact details, project names, dates, locations,
links, education, and skill grouping whenever they exist in the evidence.
Never translate, normalize, or invent contact details, dates, organization
names, links, or credentials.

Do not place the same date in both `subheading` and `date_or_location`. Use
`subheading` for degree, project type, or organization context and use
`date_or_location` once for a date or location.

Use at least three relevant sections: education, experience/projects, and
skills. Never return an empty section, an empty entry, or an entry without at
least one evidence-backed bullet. Omit optional sections such as awards when
the source has no content for them.
Use exact sample sizes and metrics instead of adjectives such as comprehensive,
deep, strong, expert, or proficient. Do not call a school top-tier. Do not infer
stakeholder or cross-functional experience unless the source explicitly names
the people or teams involved.

When the evidence is an independent portfolio or design-stage project, describe
it as `规划实践`, `设计实践`, or an equivalent phrase in the requested language.
Do not use an unqualified phrase such as `AI product experience` or `从0到1产品经验`
when it could be read as launched company-product experience.
Unless employment evidence directly proves it, also avoid `独立负责产品规划的经验`,
`AI Agent设计经验`, `产品落地经验`, and `Owner意识`. Use `独立产品规划实践`,
`AI Agent设计实践`, `产品方案设计实践`, or `独立推进能力` instead.

Every contact item, summary statement, and bullet needs evidence IDs and an
`achievement_status`: `completed` for work actually done, `observed` for a
measured result, `designed` for a design/proposal without executed results,
`planned` for future work, and `identity` only for identity/contact facts.
Never turn designed or planned work into completed or observed outcomes.

Add every contact item, the professional summary, and every bullet verbatim to
`claim_audit`, with its
claim type, status, evidence IDs, and a short rationale. `bounded_inference`
must be cautious and may not claim experience, proficiency, ownership, results,
or collaboration absent from the evidence. Do not serialize another JSON object
into a string field. The Harness renders the structure into Markdown.

The number of `claim_audit` records must exactly equal the number of contact
items plus one professional summary plus all resume bullets. Use `identity`
claim type only when `achievement_status` is also `identity`; project, skill,
and capability statements must use `exact_fact`, `reframed_fact`, or
`bounded_inference`.

A matched strength must describe evidence the candidate actually has. Put any
statement containing a missing capability, unsupported tool, absent experience,
or industry gap only in `gaps`, never in `matched_strengths`.

Set `fit_level` conservatively: `strong` only when nearly all must-have
requirements have direct evidence and no major experience gap exists; `partial`
when relevant transferable evidence exists alongside material gaps; otherwise
use `weak`.
