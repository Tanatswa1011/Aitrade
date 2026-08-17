# Phase 1 — Indicator / Study Accessibility Report

**Chart:** OANDA:XAUUSD · resolution `5`  
**Date:** 2026-08-15  
**Method:** CDP → `TradingViewApi.activeChart()` study APIs + Pine drawing primitives  
**Bridge status:** unchanged (`tv_health_check`, chart info, screenshot, draw, clear)

---

## Studies currently loaded

| # | Study name | ID | Visible | Notes |
|---|---|---|---|---|
| 1 | ICT Sessions & Killzones - Indices, FX, Crypto (VinceFxBT) | `h6mnwz` | **yes** | Primary session indicator |
| 2 | FXN - Asian Session Range | `ZdJin1` | no | Present but inactive / empty runtime data |
| 3 | AMD Liquidity Sweep with Alerts | `wy7VYI` | no | Not LuxAlgo; inactive / empty runtime data |

**Not found on this chart:**

- LuxAlgo – Market Structure with Inducements & Sweeps
- Dedicated CISD indicator
- Dedicated FVG indicator

---

## Per-indicator accessibility

### 1) ICT Sessions & Killzones (VinceFxBT)

```text
Indicator: ICT Sessions & Killzones - Indices, FX, Crypto (VinceFxBT)
Detected: YES
Study ID: h6mnwz
Readable: PARTIAL → USEFUL for session levels
Historical values readable: YES (multiple prior Asia/London/NY boxes retained)
Live values readable: YES (latest box high/low + labels)
Labels readable: YES (text: "Asia", "London", "New York", "NYO", "8.30")
Lines readable: YES (dwglines; dotted high/low / open markers)
Boxes readable: YES (dwgboxes; y1/y2 = session high/low)
Inputs readable: YES (109 inputs; timezone currently "GMT")
Alert state exposed: API flags exist; no reliable current alert payload found
Reliable enough for strategy engine: YES for observable Asia/London/NY High-Low
Recommended source of truth: LIVE OBSERVABLE for session levels; strategy engine still owns sequencing
Notes:
- Session High/Low are machine-readable from Pine drawing primitives:
  labels[].t + boxes[].y1/y2
- Latest observed levels on this chart:
  Asia High 4362.045 / Asia Low 4311.04
  London High 4380.745 / London Low 4329.81
  New York High 4397.06 / New York Low 4370.32
- Plot series exist (apiDataLength=400) but are mostly shape/colorer sentinel values,
  not clean named "AsiaHigh" numeric series.
- Time mapping via graphics indexes is incomplete (many -2000000 placeholders).
  Price levels are solid; exact session start/end timestamps from drawings are weaker.
- Killzones currently hidden via input ("Hide all Killzones" = true).
```

### 2) FXN - Asian Session Range

```text
Indicator: FXN - Asian Session Range
Detected: YES
Study ID: ZdJin1
Readable: INPUTS ONLY (runtime empty while hidden)
Historical values readable: NO (dataLength=0, empty graphics)
Live values readable: NO while invisible
Labels readable: NO (empty)
Lines readable: NO (empty)
Boxes readable: NO (empty)
Inputs readable: YES — very useful for intended session clock times
Alert state exposed: alertcondition plot exists; no live state
Reliable enough for strategy engine: NO for live levels; YES as config hint
Recommended source of truth: not live levels; may inform config defaults if you confirm these times
Notes:
- Configured clocks (inputs, labeled EST):
  Asia 20:00–02:00
  London 03:00–07:00
  New York 08:00–12:00
- Legend DOM also echoes these input strings.
- Must not be treated as live Asia/London High/Low while invisible/empty.
```

### 3) AMD Liquidity Sweep with Alerts

```text
Indicator: AMD Liquidity Sweep with Alerts
Detected: YES (but NOT LuxAlgo MS / NOT CISD)
Study ID: wy7VYI
Readable: INPUTS ONLY while hidden
Historical values readable: NO (dataLength=0)
Live values readable: NO
Labels readable: NO
Lines readable: NO
Boxes readable: NO
Inputs readable: YES (includes Asia Session Start/End Hour 0–6, swing pivot settings)
Alert state exposed: alertcondition plots defined; no live payload
Reliable enough for strategy engine: NO
Recommended source of truth: none — do not use for AITRADE session-liquidity sweeps
Notes:
- Triangle up/down plot styles suggest structural/sweep markers, but no data while hidden.
- Even if enabled later, this is still not a substitute for:
  Asia High/Low or London High/Low session-liquidity events owned by the setup engine.
```

### 4) LuxAlgo Market Structure with Inducements & Sweeps

