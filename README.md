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
python -m agent.harness "how many errors per subsystem"   # or any free-form question
```

Requires Python 3.10+. Everything runs offline with no API key; setting
`ANTHROPIC_API_KEY` upgrades the Part 2 agent from the keyword router to real
LLM tool-calling (see Part 2 below). Outputs land in `outputs/`: charts used in
the report, `bess.db` / `logs.db` (SQLite databases that `queries.sql` runs
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
impossible: a battery can't put out more energy than it takes in. The explanation is SOC.
The site starts around 79% full and ends around 10% full, so it releases roughly 137 MWh
of previously stored energy, and that almost exactly closes the 139 MWh gap.

What's left after that correction is the real tell. Once I account for the stored
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
whole quarter's prices. In reality the maintenance date has to be committed in advance,
before those prices are known, so a real deployment would need a price forecast and
would carry the risk of that forecast being wrong.

## What I'd do with more time

- **Forecast the prices instead of using hindsight.** Right now I pick the cheapest
  window because I can already see that March was cheap. In real life the maintenance
  date is chosen *before* future prices are known, so I'd need a price forecast and
  accept that it might be wrong.
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

### Approach

I split the work into three files because they are three different jobs: `etl.py`
turns messy text into a trustworthy table, `diagnostics.py` interprets that table,
and `agent/` exposes it to natural language.

The log looks clean but isn't: of 5,746 lines, five are malformed, and my policy is
to never silently drop a line. Whatever can be salvaged safely is kept with a note
(missing severity, empty subsystem, truncated timestamp); the one unparseable line
is quarantined to a `rejected_lines` table with a reason, and every input line is
accounted for. Numeric values are extracted with one regex per message shape rather
than "grab the first number", because in messages like `Overtemperature fault
Module 7: 78.1°C` the module number comes first and a naive parser would store 7
instead of 78.1.

For diagnostics I cluster the 45 alerts into episodes (an hour of quiet starts a
new one), score each episode by severity, tag it with the issue types it contains
(thermal event, coolant-flow loss, cell imbalance, voltage drop, grid-sync
instability, SOC mismatch), and classify it as internal or externally-driven from
the evidence: grid-only signals that recover on their own mean external, a
cross-subsystem escalation ending in a trip means internal. The three episodes this
finds, and the timeline of the 18 June thermal incident, are in
[report.md](report.md).

The agent is five deterministic tools over the structured database (query, stats,
plots, incident summary as JSON, service tickets) with two interchangeable routers
on top: with `ANTHROPIC_API_KEY` set, a small LLM (`claude-haiku-4-5`) picks the
tools and writes the answer; without a key, a keyword router maps the standard
prompts to the same tools. The design rule is *LLM where language is the problem,
plain code where correctness is the problem*: every number, timestamp and chart
comes from a tool, never from the model, so answers are auditable against the
database — and questions the tools can't ground get an explicit "I can't answer
that" instead of a guess. A recorded session of the agent answering the
assignment's prompts (plus a generalization test and a refusal) is in
[agent/terminal_session.md](agent/terminal_session.md), and the optional bonus
designs are in [bonus.md](bonus.md).

### Assumptions

- I read the PDF's "output should include text, a JSON summary, and at least one
  chart" as applying to every answer, so summaries and tickets attach an evidence
  chart too.
- The severity weights (10/5/1) and the one-hour gap that separates episodes are
  my judgment calls.
- "Today" means the last day in the log (18 June 2026), not the real date.
- The logs point to the coolant pump as the trigger, but they can't prove pump
  wear versus a blockage or leak — that needs the site visit the ticket recommends.

### What I'd do with more time

- **Harden the agent.** Conversation memory for follow-up questions, a small eval
  set of question-to-expected-tool pairs to regression-test the routing, and
  auto-checking every number in an LLM answer against the tool JSON.
- **More tools, sharper logic.** Per-module queries, day-vs-day comparisons,
  richer statistics, and more specific tool descriptions so routing gets even
  more precise on unusual questions.
