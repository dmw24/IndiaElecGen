const THEME = {
  paperBg: "rgba(0,0,0,0)",
  plotBg: "rgba(0,0,0,0)",
  text: "#e6f2ef",
  grid: "rgba(255,255,255,0.12)",
  demand: "#f2a13b",
  colors: {
    solar: "#ffcf5c",
    diesel: "#ff7f50",
    ccgt: "#55d3be",
    coal: "#7fb3ff",
    battery_discharge: "#9ef076",
    battery_charge: "#f27d7d",
    unserved: "#ff4d6d",
    soc: "#7ec7ff",
    net: "#50d2c2",
  },
};

let scenarios = [];
let currentScenario = null;
let summary = null;
let hourly = [];
let costRows = [];
let assumptionsRows = [];
let comparisonSummaries = [];
let comparisonLoadFailures = 0;
let activeTab = "deep-dive";
let useApiBackend = false;
const STATIC_OUTPUTS_DIR = "./outputs";

const DAILY_SUM_KEYS = [
  "demand_mwh",
  "gen_solar_mwh",
  "gen_diesel_mwh",
  "gen_ccgt_mwh",
  "gen_coal_mwh",
  "battery_discharge_mwh",
  "battery_charge_mwh",
  "battery_net_mwh",
  "unserved_mwh",
];

const TECH_COST_COLORS = {
  solar: THEME.colors.solar,
  battery: THEME.colors.battery_discharge,
  diesel: THEME.colors.diesel,
  ccgt: THEME.colors.ccgt,
  coal: THEME.colors.coal,
  unserved_penalty: THEME.colors.unserved,
};

function fmtMoney(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value || 0);
}

function fmtNumber(value, digits = 0) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value || 0);
}

function fmtPct(value, digits = 1) {
  return `${fmtNumber((value || 0) * 100, digits)}%`;
}

function plotConfig() {
  return { responsive: true, displayModeBar: true, displaylogo: false };
}

function layoutBase() {
  return {
    paper_bgcolor: THEME.paperBg,
    plot_bgcolor: THEME.plotBg,
    margin: { l: 58, r: 18, t: 14, b: 45 },
    font: { color: THEME.text, family: "Space Grotesk, sans-serif" },
    xaxis: { gridcolor: THEME.grid, zeroline: false },
    yaxis: { gridcolor: THEME.grid, zeroline: false },
    legend: { orientation: "h", y: -0.18, x: 0 },
  };
}

async function fetchJson(url, options = {}) {
  const resp = await fetch(url);
  if (!resp.ok) {
    if (options.optional && resp.status === 404) {
      return null;
    }
    const text = await resp.text();
    throw new Error(text || `Request failed: ${url}`);
  }
  return resp.json();
}

async function fetchCsvRows(url, options = {}) {
  const resp = await fetch(url);
  if (!resp.ok) {
    if (options.optional && resp.status === 404) {
      return [];
    }
    const text = await resp.text();
    throw new Error(text || `Request failed: ${url}`);
  }

  if (typeof Papa === "undefined") {
    throw new Error("CSV parser not loaded (Papa Parse missing).");
  }

  const text = await resp.text();
  const parsed = Papa.parse(text, {
    header: true,
    dynamicTyping: true,
    skipEmptyLines: true,
  });

  if (Array.isArray(parsed.errors) && parsed.errors.length > 0) {
    const first = parsed.errors[0];
    throw new Error(`CSV parse error (${url}): ${first.message}`);
  }

  return Array.isArray(parsed.data) ? parsed.data : [];
}

function scenarioStaticBasePath(scenarioId) {
  if (scenarioId === "base") {
    return STATIC_OUTPUTS_DIR;
  }
  return `${STATIC_OUTPUTS_DIR}/scenarios/${scenarioId}`;
}

