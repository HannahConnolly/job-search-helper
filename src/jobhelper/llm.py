"""Claude: score jobs against your profile, write prep packets, and review your documents."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

# Server-side refusal fallback: a declined request is re-run on another model inside the same
# call. Claude Haiku 5.5 has no fallback model, so it gets neither the parameter nor the beta.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

# USD per million tokens: input, output, cache read. Cache writes bill at 1.25x input.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

SCORE_INSTRUCTIONS = """\
You screen job postings for one job seeker. Their resume, interview notes and preferences follow; \
each request contains one posting. Decide how worthwhile it is for this person to apply, reading \
the posting the way its hiring manager would.

Score from 0 to 100:
- 85-100: the day-to-day work and stack match what they have done, the level fits, and nothing \
blocks them. Apply today.
- 70-84: a solid fit with gaps that a cover letter or interview can address.
- 50-69: a stretch; the level, stack or domain is a real reach.
- 0-49: a different discipline, far above their level, or blocked.

Weigh, in this order: whether the core work matches their real experience and what they want to \
do next; level, comparing the years and scope the posting asks for with their experience (work it \
out from the resume dates); stack and domain overlap; their stated preferences. Requirements they \
plainly can't meet, such as location, on-site rules or years of experience far beyond theirs, are \
dealbreakers and cap the score at 40.

Cite specific evidence from the posting and from their documents. Don't credit skills the resume \
doesn't show."""

PREP_INSTRUCTIONS = """\
You help one job seeker apply for a specific role. Their resume, interview notes (drafted answers \
to behavioral questions, some unfinished) and preferences follow. Each request contains a job \
posting and, when available, screening notes.

Write a prep packet in Markdown with exactly these sections:

## Snapshot
Three bullets: the strongest reason they fit, the biggest gap, and how to address that gap honestly.

## Resume tailoring
- A one or two sentence summary for the top of the resume, aimed at this role.
- The four to six bullets to feature, rewritten to mirror the posting's language. Show each as \
**Original:** and **Tailored:**.
- Keywords from the posting they can legitimately claim, and any they shouldn't.

## Cover letter
Under 250 words, plain text, specific to this company and role, in their voice. Avoid stock \
phrases such as "I am excited to apply" or "fast-paced environment".

## Interview prep
- **Why this company?** Their existing answer, adapted to this company and role.
- **Likely questions:** the five to seven behavioral or experience questions this role is most \
likely to raise. For each, name the story from their notes to use and give a STAR outline (one \
line each for Situation, Task, Action, Result) angled toward what this team cares about. When no \
story fits, say so and point to the resume experience that could become one.
- **Technical topics to review**, from the posting's requirements set against their background.
- **Questions to ask them:** three or four specific to this role; reuse theirs where they fit.

## Outreach note
A LinkedIn connection note to the recruiter or hiring manager, under 300 characters.

Use only facts from their resume and notes: never invent employers, titles, dates, numbers, tools \
or outcomes. Where a stronger version would need a detail the documents don't contain, write a \
placeholder such as [add: team size]. Write anything they will say or send in the first person."""

REVIEW_INSTRUCTIONS = """\
You are an experienced technical recruiter and interview coach. One job seeker's resume, interview \
notes (behavioral questions with drafted answers, rough notes and unanswered questions) and \
preferences follow.

Write a review in Markdown with exactly these sections:

## Resume: top fixes
At most ten changes, most valuable first. Quote the current text and give the replacement. Look \
for typos and broken links or handles, inconsistent tense and formatting, weak or repeated verbs, \
and bullets without scope or results. Address how the timeline reads, including any gap since the \
most recent role, and the most honest and effective way to present it.

## Consistency check
Places where the resume and the answers describe the same work differently (numbers, scope, \
technologies, dates). For each, say which version to standardize on, or flag it for them to verify.

## Story bank
The five to seven distinct stories their materials contain. For each: a short name, the \
competencies it demonstrates (for example ownership, conflict, failure, ambiguity, technical \
depth) and the questions it can answer.

## Answer by answer
Every question in the document, in order:
- Answered: a verdict (Ready, Tighten or Rework) with one line on why, then a tightened version \
in STAR form that takes 60 to 90 seconds to say (about 150 to 220 words), in the first person.
- Unanswered: the story to use and a STAR outline, with placeholders for details only they know.

Use only facts from their documents: never invent employers, numbers, tools or outcomes; write \
[add: ...] for missing specifics. Keep their voice: plain, direct and specific."""


RESUME_INSTRUCTIONS = """\
You rewrite one job seeker's resume. Their resume, interview notes and preferences follow; a \
request may include a job posting to tailor the resume to.

Return the complete rewritten resume in Markdown, ready to paste into a document, followed by a \
section `## What changed` with one bullet per notable change and why. Don't wrap it in a code block.

- Keep their sections, employers, titles, dates, projects and education, and keep it to one page.
- Fix typos, broken links, inconsistent formatting and repeated verbs. Lead each bullet with a \
strong verb and show scope or results.
- Use only facts from their documents; the interview notes may supply details the resume leaves \
out. Never invent numbers, tools or outcomes. Where a number would strengthen a bullet but isn't \
in the documents, write a placeholder such as [add: number of services].
- When tailoring to a posting, reorder bullets and skills and mirror its language only where that \
is true of their experience."""


