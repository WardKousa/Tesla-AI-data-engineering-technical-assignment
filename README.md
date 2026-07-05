# Tesla AI/Data Engineer Intern — Technical Assignment

BESS revenue analysis (Part 1) and, to follow, fleet log intelligence with an
agentic harness (Part 2).

## How to run

```bash
pip install -r requirements.txt
python analysis.py
```

Requires Python 3.10+. The script prints the full analysis to the console and writes
two things to `outputs/`:

- `part1_outage_window.png` — the chart used in the report
- `bess.db` — a SQLite database you can run `queries.sql` against directly
  (`sqlite3 outputs/bess.db < queries.sql`)

The headline result and chart are in [report.md](report.md).

## Approach

The data is 15-minute readings, so I turn each row's power into energy with
`energy = Power × 0.25h`, then into money with `× Market Price`. That gives a signed
**net** revenue per row: discharging earns, charging costs. I use net throughout
because it's what actually matters for the outage question — a site that's offline
loses its discharge income but also avoids paying to charge. (Gross discharge-only
value is reported alongside so the choice is transparent.) Under net, Frequency
Regulation comes out slightly negative, which is expected: the dataset only has energy
prices, and in real life FR is paid mostly through availability fees that aren't here.

For the outage window I build a daily net-revenue series, take a rolling 14-day sum
sliding one day at a time, keep every window fully inside 1 Jan–31 Mar, and pick the
cheapest. The result is double-checked two ways: a brute-force scan over all 78
candidate windows, and an assertion that the pandas and SQL revenue totals match.

The three SQL insights (monthly revenue, best/worst day, average Peak Shaving price)
live in `queries.sql` and run against SQLite — standard library, no extra
dependencies, and the file stays plain and runnable on its own.

`analysis.py` also validates the input on load (columns present, no nulls, no gaps in
the 15-minute grid) and fails fast with a clear message if something's off.

## Assumptions

- All energy settles at the given market price — no FR availability fees, network
  charges, or degradation costs, since none are in the data.
- Positive power = discharge, negative = charge (per the spec).
- The outage is whole calendar days, scheduled fully inside Q1 2024.

One thing worth being upfront about: the window is chosen with full hindsight over the
whole quarter's prices. In reality you'd have to commit to a maintenance date in
advance, before those prices are known — so a real deployment would need a price
forecast and would carry the risk of that forecast being wrong.

## What I'd do with more time

- Forecast prices for the actual outage period instead of leaning on realized history.
- Add FR availability revenue if the contract terms were available, so that mode's
  economics are complete.
- Use the two columns I left untouched: **SOC** to sanity-check the energy accounting
  (does the change in SOC over an interval line up with the integrated power?) and to
  look at cycling depth as an early degradation signal; and the **Frequency Regulation
  Signal** compared against dispatched power during FR mode, to measure how closely the
  battery actually tracks the grid — a service-quality view, not just a revenue one.

## Part 2 — Fleet log intelligence (to follow)
