# Tesla AI/Data Engineer Intern, Technical Assignment

BESS revenue analysis (Part 1) and fleet log intelligence with an agentic
harness (Part 2). Headline results and charts for both parts are in
[report.md](report.md).

## How to run

```bash
pip install -r requirements.txt

# Part 1 - BESS revenue analysis
python analysis.py

# Part 2 - log ETL, diagnostics, agent demo
python etl.py
python diagnostics.py
python -m agent.run_demo
python -m agent.harness "how many errors per subsystem"   # ask your own question
```

Requires Python 3.10+. Everything runs offline with no API key; setting
`ANTHROPIC_API_KEY` upgrades the Part 2 agent from the keyword router to real
LLM tool-calling (see Part 2 below). Outputs land in `outputs/`: charts used in
the report, `bess.db` / `logs.db` (SQLite databases you can run `queries.sql`
against), and saved console output for both parts.

## Approach

The data is 15-minute readings, so I turn each row's power into energy with
`energy = Power × 0.25h`, then into money with `× Market Price`. That gives a signed
**net** revenue per row: discharging earns, charging costs. I use net throughout
because it's what actually matters for the outage question. A site that's offline
loses its discharge income but also avoids paying to charge. (Gross discharge-only
value is reported alongside so the choice is transparent.) Under net, Frequency
Regulation comes out slightly negative, which is expected: the dataset only has energy
prices, and in real life FR is paid mostly through availability fees that aren't here.

For the outage window I build a daily net-revenue series, take a rolling 14-day sum
sliding one day at a time, keep every window fully inside 1 Jan to 31 Mar, and pick the
cheapest. The result is double-checked two ways: a brute-force scan over all 78
candidate windows, and an assertion that the pandas and SQL revenue totals match.

The three SQL insights (monthly revenue, best/worst day, average Peak Shaving price)
live in `queries.sql` and run against SQLite, which is standard library, needs no extra
dependencies, and keeps the file plain and runnable on its own. The dataset covers
January to June 2024, so these monthly and daily figures span all six months, while the
14-day outage window is deliberately restricted to Q1 (1 Jan to 31 Mar) as the scenario
requires. As a cross-check, the lowest-revenue day the SQL finds (24 March) is exactly
the last day of the recommended 11 to 24 March outage window.

`analysis.py` also validates the input on load (columns present, no nulls, no gaps in
the 15-minute grid) and fails fast with a clear message if something's off.

## A note on the energy totals (is the data real?)

The battery discharges about 139 MWh more than it charges, which at first looks
impossible: you can't get more energy out than you put in. The explanation is SOC. The
site starts around 79% full and ends around 10% full, so it releases roughly 137 MWh of
previously stored energy, and that almost exactly closes the 139 MWh gap.

What's left after that correction is the real tell. Once you account for the stored
energy, the implied round-trip loss is essentially zero. A real battery **always** loses
some energy on every charge and discharge (round-trip efficiency is typically 85 to
92%), so a near-zero or negative loss is physically impossible. That strongly suggests
the dataset is synthetic. It doesn't change the revenue analysis, but it's worth stating.

## Assumptions

- All energy settles at the given market price, with no FR availability fees, network
  charges, or degradation costs, since none are in the data.
- Positive power = discharge, negative = charge (per the spec).
- The outage is whole calendar days, scheduled fully inside Q1 2024.

One thing worth being upfront about: the window is chosen with full hindsight over the
whole quarter's prices. In reality you'd have to commit to a maintenance date in
advance, before those prices are known, so a real deployment would need a price
forecast and would carry the risk of that forecast being wrong.

## What I'd do with more time

- **Forecast the prices instead of using hindsight.** Right now I pick the cheapest
  window because I can already see that March was cheap. In real life you have to choose
  the maintenance date *before* you know future prices, so you'd need a price forecast
  and accept that it might be wrong.
- **Add the missing Frequency Regulation income.** FR shows up as slightly money-losing
  (about -£80/day) only because this dataset has energy prices but not the availability
  payments that FR actually earns most of its money from. With those contract numbers,
  that mode would look profitable.
- **Use the two columns I left untouched (SOC and the FR Signal).** I'd use SOC to
  double-check the energy maths (does the change in SOC over an interval match the power
  flowing in and out?) and to estimate battery wear from how hard it cycles. I'd compare
  the FR Signal against the actual power output to see how closely the battery follows
  the grid, which is a quality measure rather than a money one.

## Part 2, Fleet log intelligence

