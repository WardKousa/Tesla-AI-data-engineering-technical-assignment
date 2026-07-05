# Tesla AI/Data Engineer Intern — Technical Assignment

Solution for the Energy (EMEA, Amsterdam) technical assignment: BESS revenue analysis
(Part 1) and fleet log intelligence with an agentic harness (Part 2).

## Setup

```bash
python -m venv .venv          # optional
pip install -r requirements.txt
```

Requires Python 3.10+.

## Part 1 — BESS performance & revenue analysis

```bash
python analysis.py            # uses data/merged_bess_market_data.csv
```

Prints the full analysis to the console and writes:

- `outputs/part1_outage_window.png` — daily net revenue with the recommended
  14-day outage window shaded
- `outputs/bess.db` — SQLite database (`bess_readings`) against which
  `queries.sql` can be re-run directly (`sqlite3 outputs/bess.db < queries.sql`
  works too, since the file currently contains only Part 1 statements)

Findings and the outage recommendation are in [report.md](report.md).

### Approach & key decisions

- **Energy**: rows are 15-minute interval-average power, so
  `energy (MWh) = Power (MW) × 0.25 h` (rectangle integration — exact for
  interval data).
- **Revenue convention — net, stated explicitly**:
  `revenue = Power × 0.25 × Market Price`; discharge earns, charging costs.
  Net revenue is the decision metric for the outage question because an
  offline site loses discharge income *and* avoids charging cost. Gross
  discharge value is reported alongside. Under net, Frequency Regulation is
  slightly negative — expected, since FR availability payments are not in the
  dataset.
- **Average daily revenue per mode**: revenue summed per (day, mode), then
  averaged over the days each mode was active — "what does a day of running
  this mode earn", not diluted across all calendar days.
- **Lowest-revenue 14-day window**: daily net revenue → 14-day rolling sum,
  sliding one day at a time, window constrained fully inside 1 Jan – 31 Mar
  2024 (78 candidates). Result cross-checked with a brute-force loop.
- **SQL**: SQLite via the Python standard library — zero extra dependencies,
  and the `queries.sql` deliverable stays a plain, runnable file.
- **Validation**: `analysis.py` fails fast if the CSV has missing columns,
  nulls, duplicates, or gaps in the 15-minute grid (it currently has none),
  and asserts pandas and SQL totals agree.

### Assumptions (Part 1)

1. All energy settles at the given market price; no FR availability fees,
   network charges, or degradation costs (not present in the data).
2. Positive power = discharge, negative = charge (per the spec).
3. The outage is whole calendar days, fully inside Q1 2024.
4. Q1 2024 history is a proxy for the future outage period (no forecasting).

### With more time

- Forecast prices for the actual outage year instead of using one historical
  quarter.
- Model FR availability revenue if contract terms were available.
- Use SOC and the FR signal for deeper analysis (cycling behaviour,
  regulation performance).

## Part 2 — Fleet log intelligence (to follow)

## Repository layout

```
analysis.py      Part 1 analysis
queries.sql      SQL deliverable (Part 1; Part 2 appended later)
data/            input datasets
outputs/         generated chart, SQLite DB, console output
report.md        stakeholder-facing findings
```
