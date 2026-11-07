# Occupancy Quant Lab — clock-based 24-hour occupancy forecasting

A complete, reproducible **Python project** for forecasting occupancy from timestamped Bluetooth-derived estimates. It includes a **new clock-anchored 24-hour forecast** (288 five-minute points) as well as the original 5–60-minute ML research models. Compares **six candidate forecasting strategies** and demonstrates the selection, uncertainty calibration, and evaluation workflow used in robust time-series research.

> **Important:** The included 10-week dataset is entirely **synthetic**. The demo verifies that the code works; its MAE scores **do not** establish real-world predictive accuracy. Current counts refer to estimated occupants, not verified people.

## Quick start

From this project directory:

```bash
python -m pip install -r requirements.txt
python run.py demo
python -m unittest discover -s tests -v
```

The `demo` command regenerates `data/synthetic_occupancy.csv` (10 weeks, 5-minute intervals, 32-person capacity, reproducible seed), fits six candidates for each 5–60-minute horizon, and runs two chronological walk-forward tests.

Open **`outputs/occupancy_24h_dashboard.html`** in your browser to see the next 24 hours starting from **the actual computer time**, not the end of the synthetic CSV. It is self-contained and needs no server. `outputs/occupancy_dashboard.html` remains the older historical research report.

### Generate a new 24-hour forecast using the current clock

```bash
python run.py forecast-day
```

This reads the saved weekday/time-of-day model, queries the local clock in `Europe/London`, and writes:

- `outputs/forecast_24h.csv`: 288 projected values, one every five minutes for the next 24 hours.
- `outputs/occupancy_24h_dashboard.html`: clock-aligned standalone graph with an approximate 80% historical reference band.
- `outputs/forecast_log_24h.csv`: an issued forecast log, preserved for later honest evaluation. Re-running within the same five-minute window does not duplicate rows.

**No live sensor data is required.** The field `assumed_people_for_simulation` is the forecast value **used as though it were true** for a prototype or downstream scenario. `observed_people` is intentionally blank and `validation_status` is `awaiting_real_observation`: this is a model assumption, **not a verified measurement**.

Refresh the Streamlit dashboard or run `forecast-day` again to recalculate against the clock. To **automatically reissue a rolling 24-hour forecast** at each new five-minute interval, run this long-lived local process (Ctrl+C stops it):

```bash
python run.py watch-day
```

When real Bluetooth readings eventually become available, use `python run.py watch-day --data data/live_occupancy.csv` to also score matured forecasts automatically. This runs only while your computer/process stays on; it does not create a cloud job. To replay an exact known time for testing, use `python run.py forecast-day --at '2026-11-09 09:00'`.

### Optional: add real Bluetooth readings later

```bash
python run.py forecast-day --data data/live_occupancy.csv
python run.py evaluate-day --data data/live_occupancy.csv
```

If the newest sample is within ten minutes of the actual clock **and** it includes the history required for short-term lag features, the existing 5–60-minute ML models anchor the first hour. The correction then fades into the historical calendar forecast over the longer horizon. If readings are absent, old, or insufficient for the ML model, the system transparently falls back to the calendar expectation.

`evaluate-day` only scores predictions against values actually present in the supplied observation CSV at mature target timestamps. **It will never substitute a prediction as ground truth**, and it reports unverified forecasts when no observations are available. Do not pass the synthetic historical CSV as if it were real sensor readings.

The 24-hour model uses a smoothed weekday × five-minute historical median, fitted on earlier weeks and calibrated on a later 14-day interval. The 80% band comes from that separate historical calibration period, not from claims about future sensor accuracy. For a full day without live data this is intentionally a **rough seasonal projection**, because the 60-minute ML models are not proven at 24-hour horizons. It is not an exact 80% coverage guarantee on live observations.

For an optional Streamlit view:

```bash
pip install streamlit
streamlit run dashboard.py
```

## Key architecture decisions

See [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) for the detailed explanation and mathematical justification.

| Component | Choice | Reason |
|---|---|---|
| Forecasting baseline | No change + seasonal profile | A simple benchmark is essential; changes over 5 min are often small |
| Statistical model | Ridge autoregression | Regularised, interpretable model of occupancy lags and time features |
| Nonlinear model | Gradient-boosted trees | Captures relationships beyond a linear model without neural-network training costs |
| Targets | Future level **and** future occupancy change | Tests whether forecasting net arrivals is more effective than forecasting absolute headcount |
| Model selection | Separate historical validation slice | Prevents choosing the winner using the held-out test labels |
| Confidence bands | 80% split-conformal calibration | Explicit, testable estimate of future errors; calibrated after model choice |
| Evaluation | Two walk-forward blind test windows | Defends against time leakage and unrepresentative random train/test splits |
| Production monitoring | Persist every forecast and later score it | Measures whether live error drifts as more real data arrives |

