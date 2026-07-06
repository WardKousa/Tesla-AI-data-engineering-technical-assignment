# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Tesla AI/Data Engineer Intern technical assignment, two parts (both complete):

- **Part 1**: BESS (battery energy storage) revenue analysis of `data/merged_bess_market_data.csv` (15-minute readings, Jan–Jun 2024, 200 MWh UK site). Deliverables: `analysis.py`, `queries.sql`, `report.md`.
- **Part 2 (the main part)**: fleet log intelligence over `data/MP_Logs.txt` (pipe-delimited Megapack logs, 3 days, 5 subsystems). Deliverables: `etl.py` (parse/validate/persist), `diagnostics.py` (episodes, incident root-cause, SQL insights), `agent/` (NL question harness + transcript), Part 2 sections in `report.md`/`README.md`. The bonus sections (B1/B2 designs, `bonus.md`) are deliberately deferred — noted in README's "with more time".

## Commands

```bash
pip install -r requirements.txt   # pandas, matplotlib, anthropic; Python 3.10+
python analysis.py                # Part 1: prints analysis, writes outputs/
python etl.py                     # Part 2 step 1: builds outputs/logs.db + CSV
python diagnostics.py             # Part 2 steps 2+3 (auto-builds logs.db if missing)
python -m agent.run_demo          # Part 2 step 4: 3 prompts -> agent/transcript.md
python -m agent.harness "..."     # ask the agent one question
sqlite3 outputs/bess.db < queries.sql   # Part 1 SQL standalone
```

The agent runs in LLM mode (Anthropic tool-calling, `claude-haiku-4-5`) iff `ANTHROPIC_API_KEY` is set; otherwise a deterministic keyword router answers the scripted intents and refuses everything else. Both modes share the same five tools in `agent/tools.py`.

There are no tests or linters configured. The scripts self-verify: hard asserts for internal consistency (line accounting, pandas-vs-SQL cross-checks, brute-force window scan) and printed `OK/WARN` checks for ground-truth expectations about the shipped data files (45 alerts, 5 malformed lines, 3 episodes, incident timings). A successful run with all `OK` is the regression check.

## Architecture and conventions

`analysis.py` (Part 1) is a single linear pipeline: `load_data` (strict validation — fails fast on missing columns, nulls, duplicate/unsorted timestamps, or gaps in the 15-minute grid) → energy summary → revenue by mode → worst 14-day outage window → SQL insights → chart.

Part 2 is layered: `etl.py` (deterministic parsing; malformed lines are salvaged with a `salvage_note` or quarantined to `rejected_lines`, never silently dropped) → `diagnostics.py` (imports `etl`; episode clustering with a 60-min quiet gap, severity-weighted ranking, issue-type tags, internal/external classification, incident reconstruction) → `agent/tools.py` (imports `diagnostics` for episode/incident logic — DRY) → `agent/harness.py` (LLM loop + fallback router) → `agent/run_demo.py` (writes `agent/transcript.md`).

Part 2 parsing traps that must not regress: module-bearing messages ("Overtemperature fault Module 7: 78.1°C") put the module id BEFORE the value, so `etl.py` uses per-template regexes with separate named groups, never "first number wins"; the log file is UTF-8 (°C is multi-byte) and `diagnostics.py` reconfigures stdout to UTF-8 before printing messages.

Domain conventions that must stay consistent across `analysis.py`, `queries.sql`, `report.md`, and `README.md`:

- **Revenue is NET**: `revenue_gbp = power_mw × 0.25h × price`. Positive power = discharge (earns), negative = charge (costs). Gross (discharge-only) is reported alongside for transparency, but net drives all conclusions.
- The 14-day outage window must lie fully inside Q1 2024 (1 Jan – 31 Mar).
- The dataset is treated as synthetic: after SOC correction the implied round-trip loss is ~zero, which is physically impossible; the report calls this out deliberately.

Coupling between files:

- `queries.sql` is split on the literal string `"Part 2"`: `analysis.py:run_sql()` runs only statements BEFORE the marker (against `bess_readings` in `outputs/bess.db`); `diagnostics.py:run_part2_sql()` runs only statements AFTER it (against `log_events` in `outputs/logs.db`). Both runners split statements on `;` BEFORE stripping `--` comments — so comments in `queries.sql` must never contain a semicolon or an extra occurrence of "Part 2" (this bit once; the file warns about it).
- Table schemas the SQL depends on: `bess_readings` (`timestamp`, `date`, `soc_pct`, `power_mw`, `freq_reg_signal_hz`, `operational_mode`, `market_price_gbp_mwh`, `energy_mwh`, `revenue_gbp`) and `log_events` (`timestamp`, `date`, `hour`, `subsystem`, `severity`, `message`, `message_template`, `metric`, `value`, `unit`, `module`, `salvage_note`, `raw_line_no`).
- `report.md` embeds `outputs/part1_outage_window.png` and `outputs/part2_incident.png`; the numbers in `report.md` and `README.md` are hand-written from script output, so re-check them if analysis logic changes. `outputs/part*_console_output.txt` are saved copies of full runs (committed; regenerate if output changes). `agent/transcript.md` is generated by `python -m agent.run_demo` (regenerate after tool/harness changes; run with `ANTHROPIC_API_KEY` set for an LLM-mode transcript).

`outputs/bess.db`, `outputs/logs.db` and `outputs/log_events.csv` are gitignored (recreated on every run); the PNGs, console outputs and transcript are committed deliverables.