async function loadScenarioListFromStaticFiles() {
  const rows = [];
  const baseSummary = await fetchJson(`${STATIC_OUTPUTS_DIR}/summary.json`, { optional: true });
  if (baseSummary) {
    rows.push({ id: "base", label: "Base case", source: "static_base" });
  }

  const indexPayload = await fetchJson(`${STATIC_OUTPUTS_DIR}/scenarios/scenario_index.json`, {
    optional: true,
  });
  if (indexPayload && Array.isArray(indexPayload.scenarios)) {
    for (const scenario of indexPayload.scenarios) {
      const id = String(scenario.id || "").trim();
      if (!id) {
        continue;
      }
      rows.push({
        id,
        label: scenario.label || id,
        min_non_fossil_share: scenario.min_non_fossil_share,
        threshold_non_fossil_share: scenario.threshold_non_fossil_share,
        enforced_min_non_fossil_share: scenario.enforced_min_non_fossil_share,
        achieved_non_fossil_share: scenario.achieved_non_fossil_share,
        achieved_non_fossil_share_served_primary: scenario.achieved_non_fossil_share_served_primary,
        achieved_fossil_share_served_primary: scenario.achieved_fossil_share_served_primary,
        status: scenario.status,
        lcoe_usd_per_mwh_served: scenario.lcoe_usd_per_mwh_served,
        source: "static_index",
      });
    }
  }

  const deduped = [];
  const seen = new Set();
  for (const row of rows) {
    const id = String(row.id || "");
    if (!id || seen.has(id)) {
      continue;
    }
    seen.add(id);
    deduped.push(row);
  }

  if (!deduped.length) {
    throw new Error("No static outputs found.");
  }

  const preferred = deduped.find((row) => row.id !== "base");
  return {
    default_scenario: preferred ? preferred.id : deduped[0].id,
    scenarios: deduped,
  };
}

function scenarioLabelById(id) {
  const row = scenarios.find((s) => s.id === id);
  return row ? row.label : id;
}

function setKpis() {
  const generation = summary.annual_generation_mwh || {};
  const annualBatteryDischarge = Number(generation.battery_discharge || 0);
  const annualBatteryCharge = Number(generation.battery_charge || 0);
  const kpis = [
    { label: "Scenario", value: scenarioLabelById(currentScenario) },
    { label: "Objective Cost", value: fmtMoney(summary.objective_usd) },
    { label: "LCOE (served)", value: `${fmtMoney(summary.lcoe_usd_per_mwh_served)}/MWh` },
    { label: "Total Demand", value: `${fmtNumber(summary.total_demand_mwh)} MWh` },
    { label: "Unserved Energy", value: `${fmtNumber(summary.unserved_energy_mwh)} MWh` },
    { label: "Non-fossil Target", value: fmtPct(summary.min_non_fossil_share_target || 0, 2) },
    {
      label: "Non-fossil Achieved",
      value: fmtPct(summary.achieved_non_fossil_share_served_primary || summary.achieved_non_fossil_share || 0, 2),
    },
    {
      label: "Fossil Share",
      value: fmtPct(summary.achieved_fossil_share_served_primary || 0, 2),
    },
    {
      label: "Installed Solar",
      value: `${fmtNumber(summary.capacity_mw.solar, 1)} MW`,
    },
    {
      label: "Battery Throughput",
      value:
        `${fmtNumber(annualBatteryDischarge)} MWh dis` +
        `<br>${fmtNumber(annualBatteryCharge)} MWh chg`,
    },
  ];

  const container = document.getElementById("kpiGrid");
  container.innerHTML = kpis
    .map(
      (kpi) =>
        `<article class="kpi-card"><h3>${kpi.label}</h3><p>${kpi.value}</p></article>`
    )
    .join("");
}

function setMeta() {
  document.getElementById("metaBlock").innerHTML = `
    <div>Scenario ID: <strong>${currentScenario}</strong></div>
    <div>Status: <strong>${summary.status}</strong></div>
    <div>Hours Modeled: <strong>${fmtNumber(summary.hours_modeled)}</strong></div>
    <div>From: <strong>${summary.timestamp_start}</strong></div>
    <div>To: <strong>${summary.timestamp_end}</strong></div>
    <div>VOLL: <strong>${fmtMoney(summary.voll_usd_per_mwh)}/MWh</strong></div>
  `;
}

function getWindowedRows() {
  const windowSize = Number(document.getElementById("windowSize").value);
  const start = Number(document.getElementById("startHour").value);
  const end = Math.min(hourly.length, start + windowSize);
  return hourly.slice(start, end);
}

function isFullYearWindowSelected() {
  return Number(document.getElementById("windowSize").value) === 8784;
}

