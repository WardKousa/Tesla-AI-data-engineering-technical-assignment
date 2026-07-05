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
