"""Occupancy Quant Lab: reproducible research, forecasts, and live forecast scoring."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import joblib
import pandas as pd

from generate_data import generate
from occupancy_core import (read_series, train_backtest_forecast, forecast_from_series,
                            append_forecast_log, evaluate_forecast_log)
from rolling_24h import (fit_calendar_24h, forecast_clock_24h, current_local_time,
                         append_24h_log, evaluate_24h_log)


def plot_metrics(metrics, destination, *, source_label):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    means = metrics.groupby(['horizon_min', 'model'], as_index=False)['mae'].mean()
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for name, label in [('persistence', 'No change'), ('seasonal', 'Seasonal baseline'),
                        ('ridge_level', 'Ridge · level'), ('ridge_delta', 'Ridge · change'),
                        ('boost_level', 'Boost · level'), ('boost_delta', 'Boost · change')]:
        d = means.loc[means['model'] == name]
        ax.plot(d['horizon_min'], d['mae'], marker='o', label=label)
    ax.set(xlabel='Forecast horizon (minutes)', ylabel='MAE (people)',
           title=f'Blind walk-forward accuracy — {source_label} data')
    ax.grid(alpha=.2)
    ax.legend(ncol=2, fontsize=9)
    fig.tight_layout()
    fig.savefig(destination, dpi=160)
    plt.close(fig)


def make_html(series, metrics, backtest, forecast, destination, *, source_label):
    """Stand-alone interactive report, no web server required."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=3, cols=1,
                        subplot_titles=('Recent occupancy and live 60-minute forecast',
                                        'Blind 60-minute backtest (fold 2)',
                                        'Out-of-sample MAE across candidate models'),
                        vertical_spacing=.11)
    sample = series.loc[series.index >= series.index[-1] - pd.Timedelta(hours=36)]
    fig.add_trace(go.Scatter(x=sample.index, y=sample, name='Observed occupancy estimate',
                             line=dict(width=2.5)), row=1, col=1)
    t = pd.to_datetime(forecast['target_time'])
    fig.add_trace(go.Scatter(x=list(t) + list(t)[::-1],
                             y=list(forecast['upper_80']) + list(forecast['lower_80'])[::-1],
                             fill='toself', fillcolor='rgba(40,135,210,.17)',
                             line=dict(width=0), hoverinfo='skip', name='Nominal 80% band'),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=t, y=forecast['expected_people'],
                             name='Selected model', mode='lines+markers',
                             line=dict(width=3, dash='dash')), row=1, col=1)
    bt = backtest[(backtest['horizon_min'] == 60) & (backtest['fold'] == 2)].tail(300)
    fig.add_trace(go.Scatter(x=bt['target_time'], y=bt['actual'],
                             name='Blind actual', mode='lines'), row=2, col=1)
    fig.add_trace(go.Scatter(x=bt['target_time'], y=bt['predicted'],
                             name='Blind selected prediction', mode='lines',
                             line=dict(dash='dot')), row=2, col=1)
    g = metrics.groupby(['horizon_min', 'model'])['mae'].mean().unstack()
    for model in ('persistence','seasonal','ridge_level','ridge_delta','boost_level','boost_delta'):
        fig.add_trace(go.Scatter(x=g.index, y=g[model], name=model,
                                 mode='lines+markers'), row=3, col=1)
    selected = metrics.loc[metrics['selected'] & (metrics['horizon_min'] == 60)]
    mae = selected['mae'].mean()
    cov = selected['interval_coverage'].mean()
    fig.update_layout(template='plotly_white', height=1220,
                      title=f'{source_label.upper()} DATA · chosen-model 60-min MAE {mae:.2f} people · '
                            f'80% band held-out coverage {cov:.1%}',
                      legend=dict(orientation='h', y=-.065),
                      margin=dict(l=65, r=20, t=100, b=95))
    fig.update_yaxes(title_text='People', row=1, col=1)
    fig.update_yaxes(title_text='People', row=2, col=1)
    fig.update_yaxes(title_text='MAE (people)', row=3, col=1)
    fig.update_xaxes(title_text='Forecast horizon (minutes)', row=3, col=1)
    fig.write_html(str(destination), include_plotlyjs=True, full_html=True)
    print('HTML dashboard:', destination)



