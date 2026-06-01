# Pine Strategy Optimizer

This folder includes a local optimizer for the TradingView strategy from `keltner.pine`.

## Run

```bash
cd /home/pi5/proiecte/trading
source venv/bin/activate
python3 misc/pine_optimizer.py \
  --trials 200 \
  --session regular \
  --feed sip \
  --report-json misc/optimizer_report_latest.json \
  --top-csv misc/optimizer_top_latest.csv
```

## Inputs

- Strategy source: `misc/keltner.pine`
- Optional TradingView export reference: `misc/Keltner_channel_strategy_stocks_NYSE_TSM_2026-06-02.xlsx`

## Outputs

- JSON report: detailed metrics and top configurations
- CSV: ranked configurations by optimization score

Generated reports are ignored by git (`misc/optimizer_report*.json`, `misc/optimizer_top*.csv`).

## Notes

- This is an emulator for TradingView behavior, not a 1:1 engine clone.
- Data feed differences (`iex` vs `sip`) can change metrics significantly.
- By default, trailing offset is fixed to `4` ticks to match the current Pine script.
- By default, `outer_kc_mult` is searched on integer values to match `input(2, ...)` from Pine.
- Use `--bars-csv` for reproducible backtests across environments.