class LLMError(Exception):
    pass


class FitAssessment(BaseModel):
    score: int = Field(description="0-100, using the rubric")
    summary: str = Field(description="One sentence explaining the score")
    strengths: list[str] = Field(
        description="Up to 4 concrete overlaps between the posting and the candidate's actual experience"
    )
    gaps: list[str] = Field(description="Up to 4 requirements the candidate doesn't clearly meet")
    dealbreakers: list[str] = Field(
        description="Requirements they plainly can't meet (location, on-site rules, far more experience); "
        "empty if none"
    )
    years_required: int | None = Field(
        description="Minimum years of experience the posting asks for, or null if it doesn't say"
    )


@dataclass
class Usage:
    """Token totals across calls, with a cost estimate from list prices."""

    calls: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, model: str, usage) -> None:
        written = usage.cache_creation_input_tokens or 0
        read = usage.cache_read_input_tokens or 0
        price_in, price_out, price_read = PRICES.get(model, (0.0, 0.0, 0.0))
        cost = (
            usage.input_tokens * price_in
            + written * price_in * 1.25
            + read * price_read
            + usage.output_tokens * price_out
        ) / 1e6
        with self._lock:
            self.calls += 1
            self.input_tokens += usage.input_tokens
            self.cache_write_tokens += written
            self.cache_read_tokens += read
            self.output_tokens += usage.output_tokens
            self.cost += cost

    def summary(self) -> str:
        total_in = self.input_tokens + self.cache_write_tokens + self.cache_read_tokens
        cost = f" · ≈${self.cost:.2f}" if self.cost else ""
        return (
            f"{self.calls} Claude call(s) · {total_in:,} input tokens ({self.cache_read_tokens:,} from cache)"
            f" · {self.output_tokens:,} output tokens{cost}"
        )


def make_client() -> anthropic.Anthropic:
    configured = any(
        os.environ.get(name)
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID")
    )
    if not configured and not (Path.home() / ".config" / "anthropic").is_dir():
        raise LLMError("No Anthropic credentials. Put ANTHROPIC_API_KEY=... in .env (see .env.example).")
    return anthropic.Anthropic(max_retries=4)


def _extras(model: str) -> dict:
    return {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model in FALLBACK_MODELS else {}


def _system(instructions: str, profile_block: str) -> list[dict]:
    # Identical on every call, so it's written to the prompt cache once and read after that.
    return [{"type": "text", "text": f"{instructions}\n\n{profile_block}", "cache_control": {"type": "ephemeral"}}]


def _check(message) -> None:
    if message.stop_reason == "refusal":
        raise LLMError("Claude declined this request")
    if message.stop_reason == "max_tokens":
        raise LLMError("Claude's reply hit max_tokens before it finished")


def _final_text(message) -> str:
    """The reply's text. After a server-side fallback, only the model that finished counts."""
    blocks = list(message.content)
    switches = [i for i, block in enumerate(blocks) if block.type == "fallback"]
    if switches:
        blocks = blocks[switches[-1] + 1 :]
    return "".join(block.text for block in blocks if block.type == "text").strip()


def score_job(
    client: anthropic.Anthropic, model: str, effort: str, profile_block: str, job_block: str, usage: Usage
) -> FitAssessment:
    response = client.beta.messages.parse(
        model=model,
        max_tokens=4096,
        system=_system(SCORE_INSTRUCTIONS, profile_block),
        messages=[{"role": "user", "content": job_block}],
        output_format=FitAssessment,
        output_config={"effort": effort},
        **_extras(model),
    )
    usage.add(model, response.usage)
    _check(response)
    if response.parsed_output is None:
        raise LLMError(f"No structured output (stop_reason={response.stop_reason})")
    assessment = response.parsed_output
    assessment.score = max(0, min(100, assessment.score))
    return assessment


def _write(
    client: anthropic.Anthropic, model: str, effort: str, instructions: str, profile_block: str, request: str, usage: Usage
) -> str:
    # Long outputs: stream so the request can't hit an HTTP timeout.
    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        system=_system(instructions, profile_block),
        messages=[{"role": "user", "content": request}],
        output_config={"effort": effort},
        **_extras(model),
    ) as stream:
        message = stream.get_final_message()
    usage.add(model, message.usage)
    _check(message)
    return _final_text(message)


def write_prep(
    client: anthropic.Anthropic, model: str, effort: str, profile_block: str, job_request: str, usage: Usage
) -> str:
    return _write(client, model, effort, PREP_INSTRUCTIONS, profile_block, job_request, usage)


def write_resume(
    client: anthropic.Anthropic, model: str, effort: str, profile_block: str, request: str, usage: Usage
) -> str:
    return _write(client, model, effort, RESUME_INSTRUCTIONS, profile_block, request, usage)


def write_review(client: anthropic.Anthropic, model: str, effort: str, profile_block: str, usage: Usage) -> str:
    return _write(
        client, model, effort, REVIEW_INSTRUCTIONS, profile_block, "Review my resume and interview notes.", usage
    )
