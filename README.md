# Tesla AI/Data Engineer Intern, Technical Assignment

BESS revenue analysis (Part 1) and, to follow, fleet log intelligence with an
agentic harness (Part 2).

## How to run

```bash
pip install -r requirements.txt
python analysis.py
```

Requires Python 3.10+. The script prints the full analysis to the console and writes
two things to `outputs/`:

- `part1_outage_window.png`, the chart used in the report
- `bess.db`, a SQLite database you can run `queries.sql` against directly
  (`sqlite3 outputs/bess.db < queries.sql`)

The headline result and chart are in [report.md](report.md).

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

## Part 2, Fleet log intelligence (to follow)
