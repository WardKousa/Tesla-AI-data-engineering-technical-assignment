# Stakeholder Report

## Part 1 — BESS Revenue Analysis & Maintenance-Outage Recommendation

**Recommendation: schedule the 14-day full-site maintenance outage for 11–24 March 2024.**

That window puts **GBP 23,881** of net revenue at stake — the lowest of all 78 candidate
14-day windows inside Q1 2024. The average window would cost GBP 150,135 and the worst
(early January) GBP 247,275, so choosing this window saves roughly **GBP 126,000 (~84%)
versus a randomly scheduled outage**, and over GBP 220,000 versus the worst case.

![Recommended outage window](outputs/part1_outage_window.png)

### Why this window

- Market prices bottom out in March (monthly average GBP 32.59/MWh vs GBP 71.56 in
  January), following a U-shaped seasonal price curve across the dataset.
- Within 11–24 March specifically, the average price falls to ~GBP 20/MWh and site
  dispatch drops to ~40% of its Q1 norm — the battery simply has very little profitable
  work to do in that fortnight. Revenue lost by being offline is minimal.

### Supporting figures (Jan–Jun 2024, 15-minute data)

| Metric | Value |
|---|---|
| Total energy discharged | 46,225 MWh |
| Total energy charged | 46,086 MWh |
| Avg daily net revenue — Energy Arbitrage | GBP 5,917/day |
| Avg daily net revenue — Peak Shaving | GBP 5,699/day |
| Avg daily net revenue — Frequency Regulation | **−GBP 81/day** |
| Highest-revenue day | 1 Jan 2024 (GBP 26,418) |
| Lowest-revenue day | 24 Mar 2024 (GBP 1,391) |
| Avg market price during Peak Shaving | GBP 58.69/MWh |

**Revenue convention:** all figures are *net* revenue — discharge income minus charging
cost (`Power × 0.25 h × Market Price`, summed). Net is the right lens for the outage
decision because an offline site loses discharge income but also avoids charging cost.
Under this convention Frequency Regulation is slightly net-negative: the dataset contains
only energy prices, and FR is in reality paid mainly through availability fees that are
not in the data (stated assumption).

### Observations & caveats

- Discharged energy slightly exceeds charged energy (ratio 1.003), which is physically
  implausible for a real battery (round-trip efficiency < 1) — consistent with this
  being simulated data. It does not materially affect the revenue ranking.
- The recommendation assumes Q1 2024 history is representative of the future outage
  period. With more time I would forecast prices for the actual target year rather than
  rely on one historical quarter, and would add FR availability revenue if contract data
  were available.

---

## Part 2 — Fleet Log Intelligence (to follow)