function aggregateDaily(rows) {
  if (!rows.length) return [];

  const grouped = [];
  let day = null;
  let acc = null;

  for (const row of rows) {
    const dayKey = String(row.timestamp).slice(0, 10);
    if (dayKey !== day) {
      day = dayKey;
      acc = { timestamp: dayKey, battery_soc_mwh: 0 };
      for (const key of DAILY_SUM_KEYS) {
        acc[key] = 0;
      }
      grouped.push(acc);
    }

    for (const key of DAILY_SUM_KEYS) {
      acc[key] += Number(row[key] || 0);
    }
    // Keep end-of-day SOC so daily points reflect storage level at day close.
    acc.battery_soc_mwh = Number(row.battery_soc_mwh || 0);
  }

  return grouped;
}

function renderDispatch(rows, isDaily) {
  const x = rows.map((r) => r.timestamp);
  const stacks = ["gen_solar_mwh", "gen_diesel_mwh", "gen_ccgt_mwh", "gen_coal_mwh", "battery_discharge_mwh"];
  const labels = {
    gen_solar_mwh: "Solar",
    gen_diesel_mwh: "Diesel",
    gen_ccgt_mwh: "CCGT",
    gen_coal_mwh: "Coal",
    battery_discharge_mwh: "Battery Discharge",
  };
  const colors = {
    gen_solar_mwh: THEME.colors.solar,
    gen_diesel_mwh: THEME.colors.diesel,
    gen_ccgt_mwh: THEME.colors.ccgt,
    gen_coal_mwh: THEME.colors.coal,
    battery_discharge_mwh: THEME.colors.battery_discharge,
  };

  const traces = stacks.map((key) => ({
    x,
    y: rows.map((r) => Number(r[key] || 0)),
    mode: "lines",
    stackgroup: "generation",
    name: labels[key],
    line: { width: 1.2, color: colors[key] },
    fillcolor: colors[key],
  }));

  traces.push({
    x,
    y: rows.map((r) => Number(r.battery_charge_mwh || 0) * -1),
    mode: "lines",
    name: "Battery Charge (-)",
    line: { color: THEME.colors.battery_charge, width: 1.6, dash: "dot" },
  });

  traces.push({
    x,
    y: rows.map((r) => Number(r.unserved_mwh || 0)),
    mode: "lines",
    name: "Unserved",
    line: { color: THEME.colors.unserved, width: 1.8 },
  });

  traces.push({
    x,
    y: rows.map((r) => Number(r.demand_mwh || 0)),
    mode: "lines",
    name: "Demand",
    line: { color: THEME.demand, width: 2.3 },
  });

  const layout = layoutBase();
  layout.xaxis = { ...layout.xaxis, title: isDaily ? "Day" : "Time" };
  layout.yaxis = { ...layout.yaxis, title: isDaily ? "MWh per day" : "MWh per hour" };
  Plotly.newPlot("dispatchChart", traces, layout, plotConfig());
}

function renderBattery(rows, isDaily) {
  const x = rows.map((r) => r.timestamp);
  const traces = [
    {
      x,
      y: rows.map((r) => Number(r.battery_soc_mwh || 0)),
      type: "scatter",
      mode: "lines",
      name: "State of Charge",
      line: { width: 2.2, color: THEME.colors.soc },
      yaxis: "y1",
    },
    {
      x,
      y: rows.map((r) => Number(r.battery_net_mwh || 0)),
      type: "bar",
      name: "Battery Net Output",
      marker: { color: THEME.colors.net, opacity: 0.7 },
      yaxis: "y2",
    },
  ];

  const layout = layoutBase();
  layout.barmode = "relative";
  layout.xaxis = { ...layout.xaxis, title: isDaily ? "Day" : "Time" };
  layout.yaxis = { ...layout.yaxis, title: "SOC (MWh)" };
  layout.yaxis2 = {
    title: isDaily ? "Net Output (MWh/day)" : "Net Output (MWh/h)",
    overlaying: "y",
    side: "right",
    gridcolor: "rgba(0,0,0,0)",
    zerolinecolor: THEME.grid,
  };
  Plotly.newPlot("batteryChart", traces, layout, plotConfig());
}

function renderCapacity() {
  const order = ["solar", "battery", "diesel", "ccgt", "coal"];
  const labelMap = {
    solar: "Solar",
    battery: "Battery",
    diesel: "Diesel",
    ccgt: "CCGT",
    coal: "Coal",
  };
  const barColors = [THEME.colors.solar, THEME.colors.battery_discharge, THEME.colors.diesel, THEME.colors.ccgt, THEME.colors.coal];

  const trace = {
    x: order.map((k) => labelMap[k]),
    y: order.map((k) => Number(summary.capacity_mw[k] || 0)),
    type: "bar",
    marker: { color: barColors },
  };

  const layout = layoutBase();
  layout.yaxis = { ...layout.yaxis, title: "MW" };
  Plotly.newPlot("capacityChart", [trace], layout, plotConfig());
}

