"""Optional Streamlit dashboard. Run: streamlit run dashboard.py"""
from pathlib import Path
import json
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import joblib
from rolling_24h import (current_local_time, forecast_clock_24h, append_24h_log,
                         evaluate_24h_log)

st.set_page_config(page_title='Occupancy Quant Lab', page_icon='📈', layout='wide')
st.title('Occupancy Quant Lab — clock-based forecast')
st.caption('Forecast the next 24 hours from the CURRENT local clock. Historical predictions are assumptions until later real readings validate them.')
root = Path(__file__).resolve().parent
out = root / 'outputs'
expected = [out/'forecast.csv', out/'backtest_metrics.csv',
            out/'model_selection.csv', out/'input_history.csv']
if not all(p.exists() for p in expected):
    st.error('First run: python run.py demo')
    st.stop()
if st.button('Reload saved predictions'):
    st.rerun()
forecast = pd.read_csv(out/'forecast.csv', parse_dates=['origin_time','target_time'])
metrics = pd.read_csv(out/'backtest_metrics.csv')
selections = pd.read_csv(out/'model_selection.csv')
data = pd.read_csv(out/'input_history.csv', parse_dates=['timestamp'])
if (out/'project_metadata.json').exists():
    metadata = json.loads((out/'project_metadata.json').read_text())
    st.caption(f"Data source: {metadata.get('source', 'unknown')}. A Bluetooth count is not the same as a verified person count.")
# Long-horizon projection is recalculated from the actual wall clock on each UI rerun.
calpath = out / 'calendar_model.joblib'
if calpath.exists():
    calendar = joblib.load(calpath)  # trusted artifact generated locally
    now = current_local_time(calendar.timezone)
    live_path = root / 'data' / 'live_occupancy.csv'
    recent_live = None
    short_bundle = None
    if live_path.exists():
        from occupancy_core import read_series
        recent_live = read_series(live_path, calendar.room_id, fill_short_gaps=False)
        modelpath = out / 'forecast_bundle.joblib'
        if modelpath.exists():
            short_bundle = joblib.load(modelpath)
    day_forecast = forecast_clock_24h(calendar, at=now, observations=recent_live,
                                     short_bundle=short_bundle)
    st.subheader('Next 24 hours — continuously anchored to the actual clock')
    st.caption(f"Current room-local time: {now.strftime('%A %d %B %Y, %H:%M:%S %Z')} · "
               f"Prediction issued from {day_forecast['origin_time'].iloc[0]} · "
               f"Mode: {day_forecast['observation_mode'].iloc[0]}")
    p1, p2, p3 = st.columns(3)
    p1.metric('Next 5-minute assumed count',
              f"{day_forecast['expected_people'].iloc[0]:.1f} people")
    p2.metric('Maximum projected in 24 h',
              f"{day_forecast['expected_people'].max():.1f} people")
    p3.metric('Actual live readings', 'None — unverified' if recent_live is None else 'Provided')
    st.info('Without live sensors, projected occupancy is TREATED AS THE ASSUMED VALUE for simulation. '
            'It is not a measured or independently verified headcount. The blue envelope is '
            'a historically calibrated reference band, not a guarantee for the next day.')
    graph = go.Figure()
    target = pd.to_datetime(day_forecast['target_time'], utc=True).dt.tz_convert(calendar.timezone)
    graph.add_trace(go.Scatter(x=list(target) + list(target)[::-1],
                               y=list(day_forecast['upper_80']) + list(day_forecast['lower_80'])[::-1],
                               fill='toself', fillcolor='rgba(25,115,210,.16)',
                               line=dict(width=0), name='Historical 80% reference band'))
    graph.add_trace(go.Scatter(x=target, y=day_forecast['expected_people'],
                               mode='lines', name='Assumed occupancy forecast',
                               line=dict(width=3)))
    graph.update_layout(xaxis_title='Room-local time', yaxis_title='Expected occupants',
                        title='Rolling 24-hour occupancy forecast')
    st.plotly_chart(graph, use_container_width=True)
    if st.button('Issue and log this 24-hour forecast'):
        day_forecast.to_csv(out / 'forecast_24h.csv', index=False)
        written = append_24h_log(day_forecast, out / 'forecast_log_24h.csv')
        st.success(f'{written} forecast points saved for later validation.')
    st.caption(f"Calendar-only historical calibration: MAE = {calendar.calibration_mae:.2f} "
               f"people; nominal-80% coverage = {calendar.calibration_coverage_80:.0%} "
               f"over {calendar.calibration_points:,} held-out SYNTHETIC samples.")
    if (out / 'live_evaluation_24h.csv').exists():
        scored = pd.read_csv(out / 'live_evaluation_24h.csv')
        if len(scored):
            st.success(f"Later observations validated {len(scored)} previously issued forecast points. "
                       f"MAE: {scored['abs_error'].mean():.2f} people.")
    st.divider()
    st.subheader('Earlier 60-minute model research (historical data, not live right now)')