A different candidate may win at each 5-minute horizon. The algorithm chooses by *selection-period* MAE, not by the final test results.

## Files

```text
occupancy_forecaster/
├── data/synthetic_occupancy.csv       # REPRODUCIBLE SIMULATED INPUT
├── generate_data.py                   # Generator for artificial office patterns
├── occupancy_core.py                  # All ML models, selection, calibration, scoring
├── rolling_24h.py                     # Clock, 24h model, logging and validation
├── run.py                             # CLI: demo | train | forecast | evaluate
├── dashboard.py                       # Streamlit UI with refreshed 24h forecast
├── DESIGN_DECISIONS.md                # Explanation of each modelling decision
├── tests/                              # Leakage, idempotency, forecasting tests
└── outputs/
    ├── forecast_bundle.joblib          # Models, horizons, selected candidates, radii
    ├── calendar_model.joblib           # Clock-based long-horizon seasonal model
    ├── forecast_24h.csv                # 288 forecast values from NOW forward
    ├── forecast_log_24h.csv            # Issued 24h prediction history for later validation
    ├── occupancy_24h_dashboard.html    # Current-clock standalone 24h chart
    ├── input_history.csv               # Aggregated readings used by the dashboard
    ├── project_metadata.json           # Synthetic vs user-supplied data source
    ├── forecast.csv                    # Current 5/10/.../60 min forecast
    ├── model_selection.csv             # Selected model + selection MAE by horizon
    ├── forecast_skill.csv              # Improvement relative to simple baselines
    ├── backtest_metrics.csv            # Every candidate / fold / horizon
    ├── backtest_predictions.csv        # Held-out predictions and actuals
    ├── backtest_mae.png                # Candidate error curves
    ├── occupancy_dashboard.html        # Stand-alone interactive report
    ├── forecast_log.csv                # Created on first `forecast` invocation
    └── live_evaluation.csv             # Created on first `evaluate` invocation
```

## Use data from your sensors

Aggregate your sensors' calibrated estimates into a CSV:

```csv
timestamp,room_id,count
2026-11-07 09:00:00,office_1,3
2026-11-07 09:05:00,office_1,5
2026-11-07 09:10:00,office_1,7
```

Use one consistent time zone, avoid ambiguous DST timestamps, and maintain regular 5-minute readings. Multiple detections within a 5-minute interval are aggregated by median. Missing intervals of **at most 10 minutes** can be forward-filled for input features; longer outages are left missing. The current pipeline expects around **four or more weeks** of training data because it uses previous-week lag features and separate chronological fitting windows.

```bash
python run.py train --data path/to/readings.csv --room office_1 --capacity 32
```

The program saves a bundle and runs research backtests. Every 5 minutes, when the next sensor reading is available, generate a new 60-minute trajectory:

```bash
python run.py forecast --data path/to/updated_readings.csv --room office_1
```

The legacy `forecast` command saves `outputs/forecast.csv` **and appends the 12 future predictions to** `outputs/forecast_log.csv` (with stable origin/target timestamps and model names). It does not retrain automatically.

**After later real observations arrive**, score any past predictions whose targets have occurred:

```bash
python run.py evaluate --data path/to/updated_readings.csv --room office_1
```

This writes `outputs/live_evaluation.csv`, with actual occupancy, signed error, absolute error and 80% interval hit. Predictions with targets in the future, or whose real target observation is missing, are not scored. Rerunning evaluation is safe: results are reconstructed from stored logs and observed data.

The trained bundle is a Python `joblib` pickle. Load **only models you generated yourself or from a trusted source**, not arbitrary downloaded model files. Models from the old project version are not compatible with this updated bundle format; retrain with `demo` or `train`.

## Interpreting the example scores

The synthetic demo often selects **persistence at +5 min** and **boosting at longer horizons**; it is a genuine outcome of selection, not a hardcoded choice. The held-out dataset is still artificially structured. A real office could behave differently, and a sophisticated model can lose to simple baselines.

The conformal intervals represent errors in predicted input counts and do **not** include additional uncertainties from mapping detectable Bluetooth devices to people. For reliable deployment, periodically compare detections with **manual headcounts** and recalibrate the sensing and forecasting chain. Do not use uncalibrated headcounts for safety-critical monitoring.

## Potential later extension

Once occupancy forecasts are validated on real data, the forecast trajectory could be an input to a separate room CO₂ model requiring room volume, ventilation and metabolic assumptions. **No CO₂ predictions or safety conclusions are included here.**
