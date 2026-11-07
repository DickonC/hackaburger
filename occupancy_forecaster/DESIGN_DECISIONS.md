# Engineering decisions — occupancy forecasting

## Purpose

Build a **small quantitative research system**, not a replica of a proprietary trading platform. The methodology is familiar in quantitative forecasting: noisy time series, explicit reference models, carefully specified targets, separate model-selection and scoring periods, honest uncertainty measurements, and logging predictions **before** outcomes are observed. The synthetic demonstration is for verifying software, *not evidence of real-world predictive performance*.

## Model choices and rationale

| Decision | Why | What we deliberately avoid |
|---|---|---|
| Keep the **persistence** baseline | Over 5 minutes, occupancy often barely changes. An ML model should beat this before being considered useful. | Assuming more complex models must win |
| Keep a **time-of-week seasonal** baseline | Office attendance follows daily/weekly cycles; this checks whether complex models learn more than a calendar. | Comparing only against an unrealistically weak baseline |
| Add **Ridge autoregression** | A linear, regularised model is easy to explain and provides a strong statistical comparison against the nonlinear model. Inputs include lags, momentum, rolling statistics and calendar signals. | Large ARIMA search and bespoke optimisation with little data |
| Retain **gradient-boosted trees** | Efficient, strong on tabular nonlinear data; doesn't require large neural-network training sets. | Deep learning without evidence it beats simple models |
| Compare **level versus change targets** | Direct levels predict future headcount; delta targets predict future headcount **minus current headcount**. Difference models may learn arrival/departure dynamics more naturally. Test, don't assume. | Claiming either formulation is always best |
| Train **direct multi-horizon** models | A separate predictor at each +5, +10, ..., +60 min avoids recursively feeding one model's mistakes back into itself. | Complex sequence models before baseline validation |
| Choose candidates on a separate **selection window** | Avoids picking the winner based on final test results (selection bias). Model weights and scaling are fitted only on training data. | Random shuffling of time-series rows or selecting from the blind test |
| Use a separate **conformal calibration window** | The winner's errors calibrate an empirical symmetric 80% range without using test labels. | Falsely claiming the interval is automatically an exact 80% guarantee |
| Run **disjoint chronological test windows** | Two walk-forward folds expose drift across different dates and test all candidates on the same future periods. | One lucky random split and peeking at future target labels |
| **Log predictions before scoring** | Logs form an immutable record of issued predictions. Once the target time is observed, MAE and band coverage can be computed honestly. | Scoring a hindsight prediction as if it was made live |
| **Do not yet add a Kalman filter** | Count-to-person error is not necessarily zero-mean; Kalman smoothing cannot fix systematic Bluetooth bias and may lag real arrivals. First calibrate sensor counts against ground truth. | A complicated signal pipeline that cannot be validated |

## Target and time features

At a timestamp `t`, models receive current occupancy; lags 5, 15, 30, 60, 120 minutes, previous day and previous week; short rolling means/std; 15- and 60-minute changes; cyclic time of day and week; weekend indicator. Every feature has timestamp **≤ t**.

For a horizon `h`, the two ML target definitions are:

- **Level:** `y_t(h) = N_(t+h)`.
- **Change:** `d_t(h) = N_(t+h) - N_t`. Inference restores the level by `N_t + predicted_change`.

Every training label must fall entirely **before the fit period ends**; similarly, selection/calibration/test labels must fall within their respective chronological windows. Forecasts are clipped to `[0, room_capacity]`.

### Research protocol

In each of two walk-forward folds:

1. Train models and a seasonal reference on an initial historical window.
2. Forecast later **selection** observations and select the lowest-MAE candidate separately for each horizon. If tied, prefer the simpler candidate.
3. Measure the winner's absolute residuals on a **different calibration** window and estimate the 80% conformal interval width.
4. Score all six candidates and the selected winner on a **new blind test** window.

The two test windows are disjoint. After analysis, the saved deployment bundle is trained using the earliest 80% of available data; model selection uses the following 10%, and interval calibration the final 10%. The saved model **is not evaluated on its own selection/calibration data**. Its estimated real-world performance comes from the earlier blind tests.

**Interpretation of 80% intervals:** symmetric split-conformal intervals have ideal coverage under exchangeability; the presence of strong temporal dependence, drift, occupancy limits and sensor miscounts weakens any formal guarantee. Report observed held-out coverage rather than treating 80% as assured.

## What a research reviewer should examine