function renderGenerationMix() {
  const generation = summary.annual_generation_mwh;
  const labels = ["Solar", "Diesel", "CCGT", "Coal"];
  const values = [
    Number(generation.solar || 0),
    Number(generation.diesel || 0),
    Number(generation.ccgt || 0),
    Number(generation.coal || 0),
  ];

  const trace = {
    labels,
    values,
    type: "pie",
    hole: 0.45,
    automargin: true,
    textposition: "auto",
    marker: {
      colors: [
        THEME.colors.solar,
        THEME.colors.diesel,
        THEME.colors.ccgt,
        THEME.colors.coal,
      ],
    },
    textinfo: "label+percent",
  };

  const layout = layoutBase();
  layout.margin = { l: 28, r: 28, t: 56, b: 40 };
  layout.uniformtext = { minsize: 11, mode: "hide" };
  Plotly.newPlot("generationMixChart", [trace], layout, plotConfig());
}

function renderCostBreakdown() {
  if (!summary) {
    return;
  }

  const modeSelect = document.getElementById("costUnitMode");
  const mode = modeSelect ? modeSelect.value : "total";
  const servedEnergy = Number(summary.served_energy_mwh || summary.total_demand_mwh || 0);
  const usePerMwh = mode === "per_mwh" && servedEnergy > 0;
  const scale = usePerMwh ? 1 / servedEnergy : 1;
  const yAxisTitle = usePerMwh ? "USD per MWh served" : "USD";
  const formatValue = (value) =>
    usePerMwh ? `${fmtNumber(value * scale, 3)} USD/MWh` : fmtMoney(value);

  const hasDetailed = costRows.some((row) => Object.prototype.hasOwnProperty.call(row, "component"));
  if (!hasDetailed) {
    const grouped = { fixed: 0, variable: 0, penalty: 0 };
    for (const row of costRows) {
      grouped[row.bucket] = (grouped[row.bucket] || 0) + Number(row.cost_usd || 0);
    }
    const fixedTrace = {
      x: ["Annual Cost Buckets"],
      y: [grouped.fixed * scale],
      name: "Fixed",
      type: "bar",
      marker: { color: "#5ec2ff" },
      customdata: [formatValue(grouped.fixed)],
      hovertemplate: "%{x}<br>%{customdata}<extra></extra>",
    };
    const variableTrace = {
      x: ["Annual Cost Buckets"],
      y: [grouped.variable * scale],
      name: "Variable",
      type: "bar",
      marker: { color: "#ffad5a" },
      customdata: [formatValue(grouped.variable)],
      hovertemplate: "%{x}<br>%{customdata}<extra></extra>",
    };
    const penaltyTrace = {
      x: ["Annual Cost Buckets"],
      y: [grouped.penalty * scale],
      name: "Unserved Penalty",
      type: "bar",
      marker: { color: "#ff4d6d" },
      customdata: [formatValue(grouped.penalty)],
      hovertemplate: "%{x}<br>%{customdata}<extra></extra>",
    };
    const fallbackLayout = layoutBase();
    fallbackLayout.barmode = "stack";
    fallbackLayout.yaxis = { ...fallbackLayout.yaxis, title: yAxisTitle };
    Plotly.newPlot("costChart", [fixedTrace, variableTrace, penaltyTrace], fallbackLayout, plotConfig());
    return;
  }

  const techOrder = ["solar", "battery", "diesel", "ccgt", "coal"];
  const componentOrder = ["capex_annualized", "fixed_om", "var_om"];
  const techLabel = {
    solar: "Solar",
    battery: "Battery",
    diesel: "Diesel",
    ccgt: "CCGT",
    coal: "Coal",
  };
  const componentLabel = {
    capex_annualized: "CAPEX (annualized)",
    fixed_om: "Fixed O&M",
    var_om: "Var O&M",
  };

  const valueFor = (technology, component) =>
    costRows
      .filter((row) => row.technology === technology && row.component === component)
      .reduce((sum, row) => sum + Number(row.cost_usd || 0), 0);

  const steps = [];
  for (const tech of techOrder) {
    for (const component of componentOrder) {
      const value = valueFor(tech, component);
      if (Math.abs(value) < 0.5) {
        continue;
      }
      steps.push({
        label: `${techLabel[tech]} ${componentLabel[component]}`,
        value,
        technology: tech,
      });
    }
  }

  const unservedPenalty = costRows
    .filter((row) => row.component === "unserved_penalty")
    .reduce((sum, row) => sum + Number(row.cost_usd || 0), 0);
  if (Math.abs(unservedPenalty) >= 0.5) {
    steps.push({ label: "Unserved Penalty", value: unservedPenalty, technology: "unserved_penalty" });
  }

  const x = [];
  const y = [];
  const base = [];
  const colors = [];
  const hover = [];
  let running = 0;

  for (const step of steps) {
    const scaledValue = step.value * scale;
    x.push(step.label);
    y.push(scaledValue);
    base.push(running);
    colors.push(TECH_COST_COLORS[step.technology] || "#5ec2ff");
    hover.push(formatValue(step.value));
    running += scaledValue;
  }

  x.push("Total System Cost");
  y.push(running);
  base.push(0);
  colors.push(THEME.demand);
  hover.push(usePerMwh ? `${fmtNumber(running, 3)} USD/MWh` : fmtMoney(running));

  const trace = {
    type: "bar",
    x,
    y,
    base,
    marker: { color: colors },
    customdata: hover,
    hovertemplate: "%{x}<br>%{customdata}<extra></extra>",
  };

  const layout = layoutBase();
  layout.margin = { l: 78, r: 24, t: 14, b: 120 };
  layout.xaxis = { ...layout.xaxis, tickangle: -35 };
  layout.yaxis = { ...layout.yaxis, title: yAxisTitle };
  Plotly.newPlot("costChart", [trace], layout, plotConfig());
}