Parsing lives in `etl.py`, diagnostics in `diagnostics.py`, and the agent in
`agent/` — deliberately separate files because they are different jobs: turning
messy text into a trustworthy table (deterministic ETL), interpreting that table
(analysis), and exposing it to natural language (automation).

### Approach

**ETL.** The log looks clean but isn't: of 5,746 lines, five are malformed. The
policy is *never silently drop* — every input line is accounted for. Lines that
can be salvaged safely are kept with an explicit `salvage_note` (missing
severity → `UNKNOWN`, empty subsystem → `UNKNOWN`, a truncated timestamp →
seconds assumed `:00`); the one unparseable line (`CORRUPTED ENTRY ### sensor
dropout`) is quarantined to a `rejected_lines` table with a reason, and one
blank line is counted. Numeric values are extracted with one regex per message
shape rather than "grab the first number", because in messages like
`Overtemperature fault Module 7: 78.1°C` the module number comes first — a
naive parser would store 7 instead of 78.1. Each row gets `metric`, `value`,
`unit`, `module`, and a normalized `message_template` (numbers → N) that the
SQL uses to group "error types".

**Diagnostics.** Alerts (45 of 5,744 events) are clustered into episodes: a
quiet gap of over an hour starts a new episode (alerts inside an ongoing issue
recur every few minutes here, so an hour of silence separates unrelated
stories). Each episode is scored (CRITICAL=10, ERROR=5, WARNING=1), tagged with
the fleet-health issue types it contains (thermal event, coolant-flow loss,
cell imbalance, voltage drop, grid-sync instability, SOC mismatch), and
classified as internal vs externally-driven from evidence: grid-only signals
plus self-recovery without a trip means external; a cross-subsystem escalation
ending in a trip, or a progressive drift ending in an inspection flag, means
internal. The three episodes this finds, and the reconstructed timeline of the
18 June thermal incident, are in [report.md](report.md).

**Agent.** Five deterministic tools over the structured database (query events,
stats, plot signals, incident summary as JSON, service-ticket drafting) with
two interchangeable routers on top. With `ANTHROPIC_API_KEY` set, a small LLM
(`claude-haiku-4-5`) does real tool-calling: it picks the tools, reads their
JSON results, and writes the answer. Without a key, a keyword router maps the
assignment's prompts to the same tools and fills templates. The design rule is
*LLM where language is the problem, plain code where correctness is the
problem*: every number, timestamp and chart comes from a tool, never from the
model, so answers are auditable against the database. Questions neither router
can ground in tool results get an explicit "I can't answer that" — tools return
structured errors (unknown metric, date outside the log) rather than defaults,
so the LLM has nothing to hallucinate from, and the keyword router refuses
anything unscripted. Known limitation of the fallback: it only handles the
scripted intents, while LLM mode can compose tools for novel questions.
`agent/transcript.md` shows the three assignment prompts answered end to end
(text + JSON + chart).

### Assumptions

- Severity weights (10/5/1) and the 60-minute episode gap are judgment calls,
  stated in code; results are not sensitive to reasonable alternatives.
- "Today" is computed as the most recent calendar date in the file
  (2026-06-18), not the wall-clock date, per the assignment's definition.
- Ground-truth expectations about this specific file (45 alerts, 5 malformed
  lines, 3 episodes) are printed ✓/⚠ checks, not hard asserts — the pipeline
  stays usable on a different log file. Hard asserts are reserved for internal
  consistency (line accounting, pandas-vs-SQL cross-checks).
- The root-cause narrative names the coolant pump as the likely trigger (flow
  collapsed while pump speed read 71%, and a pump restart fixed it); the logs
  cannot distinguish pump wear from a blockage or a leak — that needs the site
  visit the ticket recommends.

### What I'd do with more time

- **The bonus sections (B1 predictive RUL design, B2 auto-research agent
  design)** — deliberately deferred to keep the core five steps polished; the
  Module 4 imbalance data here would make a natural worked example for B1.
- **Streaming ETL.** The parser reads the whole file; a real fleet emits logs
  continuously, so the next step is incremental ingestion with the same
  salvage/quarantine policy and idempotent upserts into the database.
- **Learned thresholds.** Episode detection uses the log's own alert severities;
  detecting *pre-alert* anomalies (the coolant flow was already sagging at
  13:19, three minutes before the first WARNING) needs baselines per metric —
  even simple rolling z-scores would have bought extra minutes of warning.
- **Harden the agent.** Conversation memory (follow-up questions), a small eval
  set of question/expected-tool pairs to regression-test routing, and running
  the LLM answers against the JSON to auto-verify every quoted number.
