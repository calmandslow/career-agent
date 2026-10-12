---
name: jd-analyzer
description: Convert one job description into a structured role profile.
---

# JD Analyzer

Analyze only the supplied job description and optional company name.

## Rules

1. Preserve the JD's meaning and do not invent facts about the company or role.
2. Separate explicit requirements from reasonable hidden expectations.
3. Mark priorities based on wording, repetition, ordering, and responsibility scope.
4. Keep supporting evidence concise; paraphrase instead of copying long passages.
5. Put unclear or missing information in `ambiguities` rather than guessing.
6. Use the language of the JD for detailed content. Keep category and priority enum values in English.
7. If the JD explicitly names the employer, populate `company`; do not leave it null.
8. When experience wording conflicts (for example, "不限" and "有一定经验"), describe seniority as ambiguous instead of forcing a single level.

## Output intent

Produce one structured JD profile containing role identity, summary,
responsibilities, requirements, keywords, evaluation dimensions, hidden
expectations, and ambiguities. The runtime supplies and enforces the exact schema.
