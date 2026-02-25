# India Grid Optimization LP

This project builds a full-year hourly linear program (8,784 hours) from `Input file.xlsx` and optimizes a least-cost supply mix with:

- Capacity expansion decisions (solar, battery, diesel, CCGT, coal)
- Hourly dispatch decisions
- Battery charge/discharge/SOC dynamics
- Hourly ramp constraints for solar, battery net output, diesel, CCGT, and coal
- High VOLL penalty for unmet demand

## Files

- `optimize_power_lp.py`: main LP model + output export
- `frontend/server.py`: local API + frontend host
- `frontend/templates/index.html`: dashboard shell
- `frontend/static/styles.css`: dashboard styling
- `frontend/static/app.js`: interactive charts and controls
- `outputs/`: generated model results
- `run_non_fossil_scenarios.py`: batch runner for 70/80/90/95/99% non-fossil scenarios

## 1) Run the optimization

```bash
python3 optimize_power_lp.py --input "Input file.xlsx" --output-dir outputs
```

Optional:

```bash
python3 optimize_power_lp.py --voll 12000 --solver-msg
```

Artifacts written to `outputs/`:

- `hourly_dispatch.csv`
- `summary.json`
- `cost_breakdown.csv`
- `assumptions_used.csv`

`cost_breakdown.csv` now includes per-technology cost components:
- `component=capex_annualized`
- `component=fixed_om`
- `component=var_om`
- plus `component=unserved_penalty` as a system row

## 2) Start the frontend

```bash
uvicorn frontend.server:app --reload
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Scenario batch runs (70/80/90/95/99% non-fossil)

```bash
python3 run_non_fossil_scenarios.py --input "Input file.xlsx" --output-root outputs/scenarios
```

This writes:

- `outputs/scenarios/nf70/`
- `outputs/scenarios/nf80/`
- `outputs/scenarios/nf90/`
- `outputs/scenarios/nf95/`
- `outputs/scenarios/nf99/`
- `outputs/scenarios/scenario_index.json`

The frontend automatically detects these scenarios and shows them in a dropdown.

## Notes

- The model is LP (continuous) and does not use unit commitment binaries.
- Ramping is enforced hour-to-hour; startup/min-up/min-down logic is not included.
- Scenario non-fossil targets are enforced as a fossil cap on served demand:
  `diesel + ccgt + coal <= (1 - target) * served_demand`.
- `Annual Primary Generation Mix` excludes battery discharge to avoid double-counting storage throughput.
- CAPEX annualization uses technology-specific lifetimes (`Solar/Battery/Diesel/CCGT/Coal lifetime`) when provided;
  if a specific lifetime is missing, `Project life` is used as fallback.