```text
Indicator: LuxAlgo - Market Structure with Inducements & Sweeps
Detected: NO
Study ID: n/a
Readable: NO
Historical values readable: NO
Live values readable: NO
Labels readable: NO
Lines readable: NO
Boxes readable: NO
Inputs readable: NO
Reliable enough for strategy engine: NO (absent)
Recommended source of truth: cannot decide until loaded and re-probed
Notes:
- Not present in getAllStudies() on the active chart.
- Re-run Phase 1 probe after adding it if you want indicator-fed MSS/IDM/CHoCH/BOS.
- Even if readable later: IDM must remain separate from CISD.
```

### 5) CISD indicator

```text
Indicator: CISD (dedicated)
Detected: NO
Study ID: n/a
Readable: NO
Historical values readable: NO
Live values readable: NO
Labels readable: NO
Lines readable: NO
Boxes readable: NO
Inputs readable: NO
Reliable enough for strategy engine: NO (absent)
Recommended source of truth: cannot decide until loaded and re-probed, or Pine provided for internal port
Notes:
- No study name containing CISD was found.
- Do not alias LuxAlgo IDM (also absent) as CISD.
```

### 6) FVG indicator

```text
Indicator: FVG (dedicated)
Detected: NO
Study ID: n/a
Readable: NO
Historical values readable: NO
Live values readable: NO
Labels readable: NO
Lines readable: NO
Boxes readable: NO
Inputs readable: NO
Reliable enough for strategy engine: NO (absent)
Recommended source of truth: Internal 3-candle FVG detector for setup-linked gaps
Notes:
- No FVG study loaded.
- User chart drawings (rectangles etc.) are manual shapes, not indicator FVGs.
```

---

## API surface findings (general)

**Readable reliably**

- `getAllStudies()` → id + name
- `getStudyById(id).getInputValues()` / `getInputsInfo()` → named inputs
- `getStyleValues()` → plot style metadata (often generic `plot_0` titles)
- Pine drawing primitives under study graphics:
  - `dwgboxes` → session rectangles with prices
  - `dwglabels` → session names + anchor prices
  - `dwglines` → horizontal/vertical marker lines

**Not reliable / not available**

- `exportData()` on Desktop (previously: “Data export is not supported”)
- Clean named plot series like `AsiaHigh` from ICT Sessions
- Alert “current fired state” as a clean boolean for these studies
- LuxAlgo / CISD / FVG (not loaded)
- Fragile DOM legend text (usable only as fallback; session times appeared for FXN inputs)

**OHLC bars** remain readable via main series `getSeries().data()` for internal calculations / backtests.

---

## Source-of-truth recommendation

| Concept | Recommended source of truth | Why |
|---|---|---|
| **Session High/Low** | **ICT Sessions indicator drawings (live)** + **internal OHLC calc (backtest / fallback)** | ICT boxes/labels currently expose Asia/London/NY high-low programmatically. Internal calc still required for deterministic history and when indicator is hidden/missing. |
| **Session Sweep** | **Always AITRADE strategy engine** | Indicators may mark unrelated sweeps. Engine must decide when Asia/London High/Low is taken. |
| **MSS** | **Internal detector for now** (re-evaluate if LuxAlgo is loaded) | LuxAlgo not on chart. |
| **CISD** | **Unavailable from indicators today** → needs CISD study loaded or Pine-based internal port | Absent; must stay separate from IDM. |
| **IDM** | **Unavailable today** → LuxAlgo when loaded, else later internal | Absent; never treat as CISD. |
| **CHoCH** | **Unavailable today** → LuxAlgo when loaded, else later internal | Absent. |
| **BOS** | **Unavailable today** → LuxAlgo when loaded, else later internal | Absent. |
| **FVG** | **Internal setup-linked 3-candle detector** | No FVG indicator loaded; FVG only matters after session-sweep setup. |

### Hard architectural rule (confirmed by Phase 1)

```text
Indicator may provide observables
        ↓
AITRADE setup engine owns sequencing:
SESSION LEVEL → SESSION LIQUIDITY SWEEP → CONFIRMATION → FVG → …
```

A readable Asia/London high-low from ICT Sessions does **not** create a setup by itself.

---

## Implications for Phase 2+

1. **Phase 2 (session model)** can proceed with:
   - adapter reading ICT Sessions boxes/labels for live Asia/London High-Low
   - parallel internal OHLC session calculator (config-driven times)
2. Before confirmation work, please load (if you want indicator-fed structure):
   - LuxAlgo Market Structure with Inducements & Sweeps
   - dedicated CISD indicator
   - then re-run the same probe
3. Do **not** wait on LuxAlgo/CISD to build session-liquidity + sweep sequencing — that remains ours.
4. FXN Asian Session Range inputs suggest EST clocks (Asia 20:00–02:00, London 03:00–07:00). Confirm whether these match your intended ICT Sessions timezone (`GMT` input currently) before locking config.

---

## Evidence files produced

- `phase1_raw.json`
- `phase1_plots.json`
- `phase1_session_levels.json`
- `phase1_primitive_data.json`
- `phase1_graphics.json` / `phase1_primitives.json` / `phase1_meta.json` (intermediate)

No MCP bridge refactor. No strategy engine implementation.
