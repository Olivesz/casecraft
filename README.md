# casecraft

Spoken consulting case interview practice, run by Claude.

Open any Claude session and say **"run me through a case."** A browser tab opens,
Claude reads you a case out loud, you answer out loud, and you get graded the way
a real interviewer grades — on structure, analytics, judgment and communication,
not just whether the number was right.

**You never see the question.** That's the point. Reading a case off a screen
isn't a case interview.

```bash
./install.sh
```

Then, in Claude: *"run me through a case"* · *"drill me on hard math"* ·
*"what am I bad at?"*

---

## How it works

```
Claude ──MCP──► casecraft ──SSE──► browser tab ──► 🔊 reads the case
                    │                        └───► 🎤 hears your answer
                    ├── case library (prompts, rubrics, answer keys)
                    ├── deterministic grading (math, delivery)
                    └── progress log → weakness-targeted drills
```

Claude is the interviewer. It never receives the case text, the rubric, or the
answer key before you've answered — those live in a separate process, so there's
nothing to leak. Case prompts travel to your speakers and are never returned
through MCP, which is what keeps them off your screen.

Speech uses your browser's built-in Web Speech API: no model downloads, no API
keys, no per-session cost. Playback rate is a true multiplier, so the three
pace settings are exact.

## What gets graded

Four dimensions, the same four every firm's feedback form uses under different
names. 3 is the bar; one 2 sinks a candidate with three 4s, which is why the
scorecard reports a `limiting_factor` rather than an average.

| | |
|---|---|
| **Structure** | MECE buckets stated upfront, hypothesis-led, prioritized |
| **Analytics** | Accurate, sanity-checked, reasoned out loud |
| **Judgment** | Says "so what", prioritizes, brings business sense |
| **Communication** | Top-down, signposted, concise, conviction |

Two things happen without any model call, instantly:

**Math is graded deterministically** — and diagnosed, not just marked. Say
*"2.89 billion"* on a capacity question and you get *"You used all 150 seats —
forgot the 80% load factor"*, because the case data names the mistakes
candidates actually make. Intermediate steps earn partial credit, so a wrong
final answer still shows where you diverged.

**Delivery is analysed from the transcript** — hedging, signposting, whether
you led with the answer, whether you gave a number without its implication,
pace against the time target. This is the feedback candidates never get, because
a human coach can't count your hedges while also running the case.

> Note: Whisper and the Web Speech API both clean up disfluencies, so "um" and
> "uh" largely don't survive transcription. Filler counts are reported as a
> floor. Hedging ("I think maybe") is real words and survives intact — which is
> why the scoring leans on it instead.

## Practice modes

- **Full case** — prompt, clarifying questions, structure, math, exhibit,
  synthesis, scorecard. Interviewer-led (McKinsey style) or candidate-led
  (Bain/BCG style).
- **Drills** — loose questions pulled across cases. *"Only hard math"* is a
  filter, because questions are first-class rather than buried inside case
  documents. Selection biases toward what you keep getting wrong, without ever
  fully excluding the rest.

## Your own cases

casecraft ships with original cases only. Casebooks are copyrighted, so the
engine ships empty of them by design — import your own and they load
automatically alongside the bundled ones, from `~/.casecraft/cases/`.

```bash
python -m casecraft.ingest yourcasebook.pdf          # --dry-run to preview
```

Handles Darden-format casebooks and math drill playbooks, including PDFs whose
fonts extract as gibberish. Imported cases **never** land in `data/cases/`,
so nothing you distribute can contain them.

Two honest caveats about auto-imported content:

* **Math is graded against the worked solution, not a number.** Pulling a final
  answer out of free-form worked solutions proved about one-in-three reliable —
  multi-part questions and intermediate results all look like conclusions to a
  parser. Rather than mark correct answers wrong, imported math uses an `open`
  rubric and keeps the heuristic number in `_candidate_expected`. Verify it and
  swap in `{"kind": "numeric", "expected": ...}` to get instant deterministic
  grading for that question.
* **Questions are dropped rather than guessed.** These slides interleave the
  question with its answer, so anything that doesn't read as a question is
  skipped — you get fewer questions, but none that read the solution aloud.

The format is documented in [SCHEMA.md](SCHEMA.md). The one thing worth getting
right when you parse: **answer keys have to become rubrics, not prose.** Prose
can't be scored, can't drive a targeted probe, and can't tell you which skill
you're weak at. Pay particular attention to `common_errors` — it's the field a
naive parser skips and the one that turns "incorrect" into a diagnosis.

## Layout

```
casecraft/
  server.py     MCP tools — the interviewer's hands
  room.py       local HTTP + SSE bridge to the browser
  session.py    interview state machine
  library.py    case loading, drill queries, weakness-biased sampling
  scoring.py    numeric grading + bucket policy + scorecard
  delivery.py   hedging, signposting, pacing, "so what"
  progress.py   attempt log → weakness model
  static/       the interview room
skill/casecraft/SKILL.md    how Claude should interview
data/cases/                 bundled original cases
```

## Development

```bash
.venv/bin/python -m casecraft --check     # the JSON loads
.venv/bin/python -m pytest tests/ -q      # 130 unit + integration tests
.venv/bin/python -m tests.soak            # every case + drills + API abuse
.venv/bin/python -m tests.playthrough     # one full interview, adversarially
.venv/bin/python -m tests.stress          # concurrency, state leaks, ports
.venv/bin/python -m tests.fuzz 7          # 180 random tool calls, invariants
.venv/bin/python -m tests.calibrate       # tiered answers: better never scores worse
.venv/bin/python -m tests.validate_cases  # case coherence: refs, weights, collisions
.venv/bin/python -m tests.dogfood         # 3 full flows, dialogue printed for reading
.venv/bin/python -m tests.inspect_output  # scorecards/reports as a candidate reads them
```

Tests deliberately weight the failure paths. A prep tool that only works on
model answers is useless — the product *is* what it says when you're wrong.

The suites are layered on purpose, because each caught bugs the others could
not see:

* **pytest** pins grading policy, delivery analysis, and source-level
  invariants — every tool that speaks must reopen the microphone; the page may
  never invent turn state locally.
* **soak** drives all 25 cases and both drill modes to a scorecard. Most of the
  library is imported from PDFs where fields are missing and rubrics are `open`
  rather than `buckets`; bugs there are invisible on the two hand-written cases.
* **playthrough** runs one interview while abusing it: talking out of turn,
  empty answers, 20,000-character replies, unicode, prompt injection.
* **stress** hits concurrency and lifecycle. It found a captured answer
  surviving into the *next* case, where it would have been graded against a
  different rubric with a plausible-looking verdict.

### Diagnosing a live room

The room is introspectable, so an agent never needs a human to describe what
they see on screen:

```bash
curl -s localhost:7654/debug
```

`page_problem` is the field to read first: it names why the room won't work
(no tab open, stale build, audio still locked by the browser) or is null when
healthy. `POST /debug/act` drives the room as the candidate — `press_start`,
`say`, `open_mic` — so a whole interview can be rehearsed with nobody present.

## Requirements

- Python 3.11+
- Chrome or Safari (Firefox has no speech recognition — you can still type)
- macOS or Linux

Browser speech recognition sends audio to the browser vendor's service. If you'd
rather keep it on-device, `pip install -e ".[local-stt]"` and the room will use
local Whisper instead.
