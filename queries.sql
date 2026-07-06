-- ============================================================
-- Part 1: BESS revenue insights (SQLite)
-- Table: bess_readings, one row per 15-minute interval with
--   timestamp TEXT, date TEXT, soc_pct REAL, power_mw REAL,
--   freq_reg_signal_hz REAL, operational_mode TEXT,
--   market_price_gbp_mwh REAL, energy_mwh REAL, revenue_gbp REAL
-- revenue_gbp is NET: power_mw * 0.25h * price, so discharge
-- earns (+) and charging costs (-).
-- Statements are separated by semicolons and run in order by analysis.py.
-- ============================================================

-- 1. Total revenue per month
SELECT
    strftime('%Y-%m', timestamp) AS month,
    ROUND(SUM(revenue_gbp), 2)  AS total_revenue_gbp
FROM bess_readings
GROUP BY month
ORDER BY month;

-- 2a. Highest-revenue day
SELECT date, ROUND(SUM(revenue_gbp), 2) AS daily_revenue_gbp
FROM bess_readings
GROUP BY date
ORDER BY daily_revenue_gbp DESC
LIMIT 1;

-- 2b. Lowest-revenue day
SELECT date, ROUND(SUM(revenue_gbp), 2) AS daily_revenue_gbp
FROM bess_readings
GROUP BY date
ORDER BY daily_revenue_gbp ASC
LIMIT 1;

-- 3. Average market price during Peak Shaving
SELECT ROUND(AVG(market_price_gbp_mwh), 2) AS avg_price_peak_shaving_gbp_mwh
FROM bess_readings
WHERE operational_mode = 'Peak Shaving';

-- ============================================================
-- Part 2: Megapack log-event insights (SQLite, outputs/logs.db)
-- Table: log_events, one row per parsed log line with
--   timestamp TEXT, date TEXT, hour INT, subsystem TEXT,
--   severity TEXT, message TEXT, message_template TEXT (numbers
--   replaced by N), metric TEXT, value REAL, unit TEXT,
--   module INT, salvage_note TEXT, raw_line_no INT
-- NOTE: analysis.py runs only the statements above this marker.
-- diagnostics.py runs only the statements below it. Comments in this
-- section must not contain semicolons (the runners split on them).
-- ============================================================

-- 1. Event counts by subsystem and severity
SELECT subsystem, severity, COUNT(*) AS n_events
FROM log_events
GROUP BY subsystem, severity
ORDER BY subsystem, severity;

-- 2. Calendar-date hour with the most WARNING/ERROR/CRITICAL activity
SELECT date, hour, COUNT(*) AS n_alerts
FROM log_events
WHERE severity IN ('WARNING', 'ERROR', 'CRITICAL')
GROUP BY date, hour
ORDER BY n_alerts DESC, date, hour
LIMIT 1;

-- 3. First and last occurrence of each alert type, read broadly as every
--    distinct WARNING/ERROR/CRITICAL message template (some occur once)
SELECT message_template, severity, COUNT(*) AS n,
       MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen
FROM log_events
WHERE severity IN ('WARNING', 'ERROR', 'CRITICAL')
GROUP BY message_template, severity
ORDER BY first_seen;

-- 4. Mean temperature recorded by the Thermal subsystem at WARNING or
--    ERROR severity. The metric filter keeps only rows whose value is a
--    temperature in degrees C, excluding coolant-flow and fan-speed rows.
SELECT ROUND(AVG(value), 2) AS mean_thermal_alert_temp_c,
       COUNT(*) AS n_rows
FROM log_events
WHERE subsystem = 'Thermal'
  AND severity IN ('WARNING', 'ERROR')
  AND metric = 'temperature';