function renderAssumptions() {
  const block = document.getElementById("assumptionsInline");
  const title = document.getElementById("assumptionsTitle");
  if (!block) {
    return;
  }

  if (!assumptionsRows.length) {
    if (title) {
      title.textContent = "All Assumptions Used (0)";
    }
    block.textContent = "No assumptions file found for this scenario.";
    return;
  }

  const formatAssumptionValue = (value) => {
    if (value === null || value === undefined || value === "") {
      return "";
    }
    const num = Number(value);
    if (Number.isFinite(num)) {
      if (Number.isInteger(num)) {
        return String(num);
      }
      return String(Number(num.toFixed(6)));
    }
    return String(value).replace(/[^\x20-\x7E]/g, "").replace(/\s+/g, " ").trim();
  };

  const cleanText = (value) =>
    String(value ?? "").replace(/[^\x20-\x7E]/g, "").replace(/\s+/g, " ").trim();

  const parts = assumptionsRows
    .map((row) => {
      const name = cleanText(row.assumption);
      if (!name) {
        return "";
      }
      const valueText = formatAssumptionValue(row.value);
      const unitText = cleanText(row.unit);

      if (valueText && unitText) {
        return `${name}=${valueText} (${unitText})`;
      }
      if (valueText) {
        return `${name}=${valueText}`;
      }
      if (unitText) {
        return `${name} (${unitText})`;
      }
      return name;
    })
    .filter((text) => text.length > 0);

  if (title) {
    title.textContent = `All Assumptions Used (${parts.length})`;
  }

  block.textContent = parts.join("; ");
}