room = forecast['room_id'].iloc[0]
data = data[data['room_id'] == room].sort_values('timestamp')
chosen = metrics[metrics['selected']]
summary = chosen.groupby('horizon_min')[['mae','interval_coverage']].mean()
q1,q2,q3,q4=st.columns(4)
q1.metric('Last historical example', f"{data['count'].iloc[-1]:.0f}")
q2.metric('60-min forecast', f"{forecast.iloc[-1]['expected_people']:.1f}")
q3.metric('60-min blind MAE', f"{summary.loc[60, 'mae']:.2f} people")
q4.metric('80% interval coverage at 60 min', f"{summary.loc[60, 'interval_coverage']:.0%}")
recent = data[data['timestamp']>=data['timestamp'].max()-pd.Timedelta(hours=36)]
fig=go.Figure()
fig.add_trace(go.Scatter(x=recent['timestamp'],y=recent['count'],name='Historical'))
t=forecast['target_time']
fig.add_trace(go.Scatter(x=list(t)+list(t)[::-1],
                         y=list(forecast['upper_80'])+list(forecast['lower_80'])[::-1],
                         fill='toself',fillcolor='rgba(20,110,210,.16)',line=dict(width=0),
                         name='Nominal 80% interval'))
fig.add_trace(go.Scatter(x=t,y=forecast['expected_people'],name='Predicted',
                         mode='lines+markers',line=dict(dash='dash')))
fig.update_layout(title='Historical example: 5-minute steps over 60 minutes (NOT clock-anchored)',
                  xaxis_title='Time',yaxis_title='Estimated occupants')
st.plotly_chart(fig, use_container_width=True)
st.subheader('Candidate model comparison — blind walk-forward tests')
mean = metrics.groupby(['horizon_min','model'])['mae'].mean().unstack()
plot=go.Figure()
for model in mean.columns:
    plot.add_trace(go.Scatter(x=mean.index,y=mean[model],name=model,mode='lines+markers'))
plot.update_layout(xaxis_title='Minutes ahead',yaxis_title='MAE (people)')
st.plotly_chart(plot,use_container_width=True)
skill_path=out/'forecast_skill.csv'
if skill_path.exists():
    skill=pd.read_csv(skill_path)
    selected_skill=skill.groupby('horizon_min')[['skill_vs_persistence','skill_vs_seasonal']].mean()
    st.subheader('Forecast skill relative to simple baselines')
    st.caption('Positive values indicate MAE improvement; negative values indicate worse performance.')
    st.dataframe(selected_skill.loc[[15,30,60]].style.format('{:.1%}'), use_container_width=True)
st.subheader('Models selected using validation data (not test data)')
st.dataframe(selections,hide_index=True,use_container_width=True)
st.subheader('Live scoring — only after forecasts mature')
scorefile=out/'live_evaluation.csv'
if scorefile.exists():
    scores=pd.read_csv(scorefile)
    if len(scores):
        st.metric('Scored forecast targets',len(scores))
        st.dataframe(scores.groupby('horizon_min')[['abs_error','covered_80']].mean().round(3),
                     use_container_width=True)
    else:
        st.info('No target timestamps have been observed yet.')
else:
    st.info('Issue forecasts with run.py forecast, collect later observations, then run run.py evaluate.')
st.warning('Bluetooth counts devices rather than people. Calibrate against manually verified headcounts. '
           'Reported prediction intervals do not include sensor-to-occupant measurement uncertainty.')
