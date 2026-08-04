# casecraft — case data contract

This is the target format for the parsing/cleaning step. Everything downstream
(drill modes, grading, probing, progress tracking) reads only this.

## The one rule

**Questions are first-class, not prose inside a case.** Drill modes
("only math", "hard math", "market sizing") are a `WHERE` clause over the
question table. A case is a container that gives questions shared context; it
is not the unit of practice.

```
Case ──┬── prompt          (read aloud to start)
       ├── clarifications  (revealed ONLY when asked for)
       ├── exhibits        (revealed at a specific question)
       └── questions[]     ← the practiceable unit, individually addressable
```

## Answer keys are rubrics, not paragraphs

Casebooks give prose answers. Prose can't be scored, can't drive a targeted
probe, and can't tell you *which* skill you're weak at. The cleaning step's real
job is converting prose into scoreable components.

Three rubric kinds cover ~everything:

| kind | used by | scored by |
|---|---|---|
| `buckets` | framework, brainstorm, synthesis | LLM matches answer → component ids |
| `numeric` | math, market sizing | **deterministic** — no model call |
| `open` | "what would you tell the CEO?" | LLM against model answer + criteria |

`numeric` being deterministic matters more than it looks: it makes math feedback
*instant* and lets you diagnose the specific error (see `common_errors`) without
a round-trip.

## Case object

```jsonc
{
  "id": "wharton-2024-orchid-airlines",     // stable, unique
  "title": "Orchid Airlines — Falling Profits",
  "source": { "casebook": "Wharton 2024", "page": 42 },

  "meta": {
    "format": "interviewer_led",            // interviewer_led | candidate_led
    "case_type": "profitability",           // profitability | market_entry | market_sizing
                                            // | m_and_a | ops | pricing | growth
    "industry": "airlines",
    "difficulty": 3,                        // 1–5, case-level
    "expected_minutes": 30,
    "tags": ["breakeven", "cost_structure", "chart_reading"]
  },

  "prompt": {
    "text": "Our client is Orchid Airlines, a regional US carrier ...",
    "read_aloud": true
  },

  "clarifications": [ /* see below */ ],
  "exhibits":       [ /* see below */ ],
  "questions":      [ /* see below */ ]
}
```

### `clarifications` — the withheld facts

```jsonc
{
  "id": "geography",
  "topic": "where the client operates",     // ONLY this is shown to the matcher
  "match": ["where", "geography", "region", "markets", "routes"],
  "response": "Orchid operates 40 routes, all within the continental US."
}
```

The matcher model receives the list of `{id, topic}` pairs and the candidate's
question, and returns an id or `null`. It never sees `response`. Unasked facts
are therefore unleakable — enforced by the data flow, not by instructions.

### `exhibits` — charts and tables

```jsonc
{
  "id": "ex1",
  "title": "Cost per available seat mile, 2019–2024",
  "reveal_at": "q4",                        // question id, or "on_request"
  "asset": "exhibits/orchid_ex1.png",
  "data": [ /* optional machine-readable form, for grading exhibit reads */ ],
  "read_aloud_intro": "I'm showing you a chart of cost per available seat mile."
}
```

Exhibits are the one thing that *does* appear on screen — a real interviewer
slides paper across the table.

### `questions`

```jsonc
{
  "id": "q1",
  "order": 1,
  "type": "framework",        // framework | math | brainstorm | exhibit | synthesis
  "difficulty": 3,            // 1–5, PER QUESTION — a hard case can open easy
  "tags": ["mece", "profit_equation"],
  "prompt": "What factors would you consider to find out why profits are declining?",
  "read_aloud": true,
  "time_target_sec": 120,     // for pacing feedback, not a hard cutoff
  "rubric": { /* one of the three kinds */ },
  "probes": [ /* progressive nudges, weakest first */ ],
  "model_answer": "Prose from the casebook — shown AFTER grading, never before."
}
```

#### rubric: `buckets`

```jsonc
{
  "kind": "buckets",
  "components": [
    { "id": "revenue", "label": "Revenue drivers (passengers × price per ticket)",
      "weight": 2, "must_have": true,
      "accept": ["revenue", "price", "yield", "load factor", "passengers"] },
    { "id": "fixed_costs",    "label": "Fixed costs (leases, gates, salaried crew)",
      "weight": 2, "must_have": true },
    { "id": "variable_costs", "label": "Variable costs (fuel, per-passenger service)",
      "weight": 2, "must_have": true },
    { "id": "competition",    "label": "External: new low-cost entrants",
      "weight": 1 }
  ],
  "bonus": [
    { "id": "mece",     "label": "Structure stated upfront and MECE", "weight": 1 },
    { "id": "hypothesis","label": "Leads with a hypothesis",          "weight": 1 }
  ]
}
```

`accept` is a hint list for the matcher, not a keyword gate — candidates say
"how much they charge," not "price per ticket."

#### rubric: `numeric`

```jsonc
{
  "kind": "numeric",
  "expected": 2312640000,
  "units": "USD per year",
  "tolerance_pct": 2,                  // rounding is fine; that's how real cases work
  "steps": [                           // partial credit + "where did you go wrong"
    { "id": "flights",  "label": "240 flights/day",        "value": 240 },
    { "id": "pax",      "label": "120 passengers/flight",  "value": 120 },
    { "id": "pax_day",  "label": "28,800 passengers/day",  "value": 28800 },
    { "id": "rev_day",  "label": "$6.34M/day",             "value": 6336000 }
  ],
  "common_errors": [
    { "value": 2890800000, "diagnosis": "Used all 150 seats — forgot the 80% load factor." },
    { "value": 6336000,    "diagnosis": "Stopped at daily revenue; didn't annualize." }
  ]
}
```

`common_errors` is the highest-value field in the whole schema and the one a
naive parser will skip. Matching a wrong answer to a *named* mistake is the
difference between "incorrect, the answer is $2.31B" and "you forgot the load
factor" — and it costs zero model calls.

#### rubric: `open`

```jsonc
{
  "kind": "open",
  "criteria": [
    { "id": "answer_first", "label": "States the recommendation before the reasoning", "weight": 2 },
    { "id": "quantified",   "label": "Cites the $2.3B revenue / 12% margin figures",   "weight": 2 },
    { "id": "risks",        "label": "Names at least one risk and a mitigation",       "weight": 1 }
  ],
  "model_answer": "..."
}
```

## Progress tracking

Every graded attempt writes one row:

```jsonc
{
  "user_id": "oliver", "question_id": "wharton-2024-orchid-airlines/q3",
  "case_type": "profitability", "question_type": "math",
  "tags": ["breakeven"], "difficulty": 4,
  "score": 0.6, "passed": false, "probes_used": 2,
  "seconds_spent": 187, "error_id": "load_factor",     // from common_errors
  "transcript": "...", "at": "2026-08-02T17:04:11Z"
}
```

Weakness targeting is then just: aggregate score by `(question_type, tag)`,
and bias the sampler toward the bottom of that list. `error_id` is what lets it
say "you drop the load factor on every capacity question," which is the actual
product.
