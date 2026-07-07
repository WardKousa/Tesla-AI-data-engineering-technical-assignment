# Bonus: Predictive Diagnostics and Auto-Research

Both bonuses are design answers. I ran out of time to build them, so I describe what I
would build and why.

## B1: Predicting cell wear-out before it happens

Everything else in this assignment reacts to problems after they happen. B1 is the
opposite: catch a cell going bad before it fails, by predicting RUL (remaining useful
life) per cell.

The main idea is to look at the trajectory, not the snapshot. A cell at 0.12 V imbalance
sitting flat is fine; the same cell climbing 0.01 V/hour is failing. So I'd feed the
model the trend of each signal over time, not the raw current value.

**What I'd predict**
- RUL: the main target, cycles or days of life left.
- SOH: secondary, current health as a percentage of new. It isn't a raw column, so I'd
  derive it from remaining capacity. SOH is where the cell is now, RUL is how long until
  the end.

**Handling the data**
- The telemetry is sequential and high-frequency, so instead of feeding raw points I'd
  aggregate into fixed time windows (mean, min, max, slope per window).
- Window size matches how fast the signal moves: per-cycle or hourly for slow
  degradation, finer for fast events like a thermal fault.
- This still holds with an LSTM. The raw firehose never gets fed in directly, so
  aggregation is independent of the model choice.

**Features** (the data won't arrive like this, so I'd compute them from the aggregated
windows)
- voltage-imbalance trend and its slope (the Module 4 signal from Part 2)
- capacity fade per cycle
- coulombic efficiency (energy out over energy in each cycle)
- internal-resistance growth
- usage stress: equivalent full cycles and depth-of-discharge
- rolling stats and gradients on all of the above, so the model sees trajectory rather
  than single points

**Label**
- RUL from run-to-failure data: cells cycled to end-of-life, so the remaining life is
  known at every point.
- Public NASA battery data has this, and a real fleet generates it over time.

**Model**
- Gradient-boosted trees for a small dataset like the one in this assignment: strong on
  engineered features and explainable.
- LSTM for real production data, which is sequential and high-volume enough to train it.
- Survival models are also a natural fit, since RUL is a time-to-event problem and they
  handle cells that haven't failed yet.

**Metrics**
- RMSE and MAE, weighted to stay pessimistic since a missed failure costs more than a
  false alarm.
- Report error near end-of-life separately, since that's where a wrong prediction is
  most expensive.

**Split (to avoid leakage)**
- No random shuffling: train on earlier cycles, test on later ones.
- Group by cell so no cell lands in both sets.
- Walk-forward validation to mirror how it would run live.

**Deployment**
- If built, it runs on a schedule over the live aggregated feed, writes an RUL per cell
  to the fleet dashboard, alerts when one drops below a threshold, and retrains
  periodically as it drifts.

**With more time**
- More feature engineering for better accuracy.
- Predict per individual cell instead of per module, so a bad cell doesn't hide in the
  module average.
- Actually implement the plan in code (a minimal version on the public NASA dataset).

## B2: Auto-research harness

My Part 2 agent answers questions about my own log database. B2 aims the same idea
outward: give it an open-ended fleet-health question ("likely drivers of accelerated
cell degradation"), let it research the internet and public battery data on its own,
and get back one answer where every claim is cited and checkable.

The web can't be handled the way I handled my logs. In Part 2 I could keep everything
deterministic because I knew every message format in advance. I can't write rules for
the open internet, so the exploring has to be model-driven: which queries to run,
which pages are worth reading, when there's enough. I accept that non-determinism, and
instead make the checks deterministic: the model is free to explore, but every fact it
keeps has to pass through code.

**The shape**
- Opus 4.8 is the professor: it breaks the question into sub-questions, decides which
  sources are worth reading, and writes the final cited answer. It never reads a raw
  web page itself.
- Haiku workers are the grad students: each one gets a single page and comes back with
  notes: the claim, the exact quote, the source link. One worker per page, several
  running in parallel, and a page can yield more than one note.
- The split is deliberate: the expensive model only does the hard thinking (decompose,
  judge, synthesize), the cheap model does the bulk reading. It also keeps the
  professor's context clean — it only ever sees tidy notes, never 30 pages of ads and
  navigation menus, so it doesn't lose the thread on a long run.

**How it actually runs** (same loop as my Part 2 harness, new tools)
- The model never touches the internet; tools do. Search and fetching are plain code
  either Anthropic's server-side web_search tool or my own fetch function that hand
  page text back.
- Opus doesn't literally "spawn" workers either. It emits a tool call like
  dispatch_readers(urls), and my harness does the mechanics: one Haiku API call per
  page, in parallel, notes collected and handed back as the tool result. Model decides,
  code executes, the exact Part 2 pattern.
- One more tool, dataset_query, runs pandas over the NASA battery data (same ETL
  pattern as Part 2, aimed at their files). So for a claim like "heat accelerates
  fade," the agent doesn't just read about it; it computes the fade-vs-temperature
  numbers from real cycling data.

**Flow**
- Plan: Opus splits the question into sub-questions, each with a reading budget.
- Search and dispatch: web search per sub-question, best pages handed to readers.
- Notes: every claim comes back with its verbatim quote and source.
- Verify: code checks the quote actually appears in the fetched page (a hallucinated
  quote fails a string comparison, no AI needed to catch it); a fresh-context model
  then checks each quote really supports its claim; numeric claims need either a
  dataset computation or two independent sources.
- Synthesize: Opus writes the answer only from surviving notes, citations on every
  claim, with "what the data shows" separated from "what the literature says", and
  gaps stated honestly instead of padded.

**Cost control**
- Hard caps live in the harness, not the model: max tool calls, token budget,
  per-sub-question quotas. The model can want a 26th call; the code won't run it.
- Soft stop when the last few reads add nothing (new coverage has saturated).

**Anti-hallucination** (same principle as Part 2: never state a fact that isn't in a
tool result)
- The final answer can only cite the notes store; code verifies every citation maps to
  a real note.
- Quotes are verbatim, so they're checkable by string matching against the source.
- The NASA data gives ground truth for numbers instead of trusting the web.
- Thin evidence gets hedged or refused, not filled in.

**Trade-offs I'd accept**
- More moving parts and harder debugging than a single-model setup.
- Not reproducible run-to-run — the guardrails make it trustworthy, not identical.
- Still garbage-in-garbage-out on what the search engine surfaces; the verify step
  reduces that, it doesn't eliminate it.

**With more time**
- Wire it up and run it once on the degradation question, saving the transcript and
  the notes database as the audit trail.

