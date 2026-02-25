#!/usr/bin/env python3
"""Run multiple non-fossil minimum-share scenarios and export scenario index."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from optimize_power_lp import build_and_solve, load_input_data, write_outputs


DEFAULT_SCENARIOS: list[tuple[str, float]] = [
    ("nf70", 0.70),
    ("nf80", 0.80),
    ("nf90", 0.90),
    ("nf95", 0.95),
    ("nf99", 0.99),
]


def parse_scenarios(raw: str | None) -> list[tuple[str, float]]:
    if not raw:
        return DEFAULT_SCENARIOS

    scenarios: list[tuple[str, float]] = []
    for entry in raw.split(","):
        piece = entry.strip()
        if not piece:
            continue

        if ":" in piece:
            name, value = piece.split(":", 1)
            share = float(value)
            scenarios.append((name.strip(), share))
        else:
            pct = float(piece)
            if pct > 1:
                pct = pct / 100.0
            name = f"nf{int(round(pct * 100))}"
            scenarios.append((name, pct))

    if not scenarios:
        raise ValueError("No scenarios parsed from --scenarios value.")
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description="Run >70/80/90/95/99% non-fossil scenarios.")
    parser.add_argument("--input", default="Input file.xlsx", help="Path to workbook input.")
    parser.add_argument(
        "--output-root",
        default="outputs/scenarios",
        help="Root directory where scenario output folders are created.",
    )
    parser.add_argument(
        "--scenarios",
        default=None,
        help=(
            "Comma-separated scenarios. Example: 'nf70:0.7,nf80:0.8' or '70,80,90'. "
            "Default runs nf70,nf80,nf90,nf95,nf99."
        ),
    )
    parser.add_argument(
        "--voll",
        type=float,
        default=10000.0,
        help="Value of lost load penalty in $/MWh.",
    )
    parser.add_argument("--solver-msg", action="store_true", help="Show CBC solver log output.")
    args = parser.parse_args()

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    hourly, assumptions, metadata = load_input_data(Path(args.input))
    print(
        f"Loaded {metadata['hours']} hours from {metadata['start']} to {metadata['end']} "
        f"from {args.input}"
    )

    scenarios = parse_scenarios(args.scenarios)
    index_rows: list[dict[str, object]] = []

    for name, share in scenarios:
        scenario_name = name.strip()
        threshold = max(0.0, min(1.0, float(share)))
        min_share = threshold

        print(f"Running scenario {scenario_name} (min_non_fossil_share={min_share:.2%})")
        result = build_and_solve(
            hourly,
            assumptions,
            voll=args.voll,
            solver_msg=args.solver_msg,
            min_non_fossil_share=min_share,
            scenario_name=scenario_name,
        )

        scenario_dir = output_root / scenario_name
        write_outputs(result, scenario_dir)

        summary = result["summary"]
        row = {
            "id": scenario_name,
            "label": f">={int(round(threshold * 100))}% non-fossil",
            "threshold_non_fossil_share": threshold,
            "enforced_min_non_fossil_share": min_share,
            "min_non_fossil_share": min_share,
            "achieved_fossil_share_served_primary": summary["achieved_fossil_share_served_primary"],
            "achieved_non_fossil_share_served_primary": summary["achieved_non_fossil_share_served_primary"],
            "achieved_solar_share_served": summary["achieved_solar_share_served"],
            "achieved_non_fossil_share": summary["achieved_non_fossil_share"],
            "status": summary["status"],
            "lcoe_usd_per_mwh_served": summary["lcoe_usd_per_mwh_served"],
            "objective_usd": summary["objective_usd"],
            "output_dir": str(scenario_dir.resolve()),
        }
        index_rows.append(row)

        print(
            f"  status={row['status']} lcoe=${row['lcoe_usd_per_mwh_served']:.2f}/MWh "
            f"primary_non_fossil_share={row['achieved_non_fossil_share_served_primary']:.2%}"
        )

    index_payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_file": str(Path(args.input).resolve()),
        "hours": metadata["hours"],
        "scenarios": index_rows,
    }

    index_path = output_root / "scenario_index.json"
    index_path.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")
    print(f"Wrote scenario index to {index_path.resolve()}")


if __name__ == "__main__":
    main()
