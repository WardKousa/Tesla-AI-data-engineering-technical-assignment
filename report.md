# Part 1, Maintenance Outage Recommendation

**Schedule the 14-day outage for 11–24 March 2024.**

Of every 14-day window in Q1, this one costs the least to be offline for: about
**£24k** of net revenue, versus roughly £150k for an average window and £247k for the
worst (early January). Picking it saves around £126k over scheduling the outage blind.

The reason is simply that March is the cheap part of the quarter (prices average
£33/MWh in March against £72/MWh in January), so the battery earns very little during
that fortnight anyway. The chart below shows daily net revenue (bars) and the running
14-day total (line); the shaded band is the recommended window, sitting right at the
bottom of the curve. As an independent check, the lowest-revenue single day in the whole
dataset (24 March) falls on the last day of this window.

![Recommended outage window](outputs/part1_outage_window.png)

All figures are *net* revenue (discharge income minus charging cost), which is the right
measure here: an offline site loses its discharge earnings but also stops paying to
charge.