- `outputs/backtest_metrics.csv`: every candidate, test MAE/RMSE, fold, horizon, which candidate was selected; band coverage and width for selected candidate.
- `outputs/forecast_skill.csv`: improvement in MAE relative to persistence and seasonal references; negative values indicate the chosen model performs worse.
- `outputs/model_selection.csv`: independently selected candidate for each prediction horizon in the final model.
- `outputs/backtest_predictions.csv`: actual and model-predicted values at each blind test timestamp, useful for residual plots and error analysis.
- `outputs/forecast_log.csv`: every *issued* live forecast, including its origin, target, model, estimate and interval. Repeated identical origin/horizon/model submissions are deduplicated.
- `outputs/live_evaluation.csv`: produced only once later observations become available. Missing readings are **not** forward-filled for scoring.

Model selection is done using **validation MAE**, not test MAE. The difference matters: selecting winners by their performance on the reported test data would inflate the claimed accuracy.

## What needs to change before deploying with Arduino/Bluetooth

1. Calibrate Bluetooth counts versus human-labelled ground-truth occupancy. Phones/devices are not people; multipath, multiple devices, randomised BLE identifiers, and observations through walls all matter.
2. Collect sufficient observations (the current 7-day lag plus four-way time splits requires about four weeks of regular data, with more preferred), with reliable timestamps and room IDs.
3. Validate on *new real weeks*, including unexpected meetings, room closures, sensor outages and non-working days. The included generator is a deterministic simulation with office-like recurring patterns.
4. Establish uncertainty on **actual people**, not merely around noisy observed device counts; end-to-end calibration should be revisited when the sensor model changes.
5. Use the output for non-safety-critical planning. Don't use this software as an emergency occupancy counter, regulatory ventilation controller or reliable CO₂ measurement.

## Scope discipline

We intentionally did **not** add LSTM/Transformers, a complex ensemble, a real-time Kalman tracker or a CO₂ predictor. They all introduce additional assumptions and failure modes. The existing candidate comparison is the best defensible technical depth for a small prototype. CO₂ forecasting is a separate later model requiring room volume, ventilation rate, and respiratory generation assumptions or measurements.


## New: Why the clock-based 24-hour forecast uses a different model

The earlier ensemble candidates were **only trained and validated at horizons of +5 to +60 minutes**. Claiming that these directly predict +24 hours would be extrapolating far beyond their evidence. Training hundreds of separate boosted trees for every five-minute horizon is disproportionately expensive for a hackathon, and with only ten weeks of synthetic data is likely to overfit.

Instead, the new `rolling_24h.py` fits a **calendar-conditional weekday/time-of-day reference**. For each weekday and five-minute slot it computes the historical median, then gently smooths adjacent slots to avoid unrealistic spikes due to the small training sample. When asked for a forecast, it reads the **actual local computer time**, rounds down to the current five-minute bucket, and projects the next 288 five-minute slots. This means a Saturday evening correctly leads to Sunday expectations, regardless of the synthetic dataset's final timestamp.

This is a **deliberate, defensible multi-timescale design**:

1. **No live measurements**: A 24-hour seasonal expectation is the best available *assumption*; it is not an observed count or guaranteed truth.
2. **Fresh live measurements later**: The existing horizon-specific ML forecasts take priority for the next 60 minutes, *provided the last reading is fresh and its lag history is sufficient*.
3. **Longer horizons**: The difference between the +60-minute ML forecast and the calendar pattern fades exponentially with a 90-minute time constant. Avoids simply carrying today's occupancy anomaly across the entire following day.
4. **Historical 80% reference band**: Absolute residuals on the most recent 14 days, withheld from calendar-profile fitting, set a single conformal-style error radius. This measures *historical variability*. It does **not** validate a future daily trajectory, handle Bluetooth bias, or guarantee 80% live coverage.
5. **Forecast/observation separation**: `expected_people` is a predicted number, `assumed_people_for_simulation` is the rounded value downstream software can use when the user chooses to assume the forecast is true, and `observed_people` is left empty until genuine later measurements arrive. The issued forecast is logged before any later scores are computed. Never feed assumed occupancy back into the score as an actual observation.
6. **Actual clock and DST**: Internally the future 288 timestamps are timezone-aware (`Europe/London` by default), so even on the DST transition the horizon lasts 24 elapsed hours. For real sensor deployment, prefer ISO 8601 timestamps with timezone offsets to avoid duplicated wall-clock times.

### Current research limitations

- The sample data follow an intentionally predictable synthetic weekly office rhythm. Forecasting a genuine unknown next day remains unvalidated.
- With no Bluetooth observations, the 24-hour projection is a **scenario assumption**. There is no honest measured error or live accuracy number to report.
- The long-horizon reference band is calibrated on past points (not conditional on forecast horizon) and should be described as *indicative*. Review its coverage after real observations arrive.
- Opening a window should not be controlled from these occupancy assumptions alone. The later CO₂ prediction task needs actual ventilation and room parameters and preferably a CO₂ sensor.