def make_24h_html(forecast, destination, *, source_label='synthetic'):
    """A standalone chart anchored to the actual computer-local clock."""
    import plotly.graph_objects as go
    fig = go.Figure()
    t = pd.to_datetime(forecast['target_time'], utc=True)
    fig.add_trace(go.Scatter(
        x=list(t) + list(t)[::-1],
        y=list(forecast['upper_80']) + list(forecast['lower_80'])[::-1],
        fill='toself', fillcolor='rgba(40,130,210,.15)',
        line=dict(width=0), name='Historical 80% reference band', hoverinfo='skip'))
    fig.add_trace(go.Scatter(x=t, y=forecast['expected_people'],
                             name='Assumed / projected occupants',
                             line=dict(width=3, color='#287bc1')))
    fig.update_layout(
        template='plotly_white', height=620,
        title=f"NEXT 24 HOURS · {source_label.upper()} HISTORY · "
              f"{forecast.iloc[0]['observation_mode']} · NO LIVE VALIDATION",
        xaxis_title='Time (local clock shown by your browser)',
        yaxis_title='Projected people',
        legend=dict(orientation='h', y=-.18),
        annotations=[dict(x=.01, y=1.10, xref='paper', yref='paper', showarrow=False,
                          text=f"Issued: {forecast.iloc[0]['issued_at']}  |  "
                               "The forecast is an assumed value, NOT an observation.",
                          font=dict(size=12))],
        margin=dict(t=120, b=120))
    fig.write_html(str(destination), include_plotlyjs=True, full_html=True)
    print('24-hour HTML report:', destination)


