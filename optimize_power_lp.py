#!/usr/bin/env python3
"""Hourly full-year power system LP with ramping constraints.

The model reads assumptions and profiles from an Excel workbook, optimizes
capacity build-out + dispatch, and writes detailed outputs for a frontend.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
import pulp


GEN_TECHS = ["solar", "diesel", "ccgt", "coal"]
ALL_TECHS = ["solar", "battery", "diesel", "ccgt", "coal"]


def to_fraction(value: float) -> float:
    """Convert percent-like values to fractions when needed."""
    value = float(value)
    return value / 100.0 if value > 1.0 else value


def annualized_capex_per_kw(capex_per_kw: float, wacc: float, life_years: float) -> float:
    """Convert upfront capex ($/kW) into annualized $/kW-yr via CRF."""
    if life_years <= 0:
        return 0.0
    if wacc == 0:
        return capex_per_kw / life_years

    growth = (1 + wacc) ** life_years
    crf = (wacc * growth) / (growth - 1)
    return capex_per_kw * crf


def load_input_data(input_path: Path) -> tuple[pd.DataFrame, dict[str, float], dict[str, Any]]:
    profiles = pd.read_excel(input_path, sheet_name="Profiles")
    costs = pd.read_excel(input_path, sheet_name="Cost assumptions")

    required_cols = {"Date", "Solar profile", "Total Demand (MWh)"}
    missing_cols = required_cols - set(profiles.columns)
    if missing_cols:
        missing = ", ".join(sorted(missing_cols))
        raise ValueError(f"Profiles sheet is missing columns: {missing}")

    # Date values are Excel serials in the provided workbook.
    date_raw = profiles["Date"]
    numeric_date = pd.to_numeric(date_raw, errors="coerce")
    if numeric_date.notna().mean() > 0.9:
        timestamps = pd.to_datetime(numeric_date, unit="D", origin="1899-12-30")
    else:
        timestamps = pd.to_datetime(date_raw, errors="coerce")
    timestamps = timestamps.dt.round("h")

    hourly = pd.DataFrame(
        {
            "timestamp": timestamps,
            "solar_profile": pd.to_numeric(profiles["Solar profile"], errors="coerce").fillna(0.0),
            "demand_mwh": pd.to_numeric(profiles["Total Demand (MWh)"], errors="coerce").fillna(0.0),
        }
    )
    if hourly["timestamp"].isna().any():
        raise ValueError("Failed to parse one or more timestamps in Profiles sheet.")

    assumptions: dict[str, float] = {}
    for _, row in costs.iterrows():
        name = row.get("Assumption")
        value = row.get("Value")
        note = row.get("Unit / Notes")
        if pd.isna(name):
            continue
        key = str(name).strip()
        if pd.isna(value):
            continue
        assumptions[key] = float(value)

    metadata = {
        "hours": int(len(hourly)),
        "start": hourly["timestamp"].min().isoformat(),
        "end": hourly["timestamp"].max().isoformat(),
    }

    return hourly, assumptions, metadata


def need(assumptions: dict[str, float], key: str) -> float:
    if key not in assumptions:
        raise KeyError(f"Missing assumption: {key}")
    return float(assumptions[key])


def build_and_solve(
    hourly: pd.DataFrame,
    assumptions: dict[str, float],
    voll: float,
    solver_msg: bool,
    min_non_fossil_share: float = 0.0,
    scenario_name: str = "default",
) -> dict[str, Any]:
    hours = list(range(len(hourly)))

    solar_capex = need(assumptions, "Solar PV capex")
    solar_fixed_om = need(assumptions, "Solar fixed O&M")
    solar_degradation = to_fraction(need(assumptions, "Solar degradation"))

    battery_capex_per_kwh = need(assumptions, "Battery capex")
    battery_duration = float(assumptions.get("Battery duration", 4.0))
    battery_rte = to_fraction(need(assumptions, "Battery round-trip efficiency"))
    battery_fixed_om = need(assumptions, "Battery fixed O&M")
    battery_degradation = to_fraction(need(assumptions, "Battery degradation"))

    diesel_capex = need(assumptions, "Diesel capex")
    diesel_fixed_om = need(assumptions, "Diesel fixed O&M")
    diesel_var = need(assumptions, "Diesel variable O&M (fuel)") + need(
        assumptions, "Diesel variable O&M (other)"
    )

    ccgt_capex = need(assumptions, "CCGT capex")
    ccgt_fixed_om = need(assumptions, "CCGT fixed O&M")
    ccgt_var = need(assumptions, "CCGT variable O&M (fuel)") + need(
        assumptions, "CCGT variable O&M (other)"
    )

    coal_capex = need(assumptions, "Coal capex")
    coal_fixed_om = need(assumptions, "Coal fixed O&M")
    coal_var = need(assumptions, "Coal variable O&M (fuel)") + need(
        assumptions, "Coal variable O&M (other)"
    )

    wacc = to_fraction(need(assumptions, "Discount rate (WACC)"))

    default_project_life = assumptions.get("Project life")

    def get_lifetime(tech_key: str) -> float:
        specific = assumptions.get(tech_key)
        if specific is not None:
            return float(specific)
        if default_project_life is not None:
            return float(default_project_life)
        raise KeyError(
            f"Missing assumption: {tech_key} (and no fallback 'Project life' provided)."
        )

    technology_lifetimes_years = {
        "solar": get_lifetime("Solar lifetime"),
        "battery": get_lifetime("Battery lifetime"),
        "diesel": get_lifetime("Diesel lifetime"),
        "ccgt": get_lifetime("CCGT lifetime"),
        "coal": get_lifetime("Coal lifetime"),
    }

    ramp_per_hour = {
        "solar": to_fraction(need(assumptions, "Solar PV ramp rate")) * 60.0,
        "battery": to_fraction(need(assumptions, "Battery ramp rate")) * 60.0,
        "diesel": to_fraction(need(assumptions, "Diesel ramp rate")) * 60.0,
        "ccgt": to_fraction(need(assumptions, "CCGT ramp rate")) * 60.0,
        "coal": to_fraction(need(assumptions, "Coal ramp rate")) * 60.0,
    }

    annualized_capex_kw_year = {
        "solar": annualized_capex_per_kw(solar_capex, wacc, technology_lifetimes_years["solar"]),
        "battery": annualized_capex_per_kw(
            battery_capex_per_kwh * battery_duration, wacc, technology_lifetimes_years["battery"]
        ),
        "diesel": annualized_capex_per_kw(diesel_capex, wacc, technology_lifetimes_years["diesel"]),
        "ccgt": annualized_capex_per_kw(ccgt_capex, wacc, technology_lifetimes_years["ccgt"]),
        "coal": annualized_capex_per_kw(coal_capex, wacc, technology_lifetimes_years["coal"]),
    }

    fixed_om_kw_year = {
        "solar": solar_fixed_om,
        # Battery fixed O&M is interpreted as $/kWh-yr and converted to $/kW-yr using duration.
        "battery": battery_fixed_om * battery_duration,
        "diesel": diesel_fixed_om,
        "ccgt": ccgt_fixed_om,
        "coal": coal_fixed_om,
    }

    # Convert each technology into annual fixed $/kW-year.
    fixed_cost_kw_year = {
        tech: annualized_capex_kw_year[tech] + fixed_om_kw_year[tech]
        for tech in ALL_TECHS
    }

    var_om_mwh = {
        "solar": 0.0,
        "battery": 0.0,
        "diesel": diesel_var,
        "ccgt": ccgt_var,
        "coal": coal_var,
    }

    # Small degradation approximation for a single-year horizon.
    solar_available = max(0.0, 1.0 - solar_degradation)
    battery_energy_available = max(0.0, 1.0 - battery_degradation)

    # Split round-trip efficiency equally for charge and discharge legs.
    battery_eff = max(1e-6, min(battery_rte, 1.0))
    eta_charge = math.sqrt(battery_eff)
    eta_discharge = math.sqrt(battery_eff)

    demand = hourly["demand_mwh"].to_list()
    solar_profile = hourly["solar_profile"].to_list()

    problem = pulp.LpProblem("india_grid_hourly_lp", pulp.LpMinimize)

    capacity = {
        tech: pulp.LpVariable(f"capacity_mw_{tech}", lowBound=0, cat="Continuous")
        for tech in ALL_TECHS
    }
    gen = {
        tech: {
            h: pulp.LpVariable(f"gen_mwh_{tech}_{h}", lowBound=0, cat="Continuous")
            for h in hours
        }
        for tech in GEN_TECHS
    }

    battery_charge = {
        h: pulp.LpVariable(f"battery_charge_mwh_{h}", lowBound=0, cat="Continuous")
        for h in hours
    }
    battery_discharge = {
        h: pulp.LpVariable(f"battery_discharge_mwh_{h}", lowBound=0, cat="Continuous")
        for h in hours
    }
    battery_soc = {
        h: pulp.LpVariable(f"battery_soc_mwh_{h}", lowBound=0, cat="Continuous") for h in hours
    }
    battery_net = {
        h: pulp.LpVariable(f"battery_net_mwh_{h}", lowBound=None, cat="Continuous") for h in hours
    }
    unserved = {
        h: pulp.LpVariable(f"unserved_mwh_{h}", lowBound=0, cat="Continuous") for h in hours
    }

    fixed_cost_term = pulp.lpSum(
        capacity[t] * 1000.0 * fixed_cost_kw_year[t] for t in ALL_TECHS
    )
    variable_cost_term = pulp.lpSum(
        gen[t][h] * var_om_mwh[t] for t in GEN_TECHS for h in hours
    )
    unserved_penalty_term = pulp.lpSum(unserved[h] * voll for h in hours)

    problem += fixed_cost_term + variable_cost_term + unserved_penalty_term

    for h in hours:
        problem += (
            gen["solar"][h]
            + gen["diesel"][h]
            + gen["ccgt"][h]
            + gen["coal"][h]
            + battery_discharge[h]
            - battery_charge[h]
            + unserved[h]
            == demand[h]
        ), f"balance_{h}"

        problem += gen["solar"][h] <= capacity["solar"] * solar_profile[h] * solar_available, f"solar_cap_{h}"
        problem += gen["diesel"][h] <= capacity["diesel"], f"diesel_cap_{h}"
        problem += gen["ccgt"][h] <= capacity["ccgt"], f"ccgt_cap_{h}"
        problem += gen["coal"][h] <= capacity["coal"], f"coal_cap_{h}"

        problem += battery_charge[h] <= capacity["battery"], f"battery_charge_cap_{h}"
        problem += battery_discharge[h] <= capacity["battery"], f"battery_discharge_cap_{h}"
        problem += (
            battery_soc[h] <= capacity["battery"] * battery_duration * battery_energy_available
        ), f"battery_energy_cap_{h}"

        prev_h = h - 1 if h > 0 else len(hours) - 1
        problem += (
            battery_soc[h]
            == battery_soc[prev_h]
            + eta_charge * battery_charge[h]
            - (1.0 / eta_discharge) * battery_discharge[h]
        ), f"battery_soc_balance_{h}"

        problem += battery_net[h] == battery_discharge[h] - battery_charge[h], f"battery_net_{h}"

    # Scenario policy: enforce non-fossil target as a fossil cap on served energy.
    # In this workbook, fossil technologies are diesel + CCGT + coal.
    min_non_fossil_share = max(0.0, min(1.0, float(min_non_fossil_share)))
    if min_non_fossil_share > 0:
        fossil_gen = pulp.lpSum(
            gen["diesel"][h] + gen["ccgt"][h] + gen["coal"][h]
            for h in hours
        )
        served_energy = pulp.lpSum(demand[h] - unserved[h] for h in hours)
        fossil_cap = (1.0 - min_non_fossil_share) * served_energy
        problem += (
            fossil_gen <= fossil_cap
        ), "maximum_fossil_share"

    # Hourly ramping constraints.
    for h in range(1, len(hours)):
        for tech in GEN_TECHS:
            ramp = ramp_per_hour[tech]
            problem += (
                gen[tech][h] - gen[tech][h - 1] <= ramp * capacity[tech]
            ), f"ramp_up_{tech}_{h}"
            problem += (
                gen[tech][h - 1] - gen[tech][h] <= ramp * capacity[tech]
            ), f"ramp_down_{tech}_{h}"

        battery_ramp = ramp_per_hour["battery"]
        problem += (
            battery_net[h] - battery_net[h - 1] <= battery_ramp * capacity["battery"]
        ), f"ramp_up_battery_{h}"
        problem += (
            battery_net[h - 1] - battery_net[h] <= battery_ramp * capacity["battery"]
        ), f"ramp_down_battery_{h}"

    solver = pulp.PULP_CBC_CMD(msg=solver_msg)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]

    result = {
        "status": status,
        "objective_usd": float(pulp.value(problem.objective) or 0.0),
        "capacity_mw": {t: float(capacity[t].value() or 0.0) for t in ALL_TECHS},
        "fixed_cost_kw_year": fixed_cost_kw_year,
        "variable_cost_mwh": var_om_mwh,
        "ramp_per_hour_of_capacity": ramp_per_hour,
        "voll_usd_per_mwh": voll,
        "eta_charge": eta_charge,
        "eta_discharge": eta_discharge,
    }

    hourly_out = hourly.copy()
    for tech in GEN_TECHS:
        hourly_out[f"gen_{tech}_mwh"] = [float(gen[tech][h].value() or 0.0) for h in hours]

    hourly_out["battery_charge_mwh"] = [float(battery_charge[h].value() or 0.0) for h in hours]
    hourly_out["battery_discharge_mwh"] = [float(battery_discharge[h].value() or 0.0) for h in hours]
    hourly_out["battery_net_mwh"] = [float(battery_net[h].value() or 0.0) for h in hours]
    hourly_out["battery_soc_mwh"] = [float(battery_soc[h].value() or 0.0) for h in hours]
    hourly_out["unserved_mwh"] = [float(unserved[h].value() or 0.0) for h in hours]

    installed_solar = result["capacity_mw"]["solar"]
    hourly_out["solar_potential_mwh"] = hourly_out["solar_profile"] * installed_solar * solar_available
    hourly_out["solar_curtailment_mwh"] = (
        hourly_out["solar_potential_mwh"] - hourly_out["gen_solar_mwh"]
    ).clip(lower=0.0)

    demand_total = float(hourly_out["demand_mwh"].sum())
    unserved_total = float(hourly_out["unserved_mwh"].sum())
    served_total = max(1e-9, demand_total - unserved_total)

    annual_generation = {
        tech: float(hourly_out[f"gen_{tech}_mwh"].sum()) for tech in GEN_TECHS
    }
    annual_generation["battery_charge"] = float(hourly_out["battery_charge_mwh"].sum())
    annual_generation["battery_discharge"] = float(hourly_out["battery_discharge_mwh"].sum())

    fossil_generation_total = float(
        annual_generation["diesel"] + annual_generation["ccgt"] + annual_generation["coal"]
    )
    achieved_fossil_share_served_primary = float(fossil_generation_total / served_total)
    achieved_non_fossil_share_served_primary = float(1.0 - achieved_fossil_share_served_primary)
    achieved_solar_share_served = float(annual_generation["solar"] / served_total)

    capex_annualized_cost_usd = {
        tech: result["capacity_mw"][tech] * 1000.0 * annualized_capex_kw_year[tech]
        for tech in ALL_TECHS
    }
    fixed_om_cost_usd = {
        tech: result["capacity_mw"][tech] * 1000.0 * fixed_om_kw_year[tech]
        for tech in ALL_TECHS
    }
    var_om_cost_usd = {
        tech: annual_generation.get(tech, 0.0) * var_om_mwh[tech]
        for tech in ALL_TECHS
    }
    fixed_cost_usd = {
        tech: capex_annualized_cost_usd[tech] + fixed_om_cost_usd[tech]
        for tech in ALL_TECHS
    }
    variable_cost_usd = {
        tech: var_om_cost_usd[tech] for tech in GEN_TECHS
    }
    unserved_penalty_usd = unserved_total * voll

    cost_components_by_technology = {
        tech: {
            "capex_annualized_usd": float(capex_annualized_cost_usd[tech]),
            "fixed_om_usd": float(fixed_om_cost_usd[tech]),
            "var_om_usd": float(var_om_cost_usd[tech]),
            "total_usd": float(
                capex_annualized_cost_usd[tech]
                + fixed_om_cost_usd[tech]
                + var_om_cost_usd[tech]
            ),
        }
        for tech in ALL_TECHS
    }
    cost_component_totals = {
        "capex_annualized_usd": float(sum(capex_annualized_cost_usd.values())),
        "fixed_om_usd": float(sum(fixed_om_cost_usd.values())),
        "var_om_usd": float(sum(var_om_cost_usd.values())),
        "unserved_penalty_usd": float(unserved_penalty_usd),
    }

    summary = {
        "scenario_name": scenario_name,
        "status": status,
        "objective_usd": result["objective_usd"],
        "lcoe_usd_per_mwh_served": result["objective_usd"] / served_total,
        "total_demand_mwh": demand_total,
        "served_energy_mwh": served_total,
        "unserved_energy_mwh": unserved_total,
        "min_non_fossil_share_target": min_non_fossil_share,
        "achieved_fossil_share_served_primary": achieved_fossil_share_served_primary,
        "achieved_non_fossil_share_served_primary": achieved_non_fossil_share_served_primary,
        "achieved_solar_share_served": achieved_solar_share_served,
        # Backward-compatible alias now tied to the primary served-energy definition.
        "achieved_non_fossil_share": achieved_non_fossil_share_served_primary,
        "share_metric_definition": (
            "Primary generation share on served demand: "
            "fossil=(diesel+ccgt+coal)/served_demand, "
            "non_fossil=1-fossil. Battery discharge is excluded from share denominator."
        ),
        "capacity_mw": result["capacity_mw"],
        "annual_generation_mwh": annual_generation,
        "cost_components_by_technology": cost_components_by_technology,
        "cost_component_totals": cost_component_totals,
        "fixed_cost_usd": fixed_cost_usd,
        "variable_cost_usd": variable_cost_usd,
        "unserved_penalty_usd": unserved_penalty_usd,
        "total_fixed_cost_usd": float(sum(fixed_cost_usd.values())),
        "total_variable_cost_usd": float(sum(variable_cost_usd.values())),
        "voll_usd_per_mwh": voll,
        "wacc_fraction": wacc,
        "project_life_years": None if default_project_life is None else float(default_project_life),
        "technology_lifetimes_years": technology_lifetimes_years,
        "capacity_factor_constraints_applied": False,
        "battery": {
            "duration_hours": battery_duration,
            "round_trip_efficiency": battery_rte,
            "charge_efficiency": eta_charge,
            "discharge_efficiency": eta_discharge,
        },
        "ramp_rate_per_min_fraction_of_capacity": {
            "solar": to_fraction(need(assumptions, "Solar PV ramp rate")),
            "battery": to_fraction(need(assumptions, "Battery ramp rate")),
            "diesel": to_fraction(need(assumptions, "Diesel ramp rate")),
            "ccgt": to_fraction(need(assumptions, "CCGT ramp rate")),
            "coal": to_fraction(need(assumptions, "Coal ramp rate")),
        },
        "hours_modeled": len(hours),
        "timestamp_start": hourly_out["timestamp"].iloc[0].isoformat(),
        "timestamp_end": hourly_out["timestamp"].iloc[-1].isoformat(),
    }

    return {
        "hourly": hourly_out,
        "summary": summary,
        "assumptions": assumptions,
    }


def write_outputs(result: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    hourly_path = output_dir / "hourly_dispatch.csv"
    summary_path = output_dir / "summary.json"
    assumptions_path = output_dir / "assumptions_used.csv"
    costs_path = output_dir / "cost_breakdown.csv"

    hourly = result["hourly"].copy()
    hourly["timestamp"] = pd.to_datetime(hourly["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    hourly.to_csv(hourly_path, index=False)

    summary = result["summary"]
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    assumptions_items = sorted(result["assumptions"].items(), key=lambda x: x[0].lower())
    pd.DataFrame(assumptions_items, columns=["assumption", "value"]).to_csv(assumptions_path, index=False)

    cost_rows = []
    component_to_bucket = {
        "capex_annualized": "fixed",
        "fixed_om": "fixed",
        "var_om": "variable",
    }
    for tech, parts in summary["cost_components_by_technology"].items():
        for component in ["capex_annualized", "fixed_om", "var_om"]:
            key = f"{component}_usd"
            cost_rows.append(
                {
                    "bucket": component_to_bucket[component],
                    "technology": tech,
                    "component": component,
                    "cost_usd": float(parts.get(key, 0.0)),
                }
            )
    cost_rows.append(
        {
            "bucket": "penalty",
            "technology": "system",
            "component": "unserved_penalty",
            "cost_usd": summary["unserved_penalty_usd"],
        }
    )
    pd.DataFrame(cost_rows).to_csv(costs_path, index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run full-year power LP with ramping constraints.")
    parser.add_argument(
        "--input",
        default="Input file.xlsx",
        help="Path to workbook with 'Profiles' and 'Cost assumptions' sheets.",
    )
    parser.add_argument("--output-dir", default="outputs", help="Directory for result artifacts.")
    parser.add_argument(
        "--voll",
        type=float,
        default=10000.0,
        help="Value of lost load penalty in $/MWh (forces high reliability).",
    )
    parser.add_argument(
        "--min-non-fossil-share",
        type=float,
        default=0.0,
        help="Minimum non-fossil generation share (0 to 1).",
    )
    parser.add_argument(
        "--scenario-name",
        default="default",
        help="Scenario name recorded in summary output.",
    )
    parser.add_argument("--solver-msg", action="store_true", help="Show CBC solver log output.")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)

    hourly, assumptions, metadata = load_input_data(input_path)
    print(
        f"Loaded {metadata['hours']} hours from {metadata['start']} to {metadata['end']} "
        f"from {input_path}"
    )

    result = build_and_solve(
        hourly,
        assumptions,
        voll=args.voll,
        solver_msg=args.solver_msg,
        min_non_fossil_share=args.min_non_fossil_share,
        scenario_name=args.scenario_name,
    )

    status = result["summary"]["status"]
    print(f"Solver status: {status}")
    if status != "Optimal":
        print("Warning: Solution is not Optimal. Outputs are still being written for debugging.")

    write_outputs(result, output_dir)
    print(f"Wrote results to {output_dir.resolve()}")


if __name__ == "__main__":
    main()
