# casecraft — architecture

## The constraint everything follows from

**The candidate must never see the question.** Reading a case off a screen isn't
a case interview. That single requirement forces the whole design:

1. MCP tool results **render in the chat transcript** — Claude's own docs:
   *"Connector queries appear in the conversation as expandable code steps."*
2. ⇒ The question text can never be a tool return value.
3. ⇒ It has to reach the candidate through a channel MCP doesn't carry: **audio**.
4. MCP has **no host-audio API** — a remote connector is a network peer with
   zero device access.
5. ⇒ The server runs **locally**, and the audio happens in a **browser tab** on
   the candidate's own machine.

So MCP carries control signals only. Nothing that could spoil the case ever
crosses it.

```
Claude ──MCP(stdio)──► casecraft ──SSE──► browser tab ──► 🔊 speaks the case
                            ▲                        └───► 🎤 hears the answer
                            └──────POST /answer───────────┘
```

The case prompt travels left-to-right only. It is spoken, then discarded. What
comes back is the candidate's own words, which are safe to show.

## Why a browser tab rather than server-side audio

The first design put TTS and STT in the Python process (Kokoro + Whisper) and
shipped as an MCPB bundle. The browser won on ease of use, which was the
overriding requirement:

| | server-side audio | browser tab |
|---|---|---|
| Install | ~500MB of models before first case | nothing |
| TTS | Kokoro, natural | Web Speech API, decent |
| Speed control | `speed` param | `rate` multiplier — **exact** |
| Exhibits | needs a window anyway | already have one |
| "Answer done" | audio-only VAD guessing | **a real button** |

The last two decided it. Exhibits are charts — a real interviewer slides paper
across the table, and that needs a surface. And once you have a surface, the
turn-taking problem I flagged as the hardest open question simply evaporates:
there's a button, plus a spacebar shortcut, plus silence detection as backup.

On-device Whisper is still supported and auto-detected — `pip install -e
".[local-stt]"` and the room switches to MediaRecorder + local transcription, so
audio never leaves the machine. It's opt-in rather than required.

## Information gating is structural, not prompted

"Don't reveal unasked data" as a system-prompt instruction leaks. Here it can't,
because the model is never given the thing:

| What Claude receives | What it never receives |
|---|---|
| Clarification *topics* ("the competitive landscape") | The responses, until asked |
| Question type, difficulty, time target | The question text — ever |
| Rubric component *labels*, after an answer is committed | Weights, `must_have` flags |
| The verdict | The model answer, until after grading |

`ask_case_prompt` returns `{spoken: true, seconds: 14}`. A tool whose entire
payload is a side effect on the speakers, and whose return value is deliberately
almost empty, because the return value is the leak surface.

## Grading splits across the process boundary

```
math:        transcript ──► grade_numeric() ──► verdict
                            deterministic · no model · no network · instant

framework:   1. candidate answers            ← transcript LOCKED
             2. server returns labels + transcript to Claude
             3. Claude replies which ids were covered   ← semantic matching only
             4. grade_buckets() applies policy          ← judgement stays in code
```

Step 1 before step 2 is what makes releasing the rubric safe — the answer is
already committed, so it's feedback, not a hint.

Claude is a **matcher**, never the judge. The pass/probe policy lives in
`scoring.py`, so the verdict is deterministic, testable, and identical for every
candidate. Three calls are baked in there and tunable at the top of the function:
`must_have` is near-absolute (missing one caps you at PARTIAL), bonus weight is
excluded from the denominator, and PASS sits at 0.75 — deliberately not lower,
because a tool that rubber-stamps vague answers trains the exact habit that gets
people dinged.

## What's scored

Four dimensions — the same four on every firm's feedback form. 3 is the bar, and
the scorecard reports `limiting_factor` rather than an average because real
interviewers decide on the weakest box: one 2 sinks a candidate with three 4s.

Content lands on the question's primary dimension; **delivery always lands on
communication**. That mirrors a real form, where you can nail the analysis and
still be dinged for how it came out.

`delivery.py` computes hedging, signposting, answer-first, "so what", and pace
straight from the transcript and the clock — no model, no cost. This is the
feedback candidates never get, because a human coach can't count your hedges
while also running the case.

One honest limitation, documented in the module: Whisper and the Web Speech API
both clean up disfluencies, so "um" and "uh" largely don't survive
transcription. Filler counts are a floor. Hedging is real words and survives
intact, which is why the scoring leans on it instead.

## Questions are first-class

A case is a container; the **question** is the practiceable unit. That's what
makes "only hard math" a `WHERE` clause instead of a document search, and it's
the schema decision everything else depends on. Each question carries standalone
`context` so it still makes sense pulled out of case order.

Weakness targeting falls out of the attempt log: mastery per type and per tag,
recency-weighted with a ~30-day half-life, feeding a weighted sampler with a
floor so practice never collapses onto one skill. `error_id` is the column that
matters — scores say you're weak at capacity math; `error_id` says you drop the
load factor specifically, every time.

## Content and copyright

Casebooks are copyrighted. Bundling them into something distributed to "anyone
who installs it" is redistribution, so the engine **ships empty of them by
design**: bundled cases are originals, and anything derived from a casebook
stays in `~/.casecraft/cases/` on the machine that made it. Both directories
load, user cases last so they can override.

This also turns the PDF ingest pipeline from a chore-before-launch into a
product feature.

## Layout

| file | role |
|---|---|
| `server.py` | 16 MCP tools; descriptions double as the interviewer's operating instructions |
| `room.py` | FastAPI + SSE bridge, `/transcribe` for local STT, uvicorn on a daemon thread |
| `session.py` | `Room` (the MCP↔browser mailbox) and `Session` (the state machine) |
| `library.py` | case loading, the public/briefing views, drill queries |
| `scoring.py` | spoken-number extraction, numeric grading, bucket policy, scorecard |
| `delivery.py` | how it was said, independent of whether it was right |
| `progress.py` | attempt log → weakness model |
| `static/room.html` | the interview room |
| `skill/casecraft/SKILL.md` | how Claude should actually interview |

## Not yet built

- **`import_casebook`** — PDF → schema. The biggest remaining piece, and the one
  that makes the tool useful beyond the bundled cases. Auto-extracted rubrics
  will need a review pass; `common_errors` in particular won't survive naive
  extraction.
- **Candidate-led flow.** The Meridian case is marked `candidate_led` and the
  skill describes the difference, but the tools still advance through a fixed
  question order. A real candidate-led case lets the candidate choose the
  branch.
- **Push-back prompts.** The skill tells Claude to challenge a correct answer
  once per case; there's no tooling to track whether it did.
- **More content.** Two cases, eleven questions. Enough to prove the engine,
  not enough to practise on for a week.