function renderComparison() {
  const objectiveEl = document.getElementById("comparisonObjectiveChart");
  const lcoeEl = document.getElementById("comparisonLcoeChart");
  const shareEl = document.getElementById("comparisonShareChart");
  const capacityEl = document.getElementById("comparisonCapacityChart");
  const generationEl = document.getElementById("comparisonGenerationChart");
  const reliabilityEl = document.getElementById("comparisonReliabilityChart");
  const meta = document.getElementById("comparisonMeta");
  if (!objectiveEl || !lcoeEl || !shareEl || !capacityEl || !generationEl || !reliabilityEl) {
    return;
  }

  if (!comparisonSummaries.length) {
    for (const id of [
      "comparisonObjectiveChart",
      "comparisonLcoeChart",
      "comparisonShareChart",
      "comparisonCapacityChart",
      "comparisonGenerationChart",
      "comparisonReliabilityChart",
    ]) {
      const node = document.getElementById(id);
      if (node) {
        Plotly.purge(node);
      }
    }
    if (meta) {
      meta.textContent = "Comparison data not available.";
    }
    return;
  }

  const rows = comparisonSummaries.slice();
  const labels = rows.map((row) => row.scenario_label || row.scenario_id || "");
  const asNumber = (value, fallback = 0) => {
    const num = Number(value);
    return Number.isFinite(num) ? num : fallback;
  };

  const comparisonLayout = (yTitle, options = {}) => {
    const layout = layoutBase();
    layout.margin = { l: 76, r: 20, t: 12, b: 90 };
    layout.xaxis = { ...layout.xaxis, tickangle: -24, automargin: true };
    layout.yaxis = { ...layout.yaxis, title: yTitle };
    return { ...layout, ...options };
  };

  const objectiveTrace = {
    type: "bar",
    x: labels,
    y: rows.map((row) => asNumber(row.objective_usd)),
    marker: { color: "#6dbfff" },
    hovertemplate: "%{x}<br>%{y:$,.0f}<extra></extra>",
  };
  Plotly.newPlot(
    objectiveEl,
    [objectiveTrace],
    comparisonLayout("USD"),
    plotConfig()
  );

  const lcoeTrace = {
    type: "bar",
    x: labels,
    y: rows.map((row) => asNumber(row.lcoe_usd_per_mwh_served)),
    marker: { color: "#f2a13b" },
    hovertemplate: "%{x}<br>%{y:.3f} USD/MWh<extra></extra>",
  };
  Plotly.newPlot(
    lcoeEl,
    [lcoeTrace],
    comparisonLayout("USD/MWh"),
    plotConfig()
  );

  const targetTrace = {
    type: "bar",
    name: "Target NF",
    x: labels,
    y: rows.map((row) => asNumber(row.min_non_fossil_share_target) * 100),
    marker: { color: "#67c5ff" },
    hovertemplate: "%{x}<br>Target: %{y:.2f}%<extra></extra>",
  };
  const achievedTrace = {
    type: "bar",
    name: "Achieved NF",
    x: labels,
    y: rows.map((row) => asNumber(row.achieved_non_fossil_share_served_primary ?? row.achieved_non_fossil_share) * 100),
    marker: { color: THEME.colors.solar },
    hovertemplate: "%{x}<br>Achieved NF: %{y:.2f}%<extra></extra>",
  };
  const fossilTrace = {
    type: "bar",
    name: "Achieved Fossil",
    x: labels,
    y: rows.map((row) => asNumber(row.achieved_fossil_share_served_primary) * 100),
    marker: { color: THEME.colors.coal },
    hovertemplate: "%{x}<br>Fossil: %{y:.2f}%<extra></extra>",
  };
  Plotly.newPlot(
    shareEl,
    [targetTrace, achievedTrace, fossilTrace],
    comparisonLayout("%", { barmode: "group" }),
    plotConfig()
  );

  const techOrder = ["solar", "battery", "diesel", "ccgt", "coal"];
  const techLabels = {
    solar: "Solar",
    battery: "Battery",
    diesel: "Diesel",
    ccgt: "CCGT",
    coal: "Coal",
  };
  const capacityTraces = techOrder.map((tech) => ({
    type: "bar",
    name: techLabels[tech],
    x: labels,
    y: rows.map((row) => asNumber((row.capacity_mw || {})[tech])),
    marker: { color: TECH_COST_COLORS[tech] || "#5ec2ff" },
    hovertemplate: `%{x}<br>${techLabels[tech]}: %{y:,.1f} MW<extra></extra>`,
  }));
  Plotly.newPlot(
    capacityEl,
    capacityTraces,
    comparisonLayout("MW", { barmode: "stack" }),
    plotConfig()
  );

  const generationTraces = ["solar", "diesel", "ccgt", "coal"].map((tech) => ({
    type: "bar",
    name: techLabels[tech],
    x: labels,
    y: rows.map((row) => asNumber((row.annual_generation_mwh || {})[tech])),
    marker: { color: TECH_COST_COLORS[tech] || "#5ec2ff" },
    hovertemplate: `%{x}<br>${techLabels[tech]}: %{y:,.0f} MWh<extra></extra>`,
  }));
  Plotly.newPlot(
    generationEl,
    generationTraces,
    comparisonLayout("MWh", { barmode: "stack" }),
    plotConfig()
  );

  const servedTrace = {
    type: "bar",
    name: "Served",
    x: labels,
    y: rows.map((row) => asNumber(row.served_energy_mwh)),
    marker: { color: "#5ec2ff" },
    hovertemplate: "%{x}<br>Served: %{y:,.0f} MWh<extra></extra>",
  };
  const unservedTrace = {
    type: "bar",
    name: "Unserved",
    x: labels,
    y: rows.map((row) => asNumber(row.unserved_energy_mwh)),
    marker: { color: THEME.colors.unserved },
    hovertemplate: "%{x}<br>Unserved: %{y:,.0f} MWh<extra></extra>",
  };
  Plotly.newPlot(
    reliabilityEl,
    [servedTrace, unservedTrace],
    comparisonLayout("MWh", { barmode: "group" }),
    plotConfig()
  );

  if (meta) {
    const loaded = rows.length;
    const total = scenarios.length;
    if (comparisonLoadFailures > 0 || loaded !== total) {
      meta.textContent = `Loaded ${loaded} of ${total} scenarios.`;
    } else {
      meta.textContent = `Loaded ${loaded} scenarios.`;
    }
  }
}