def issue_day_forecast(calendar, out, *, at=None, observations=None,
                       short_bundle=None, allow_live=True,
                       source_label='synthetic'):
    f = forecast_clock_24h(calendar, at=at, observations=observations,
                           short_bundle=short_bundle, allow_live=allow_live)
    f.to_csv(out / 'forecast_24h.csv', index=False)
    added = append_24h_log(f, out / 'forecast_log_24h.csv')
    make_24h_html(f, out / 'occupancy_24h_dashboard.html', source_label=source_label)
    summary = f.iloc[[0, 11, 35, 71, 143, 287]][['target_time', 'expected_people',
                                                   'lower_80', 'upper_80']]
    print('\nCLOCK-BASED 24-HOUR OCCUPANCY (ASSUMED, NOT OBSERVED):')
    print(summary.to_string(index=False))
    print(f"Mode: {f.iloc[0]['observation_mode']} | "
          f"Logged {added} predictions for later actual validation")
    return f


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    demo = sub.add_parser('demo', help='Generate synthetic data and run research demo')
    demo.add_argument('--weeks', type=int, default=10)
    demo.add_argument('--capacity', type=int, default=32)
    demo.add_argument('--room', default='office_1')
    demo.add_argument('--seed', type=int, default=43)
    train = sub.add_parser('train', help='Train / backtest on existing counts CSV')
    train.add_argument('--data', required=True)
    train.add_argument('--room', required=True)
    train.add_argument('--capacity', required=True, type=int)
    forecast = sub.add_parser('forecast', help='Load saved bundle, predict and log issued forecasts')
    forecast.add_argument('--data', required=True)
    forecast.add_argument('--room', required=True)
    forecast.add_argument('--model', default='outputs/forecast_bundle.joblib')
    day = sub.add_parser('forecast-day', help='From actual room-local clock: predict next 24 h, even without live values')
    day.add_argument('--data', default=None, help='Optional RECENT live observation CSV; omit for no-live mode')
    day.add_argument('--room', default=None, help='Defaults to trained calendar room ID')
    day.add_argument('--model', default='outputs/forecast_bundle.joblib')
    day.add_argument('--calendar-model', default='outputs/calendar_model.joblib')
    day.add_argument('--at', default=None, help='Override clock for tests or historical replay (ISO local time)')
    day.add_argument('--no-live', action='store_true', help='Ignore observations even if recent')
    watch = sub.add_parser('watch-day', help='Keep clock-based 24 h prediction updated each new 5-minute slot')
    watch.add_argument('--data', default=None, help='Optional growing sensor CSV (real readings)')
    watch.add_argument('--room', default=None)
    watch.add_argument('--model', default='outputs/forecast_bundle.joblib')
    watch.add_argument('--calendar-model', default='outputs/calendar_model.joblib')
    watch.add_argument('--no-live', action='store_true')
    watch.add_argument('--poll-seconds', type=int, default=15)
    watch.add_argument('--cycles', type=int, default=None,
                       help='Optional stop after N new five-minute slots (for testing)')
    assess_day = sub.add_parser('evaluate-day', help='Score issued 24 h predictions against LATER REAL data')
    assess_day.add_argument('--data', required=True, help='Actual collected data, never model-generated predictions')
    assess_day.add_argument('--room', default=None)
    assess_day.add_argument('--calendar-model', default='outputs/calendar_model.joblib')
    assess = sub.add_parser('evaluate', help='Score previously issued forecasts against later real readings')
    assess.add_argument('--data', required=True)
    assess.add_argument('--room', required=True)
    for s in (demo, train, forecast, assess, day, assess_day, watch):
        s.add_argument('--out', default='outputs')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.command == 'evaluate-day':
        calendar = joblib.load(args.calendar_model)
        room = args.room or calendar.room_id
        result = evaluate_24h_log(out / 'forecast_log_24h.csv', args.data, room,
                                   out / 'live_evaluation_24h.csv', timezone=calendar.timezone)
        if result.empty:
            print('No actual readings yet for any matured forecast targets. Forecast remains UNVERIFIED.')
        else:
            print(f'Validated {len(result)} matured 24-hour forecast targets against SUPPLIED observations.')
            print('MAE:', round(result['abs_error'].mean(), 3), 'people')
            print('Interval coverage:', round(result['covered_80'].mean(), 3))
        return
    if args.command == 'watch-day':
        if args.poll_seconds < 1 or (args.cycles is not None and args.cycles < 1):
            raise ValueError('poll-seconds and cycles must be positive')
        calendar = joblib.load(args.calendar_model)
        room = args.room or calendar.room_id
        if room != calendar.room_id:
            raise ValueError('Room does not match saved calendar model')
        short = joblib.load(args.model) if args.data and not args.no_live else None
        meta_path = out / 'project_metadata.json'
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        print('Watching actual room-local time. Press Ctrl+C to stop.')
        previous_bucket, issued = None, 0
        try:
            while True:
                now = current_local_time(calendar.timezone)
                bucket = now.floor('5min')
                if bucket != previous_bucket:
                    obs = read_series(args.data, room, fill_short_gaps=False) if args.data else None
                    issue_day_forecast(calendar, out, at=now, observations=obs,
                                       short_bundle=short, allow_live=not args.no_live,
                                       source_label=meta.get('source', 'historical'))
                    if args.data:
                        score = evaluate_24h_log(out / 'forecast_log_24h.csv', args.data,
                                                  room, out / 'live_evaluation_24h.csv',
                                                  timezone=calendar.timezone)
                        print(f'{len(score)} forecasts have actual supplied target readings.')
                    previous_bucket, issued = bucket, issued + 1
                    if args.cycles is not None and issued >= args.cycles:
                        break
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            print('Stopped watching the clock.')
        return
    if args.command == 'forecast-day':
        calendar = joblib.load(args.calendar_model)
        room = args.room or calendar.room_id
        if room != calendar.room_id:
            raise ValueError(f'Calendar room {calendar.room_id!r} != requested {room!r}')
        obs = read_series(args.data, room, fill_short_gaps=False) if args.data else None
        # The normal time-of-week forecast runs without ANY current occupancy reading.
        short = joblib.load(args.model) if args.data and not args.no_live else None
        meta_path = out / 'project_metadata.json'
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        issue_day_forecast(calendar, out, at=args.at, observations=obs,
                           short_bundle=short, allow_live=not args.no_live,
                           source_label=meta.get('source', 'historical'))
        return
    if args.command == 'evaluate':
        result = evaluate_forecast_log(out / 'forecast_log.csv', args.data, args.room,
                                       out / 'live_evaluation.csv')
        print(f'Scored {len(result)} matured forecast targets (not missing observations).')
        if len(result):
            print(result.groupby('horizon_min')[['abs_error', 'covered_80']].mean().round(3).to_string())
        return
    if args.command == 'forecast':
        bundle = joblib.load(args.model)  # load only self-generated / trusted model files
        if args.room != bundle.room_id:
            raise ValueError(f'Model room {bundle.room_id} != requested {args.room}')
        series = read_series(args.data, args.room)
        future = forecast_from_series(series, bundle)
        future.to_csv(out / 'forecast.csv', index=False)
        pd.DataFrame({'timestamp': series.index, 'room_id': args.room, 'count': series.values}).to_csv(out / 'input_history.csv', index=False)
        added = append_forecast_log(future, out / 'forecast_log.csv')
        print(future.to_string(index=False))
        print(f'Logged {added} new issued forecasts to {out / "forecast_log.csv"}')
        return
    if args.command == 'demo':
        path = Path('data/synthetic_occupancy.csv')
        path.parent.mkdir(parents=True, exist_ok=True)
        generate(weeks=args.weeks, capacity=args.capacity, seed=args.seed,
                 room_id=args.room).to_csv(path, index=False)
        print('Generated entirely SYNTHETIC data:', path)
        data, room, capacity = path, args.room, args.capacity
    else:
        data, room, capacity = args.data, args.room, args.capacity
    series = read_series(data, room)
    result = train_backtest_forecast(series, room, capacity, out)
    source_label = 'synthetic' if args.command == 'demo' else 'real / user-supplied'
    pd.DataFrame({'timestamp': series.index, 'room_id': room, 'count': series.values}).to_csv(out / 'input_history.csv', index=False)
    calendar = fit_calendar_24h(series, room, capacity)
    joblib.dump(calendar, out / 'calendar_model.joblib')
    (out / 'project_metadata.json').write_text(json.dumps({'source': source_label, 'room_id': room, 'sampling_minutes': 5}, indent=2))
    metrics = result['metrics']
    comparison = metrics.pivot(index=['fold', 'horizon_min'], columns='model', values='mae')
    selected_mae = metrics[metrics['selected']].set_index(['fold', 'horizon_min'])['mae']
    comparison['chosen_mae'] = selected_mae
    comparison['skill_vs_persistence'] = 1 - comparison['chosen_mae'] / comparison['persistence']
    comparison['skill_vs_seasonal'] = 1 - comparison['chosen_mae'] / comparison['seasonal']
    comparison[['chosen_mae', 'persistence', 'seasonal', 'skill_vs_persistence', 'skill_vs_seasonal']].reset_index().to_csv(out / 'forecast_skill.csv', index=False)
    aggregate = metrics.groupby(['horizon_min', 'model'])['mae'].mean().unstack()
    print('\nBLIND BACKTEST: MAE per candidate (people)')
    print(aggregate.loc[[15, 30, 60]].round(3).to_string())
    chosen = metrics[metrics['selected']]
    print('\nSELECTED-ON-VALIDATION MODELS, assessed on blind test:')
    print(chosen.groupby('horizon_min')[['mae','interval_coverage','interval_mean_width']]
          .mean().loc[[15,30,60]].round(3).to_string())
    print('\nFINAL 60-MINUTE FORECAST:')
    print(result['forecast'].to_string(index=False))
    plot_metrics(metrics, out / 'backtest_mae.png', source_label=source_label)
    make_html(series, metrics, result['backtest'], result['forecast'],
              out / 'occupancy_dashboard.html', source_label=source_label)
    issue_day_forecast(calendar, out, allow_live=False, source_label=source_label)
    print('\nResults:', out.resolve())

if __name__ == '__main__':
    main()
