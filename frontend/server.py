#!/usr/bin/env python3
"""Frontend server for power optimization outputs."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
OUTPUT_DIR = Path(os.getenv("POWER_RESULTS_DIR", ROOT_DIR / "outputs"))
SCENARIO_ROOT = OUTPUT_DIR / "scenarios"
SCENARIO_INDEX_FILE = SCENARIO_ROOT / "scenario_index.json"
INPUT_WORKBOOK = Path(os.getenv("POWER_INPUT_FILE", ROOT_DIR / "Input file.xlsx"))


app = FastAPI(title="Power LP Results Frontend")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _files_for_dir(base: Path) -> tuple[Path, Path, Path]:
    return (
        base / "summary.json",
        base / "hourly_dispatch.csv",
        base / "cost_breakdown.csv",
    )


def _assumptions_file_for_dir(base: Path) -> Path:
    return base / "assumptions_used.csv"


def _scenario_exists(base: Path) -> bool:
    summary_file, hourly_file, cost_file = _files_for_dir(base)
    return summary_file.exists() and hourly_file.exists() and cost_file.exists()


def _load_scenario_index() -> dict[str, Any] | None:
    if not SCENARIO_INDEX_FILE.exists():
        return None
    try:
        return json.loads(SCENARIO_INDEX_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _discover_scenarios() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []

    if _scenario_exists(OUTPUT_DIR):
        scenarios.append(
            {
                "id": "base",
                "label": "Base case",
                "source": "outputs",
                "path": str(OUTPUT_DIR.resolve()),
            }
        )

    index = _load_scenario_index()
    if index:
        for row in index.get("scenarios", []):
            scenario_id = str(row.get("id", "")).strip()
            if not scenario_id:
                continue
            scenario_dir = SCENARIO_ROOT / scenario_id
            if not _scenario_exists(scenario_dir):
                continue
            scenarios.append(
                {
                    "id": scenario_id,
                    "label": row.get("label", scenario_id),
                    "min_non_fossil_share": row.get("min_non_fossil_share"),
                    "threshold_non_fossil_share": row.get("threshold_non_fossil_share"),
                    "enforced_min_non_fossil_share": row.get("enforced_min_non_fossil_share"),
                    "achieved_non_fossil_share": row.get("achieved_non_fossil_share"),
                    "achieved_non_fossil_share_served_primary": row.get(
                        "achieved_non_fossil_share_served_primary"
                    ),
                    "achieved_fossil_share_served_primary": row.get(
                        "achieved_fossil_share_served_primary"
                    ),
                    "achieved_solar_share_served": row.get("achieved_solar_share_served"),
                    "status": row.get("status"),
                    "lcoe_usd_per_mwh_served": row.get("lcoe_usd_per_mwh_served"),
                    "source": "scenario_index",
                    "path": str(scenario_dir.resolve()),
                }
            )
    elif SCENARIO_ROOT.exists():
        for sub in sorted(SCENARIO_ROOT.iterdir()):
            if not sub.is_dir() or not _scenario_exists(sub):
                continue
            scenarios.append(
                {
                    "id": sub.name,
                    "label": sub.name,
                    "source": "scenario_scan",
                    "path": str(sub.resolve()),
                }
            )

    # Deduplicate by scenario id while preserving first appearance.
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in scenarios:
        sid = str(row["id"])
        if sid in seen:
            continue
        seen.add(sid)
        deduped.append(row)

    return deduped


def _default_scenario_id() -> str | None:
    scenarios = _discover_scenarios()
    if not scenarios:
        return None

    # Prefer explicit scenario runs over base if available.
    for row in scenarios:
        if row["id"] != "base":
            return str(row["id"])
    return str(scenarios[0]["id"])


def _resolve_scenario_dir(scenario: str | None) -> tuple[str, Path]:
    scenario_id = (scenario or "").strip()
    if not scenario_id:
        default_id = _default_scenario_id()
        if default_id is None:
            raise HTTPException(
                status_code=404,
                detail="No outputs found. Run optimize_power_lp.py or run_non_fossil_scenarios.py first.",
            )
        scenario_id = default_id

    if scenario_id == "base":
        base = OUTPUT_DIR
    else:
        base = SCENARIO_ROOT / scenario_id

    if not _scenario_exists(base):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Scenario '{scenario_id}' outputs not found. "
                "Run run_non_fossil_scenarios.py to generate scenario results."
            ),
        )

    return scenario_id, base


@lru_cache(maxsize=64)
def _load_summary(summary_path: str, mtime_ns: int) -> dict[str, Any]:
    return json.loads(Path(summary_path).read_text(encoding="utf-8"))


@lru_cache(maxsize=64)
def _load_hourly(hourly_path: str, mtime_ns: int) -> pd.DataFrame:
    return pd.read_csv(hourly_path, parse_dates=["timestamp"])


@lru_cache(maxsize=64)
def _load_costs(cost_path: str, mtime_ns: int) -> pd.DataFrame:
    return pd.read_csv(cost_path)


@lru_cache(maxsize=64)
def _load_assumptions(assumptions_path: str, mtime_ns: int) -> pd.DataFrame:
    return pd.read_csv(assumptions_path)


@lru_cache(maxsize=8)
def _load_excel_assumptions(workbook_path: str, mtime_ns: int) -> pd.DataFrame:
    return pd.read_excel(workbook_path, sheet_name="Cost assumptions")


def _clean_assumption_label(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value)
    # Remove emojis and other non-ASCII glyphs; keep plain text labels only.
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", " ", text).strip()
    return "" if text.lower() == "nan" else text


def _clean_assumption_value(value: Any) -> float | str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text if text and text.lower() != "nan" else None
    try:
        return float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text if text and text.lower() != "nan" else None


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/scenarios")
def api_scenarios() -> dict[str, Any]:
    scenarios = _discover_scenarios()
    default_id = _default_scenario_id()
    return {
        "default_scenario": default_id,
        "scenarios": scenarios,
    }


@app.get("/api/summary")
def api_summary(scenario: str | None = Query(default=None)) -> dict[str, Any]:
    scenario_id, base = _resolve_scenario_dir(scenario)
    summary_file, _, _ = _files_for_dir(base)
    payload = _load_summary(str(summary_file), summary_file.stat().st_mtime_ns)
    payload["scenario_id"] = scenario_id
    return payload


@app.get("/api/hourly")
def api_hourly(
    scenario: str | None = Query(default=None),
    start: int = Query(0, ge=0),
    length: int | None = Query(None, ge=1, le=8784),
) -> dict[str, Any]:
    scenario_id, base = _resolve_scenario_dir(scenario)
    _, hourly_file, _ = _files_for_dir(base)
    df = _load_hourly(str(hourly_file), hourly_file.stat().st_mtime_ns)

    end = len(df) if length is None else min(len(df), start + length)
    if start >= len(df):
        return {"scenario_id": scenario_id, "total_rows": len(df), "rows": []}

    window = df.iloc[start:end].copy()
    window["timestamp"] = window["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "scenario_id": scenario_id,
        "total_rows": int(len(df)),
        "start": int(start),
        "end": int(end),
        "rows": window.to_dict(orient="records"),
    }


@app.get("/api/cost-breakdown")
def api_cost_breakdown(scenario: str | None = Query(default=None)) -> dict[str, Any]:
    scenario_id, base = _resolve_scenario_dir(scenario)
    _, _, cost_file = _files_for_dir(base)
    costs = _load_costs(str(cost_file), cost_file.stat().st_mtime_ns)
    return {
        "scenario_id": scenario_id,
        "rows": costs.to_dict(orient="records"),
    }


@app.get("/api/assumptions")
def api_assumptions(scenario: str | None = Query(default=None)) -> dict[str, Any]:
    scenario_id, base = _resolve_scenario_dir(scenario)
    if INPUT_WORKBOOK.exists():
        assumptions = _load_excel_assumptions(
            str(INPUT_WORKBOOK), INPUT_WORKBOOK.stat().st_mtime_ns
        ).copy()
        if "Assumption" not in assumptions.columns:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Workbook '{INPUT_WORKBOOK}' is missing 'Assumption' column "
                    "in sheet 'Cost assumptions'."
                ),
            )

        rows = []
        for _, row in assumptions.iterrows():
            name = _clean_assumption_label(row.get("Assumption"))
            if not name:
                continue
            rows.append(
                {
                    "assumption": name,
                    "value": _clean_assumption_value(row.get("Value")),
                    "unit": _clean_assumption_label(row.get("Unit / Notes")),
                }
            )
        return {"scenario_id": scenario_id, "source": "excel", "rows": rows}

    # Fallback for environments without source workbook.
    assumptions_file = _assumptions_file_for_dir(base)
    if not assumptions_file.exists():
        return {"scenario_id": scenario_id, "rows": []}

    assumptions_csv = _load_assumptions(
        str(assumptions_file), assumptions_file.stat().st_mtime_ns
    ).copy()
    if "assumption" not in assumptions_csv.columns:
        return {"scenario_id": scenario_id, "rows": []}
    rows = []
    for _, row in assumptions_csv.iterrows():
        name = _clean_assumption_label(row.get("assumption"))
        if not name:
            continue
        rows.append(
            {
                "assumption": name,
                "value": _clean_assumption_value(row.get("value")),
                "unit": None,
            }
        )
    return {"scenario_id": scenario_id, "source": "csv_fallback", "rows": rows}


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    scenarios = _discover_scenarios()
    return {
        "status": "ok",
        "output_dir": str(OUTPUT_DIR),
        "scenario_count": len(scenarios),
        "default_scenario": _default_scenario_id(),
    }