function resizeDeepDiveCharts() {
  const ids = ["dispatchChart", "batteryChart", "capacityChart", "generationMixChart", "costChart"];
  for (const id of ids) {
    const node = document.getElementById(id);
    if (node && node.data) {
      Plotly.Plots.resize(node);
    }
  }
}

function resizeComparisonCharts() {
  const ids = [
    "comparisonObjectiveChart",
    "comparisonLcoeChart",
    "comparisonShareChart",
    "comparisonCapacityChart",
    "comparisonGenerationChart",
    "comparisonReliabilityChart",
  ];
  for (const id of ids) {
    const node = document.getElementById(id);
    if (node && node.data) {
      Plotly.Plots.resize(node);
    }
  }
}

function setActiveTab(tabId) {
  activeTab = tabId === "comparison" ? "comparison" : "deep-dive";
  const deepDivePanel = document.getElementById("deepDivePanel");
  const comparisonPanel = document.getElementById("comparisonPanel");

  if (deepDivePanel) {
    deepDivePanel.classList.toggle("is-active", activeTab === "deep-dive");
  }
  if (comparisonPanel) {
    comparisonPanel.classList.toggle("is-active", activeTab === "comparison");
  }

  document.querySelectorAll(".tab-btn[data-tab]").forEach((btn) => {
    const isActive = btn.dataset.tab === activeTab;
    btn.classList.toggle("is-active", isActive);
    btn.setAttribute("aria-selected", String(isActive));
  });

  if (activeTab === "comparison") {
    renderComparison();
    setTimeout(() => resizeComparisonCharts(), 0);
  } else {
    setTimeout(() => resizeDeepDiveCharts(), 0);
  }
}

function renderWindow() {
  const isDaily = isFullYearWindowSelected();
  const rows = isDaily ? aggregateDaily(getWindowedRows()) : getWindowedRows();
  renderDispatch(rows, isDaily);
  renderBattery(rows, isDaily);
}

function updateSliderMax() {
  const windowSize = Number(document.getElementById("windowSize").value);
  const startSlider = document.getElementById("startHour");
  const startLabel = document.getElementById("startHourLabel");
  const maxStart = Math.max(0, hourly.length - windowSize);
  startSlider.max = String(maxStart);
  if (Number(startSlider.value) > maxStart) {
    startSlider.value = String(maxStart);
  }
  startLabel.textContent = String(startSlider.value);
}

function setLoadingState(message) {
  const container = document.getElementById("kpiGrid");
  container.innerHTML = `<article class="kpi-card"><h3>Loading</h3><p>${message}</p></article>`;
}

function setError(message) {
  const container = document.querySelector(".app-shell");
  container.innerHTML = `
    <section class="card" style="padding: 1.2rem; border-color: rgba(255,77,109,0.6)">
      <h2 style="color:#ff8da1">Frontend could not load optimization outputs</h2>
      <p>${message}</p>
      <p>Generate outputs first, then ensure the <code>outputs/</code> folder is available to this page.</p>
    </section>
  `;
}

async function loadScenarioList() {
  let payload = null;
  useApiBackend = false;
  try {
    payload = await loadScenarioListFromStaticFiles();
  } catch (_error) {
    payload = await fetchJson("/api/scenarios");
    useApiBackend = true;
  }

  scenarios = payload.scenarios || [];

  if (!scenarios.length) {
    throw new Error("No scenarios found. Generate results first.");
  }

  const select = document.getElementById("scenarioSelect");
  select.innerHTML = scenarios
    .map((row) => `<option value="${row.id}">${row.label}</option>`)
    .join("");

  currentScenario = payload.default_scenario || scenarios[0].id;
  select.value = currentScenario;
}

async function loadScenarioData(scenarioId) {
  if (useApiBackend) {
    const query = `?scenario=${encodeURIComponent(scenarioId)}`;
    const [summaryPayload, hourlyPayload, costPayload, assumptionsPayload] = await Promise.all([
      fetchJson(`/api/summary${query}`),
      fetchJson(`/api/hourly${query}`),
      fetchJson(`/api/cost-breakdown${query}`),
      fetchJson(`/api/assumptions${query}`),
    ]);

    currentScenario = summaryPayload.scenario_id || scenarioId;
    summary = summaryPayload;
    hourly = hourlyPayload.rows || [];
    costRows = costPayload.rows || [];
    assumptionsRows = assumptionsPayload.rows || [];
    return;
  }

  const basePath = scenarioStaticBasePath(scenarioId);
  const [summaryPayload, hourlyRows, costs, assumptions] = await Promise.all([
    fetchJson(`${basePath}/summary.json`),
    fetchCsvRows(`${basePath}/hourly_dispatch.csv`),
    fetchCsvRows(`${basePath}/cost_breakdown.csv`),
    fetchCsvRows(`${basePath}/assumptions_used.csv`, { optional: true }),
  ]);

  currentScenario = scenarioId;
  summary = {
    ...summaryPayload,
    scenario_id: summaryPayload.scenario_id || scenarioId,
  };
  hourly = hourlyRows;
  costRows = costs;
  assumptionsRows = assumptions.map((row) => ({
    assumption: row.assumption ?? row.Assumption ?? "",
    value: row.value ?? row.Value ?? null,
    unit: row.unit ?? row["Unit / Notes"] ?? "",
  }));
}

async function loadComparisonData() {
  const tasks = scenarios.map(async (row) => {
    if (useApiBackend) {
      const query = `?scenario=${encodeURIComponent(row.id)}`;
      const payload = await fetchJson(`/api/summary${query}`);
      return {
        ...payload,
        scenario_id: payload.scenario_id || row.id,
        scenario_label: row.label || payload.scenario_id || row.id,
      };
    }

    const payload = await fetchJson(`${scenarioStaticBasePath(row.id)}/summary.json`);
    return {
      ...payload,
      scenario_id: row.id,
      scenario_label: row.label || row.id,
    };
  });

  const settled = await Promise.allSettled(tasks);
  comparisonSummaries = settled
    .filter((result) => result.status === "fulfilled")
    .map((result) => result.value);
  comparisonLoadFailures = settled.length - comparisonSummaries.length;
}

function renderAll() {
  setMeta();
  setKpis();
  updateSliderMax();
  renderCapacity();
  renderGenerationMix();
  renderCostBreakdown();
  renderWindow();
  renderAssumptions();
}

function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn[data-tab]");
  tabButtons.forEach((button) => {
    button.addEventListener("click", () => {
      setActiveTab(button.dataset.tab || "deep-dive");
    });
  });
  setActiveTab(activeTab);
}

function setupControls() {
  const windowSelect = document.getElementById("windowSize");
  const startSlider = document.getElementById("startHour");
  const startLabel = document.getElementById("startHourLabel");
  const scenarioSelect = document.getElementById("scenarioSelect");
  const costModeSelect = document.getElementById("costUnitMode");

  windowSelect.addEventListener("change", () => {
    updateSliderMax();
    renderWindow();
  });

  startSlider.addEventListener("input", () => {
    startLabel.textContent = String(startSlider.value);
    renderWindow();
  });

  if (costModeSelect) {
    costModeSelect.addEventListener("change", () => {
      renderCostBreakdown();
    });
  }

  scenarioSelect.addEventListener("change", async () => {
    try {
      setLoadingState(`Loading ${scenarioLabelById(scenarioSelect.value)}...`);
      await loadScenarioData(scenarioSelect.value);
      renderAll();
    } catch (error) {
      setError(error.message || String(error));
    }
  });
}

async function main() {
  try {
    setLoadingState("Loading scenarios...");
    await loadScenarioList();
    setupTabs();
    setupControls();

    setLoadingState(`Loading ${scenarioLabelById(currentScenario)}...`);
    await Promise.all([loadScenarioData(currentScenario), loadComparisonData()]);
    renderAll();
    renderComparison();
  } catch (error) {
    setError(error.message || String(error));
  }
}

main();
