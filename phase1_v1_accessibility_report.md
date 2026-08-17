# Phase 1 v1 — Final Accessibility & Source-of-Truth Report

**Chart:** OANDA:XAUUSD · resolution `5`  
**Loaded studies:**  
1. `ICT Sessions & Killzones - Indices, FX, Crypto (VinceFxBT)` — `h6mnwz`  
2. `Market Structure with Inducements & Sweeps [LuxAlgo]` — `smUEv2`  

**Method:** CDP → `TradingViewApi` → study `_graphics._primitivesCollection.*.*_primitivesDataById`  
**Bridge:** unchanged  
**CISD:** out of scope for v1  

---

## 1. LuxAlgo accessibility report

```text
Indicator: Market Structure with Inducements & Sweeps [LuxAlgo]
Detected: YES
Study ID: smUEv2
Readable: YES (labels + matched horizontal lines)
Historical values readable: YES (retained drawings; 189 labels / 189 lines on chart)
Live values readable: YES
Labels readable: YES — texts: CHoCH, BOS, IDM, x
Lines readable: YES — horizontal levels paired 1:1 with labels by price
Shapes readable: NO dedicated shapes/boxes (0 boxes); events are labels+lines
Inputs readable: YES
CHoCH readable: YES (17 events)
BOS readable: YES (34 events)
IDM readable: YES (51 events)
Sweeps readable: YES as label text "x" (87 events) — LuxAlgo structural sweeps, NOT session sweeps
Swing levels readable: PARTIAL
  - plot titles "Swing High" / "Swing Low" exist
  - plot series mostly empty (only 2 sparse rows observed)
  - practical swing/structure levels come from CHoCH/BOS/IDM lines
Timestamps readable: PARTIAL
  - price + drawing x always present
  - graphics._indexes maps some recent x → bar index
  - many historical events still show placeholder -2000000
Reliable enough for strategy engine: YES for LIVE structure confirmation after a session sweep
Notes:
- pineId present; pineVersion readable
- Direction for CHoCH/BOS is machine-decidable from text color (tci)
- IDM/"x" use accent color; side inferred from label style lup/ldn
- Plot series are NOT the primary source of truth for events
- Legend/DOM is noisy fallback only
```

### Key LuxAlgo inputs (live)

| Input | Value |
|---|---|
| CHoCH Detection Period | 50 |
| IDM Detection Period | 3 |
| Show CHoCH / BOS / Inducements / Sweeps / Swings | all `true` |
| Bullish Elements color | `4286683400` |
| Bearish Elements color | `4283585279` |
| Inducement/Sweep accent color | `4287003512` |

---

## 2. Exact observable mapping

### Shared extraction path

```text
getStudyById(smUEv2)
  → study()._graphics
  → _primitivesCollection.dwglabels / dwglines
  → collection._primitivesDataById
```

### Event object fields

| Field | Meaning |
|---|---|
| `t` | Event type: `CHoCH` \| `BOS` \| `IDM` \| `x` |
| `y` | Event price |
| `x` | Drawing-space bar anchor |
| `indexMapped` | Chart bar index when `_indexes[x]` is valid |
| `tci` | Text/label color (direction for CHoCH/BOS) |
| `st` | Label style: `ldn` (label_down) \| `lup` (label_up) |
| paired line `y1=y2` | Structure level price |
| paired line `st` | `dsh`=CHoCH, `sol`=BOS, `dot`=IDM or sweep |
| paired line `ci` | Line color (same direction encoding) |
| paired line `x1,x2` | Level span in drawing space |

### Mapping table

| Concept | How identified | Direction | Price | Time/bar |
|---|---|---|---|---|
| **CHoCH** | label `t=="CHoCH"` + dashed line (`dsh`) at same `y` | `tci` / line `ci` == Bullish or Bearish Elements color; consistently `ldn`↔bullish, `lup`↔bearish | label `y` / line `y1` | `x` always; bar index sometimes via `_indexes` |
| **BOS** | label `t=="BOS"` + solid line (`sol`) at same `y` | same color encoding as CHoCH | label `y` | same |
| **IDM** | label `t=="IDM"` + dotted line (`dot`) accent color | accent color only; side from `st` (`ldn`/`lup`), not bull/bear palette | label `y` | same |
| **Structural sweep** | label `t=="x"` + dotted accent line | accent color; side from `st` | label `y` | same |
| **Swing high** | best via bullish CHoCH/BOS levels / sparse Swing High plot | bullish color on high breaks | line/plot price | weak in plots |
| **Swing low** | best via bearish CHoCH/BOS levels / sparse Swing Low plot | bearish color on low breaks | line/plot price | weak in plots |

### Direction decode (validated 100% on this chart)

```text
CHoCH/BOS:
  tci == Bullish Elements (4286683400) → bullish
  tci == Bearish Elements (4283585279) → bearish
  st == "ldn" ↔ bullish
  st == "lup" ↔ bearish

IDM / sweep "x":
  tci/ci == accent (4287003512)
  use st lup/ldn only as side hint; not bull/bear palette
```

### Observed counts

| Type | Count | With price | With real bar index |
|---|---:|---:|---:|
| CHoCH | 17 | 17 | 1 |
| BOS | 34 | 34 | 2 |
| IDM | 51 | 50 | 2 |
| x (sweep) | 87 | 87 | 3 |

Latest notable live events:

- Bullish **CHoCH** @ **4397.06** (bar index 299) — matches current NY High / session high area  
- Bearish **BOS** @ **4311.04** (bar index 299) — matches Asia Low  
- LuxAlgo sweep **x** @ **4311.27** near Asia Low  

This supports using LuxAlgo as **confirmation**, while AITRADE still decides whether Asia/London liquidity was swept.

---

## 3. Recommended v1 MSS rule

```text
After a valid Session High/Low sweep, what LuxAlgo event should AITRADE use
as the primary market-structure confirmation?

→ CHoCH (direction-aligned)
```

### Rule

```text
IF Asia Low OR London Low swept
AND next LuxAlgo CHoCH is bullish
→ bullish structure confirmation

IF Asia High OR London High swept
AND next LuxAlgo CHoCH is bearish
→ bearish structure confirmation
```

### Why CHoCH (not BOS / IDM / internal)

1. **Readable cleanly** — explicit `t="CHoCH"`, price, direction via `tci`.  
2. **Fits “first shift”** — fewer events than BOS (17 vs 34); LuxAlgo exposes a dedicated CHoCH period.  
3. **Visual/line encoding matches intent** — CHoCH uses dashed levels; BOS uses solid continuation levels.  
4. **Strategy fit** — after session liquidity is taken, the first change of character is the confirmation you want; BOS is usually continuation after that.  
5. **IDM is not MSS** — inducement context only; accent-colored; not CISD; weaker direction encoding.  
6. **Internal MSS not required for v1 live path** — LuxAlgo CHoCH is reliable enough live. Keep internal MSS as a later backtest fallback if LuxAlgo history/timestamps prove insufficient.

---

## 4. Trigger vs context

```text
CHoCH                     → TRIGGER (primary MSS confirmation after session sweep)
BOS                       → CONTEXT (continuation / secondary evidence; not v1 trigger)
IDM                       → CONTEXT (optional setup-quality / inducement; never required; never = CISD)
LuxAlgo structural sweep  → IGNORE as setup originator
                            (optional weak context only; session sweep remains AITRADE-owned)
```

Hard rule preserved:

```text
SESSION LIQUIDITY
      ↓
SESSION HIGH / LOW SWEPT          ← AITRADE
      ↓
LUXALGO BULLISH/BEARISH CHoCH     ← confirmation only
      ↓
FVG (internal)
      ↓
ENTRY path (later phases)
```

Any CHoCH / BOS / IDM / LuxAlgo `x` **without** a prior Asia/London session sweep must **not** create a setup.

---

## 5. Final v1 source-of-truth matrix

| Concept | Live source | Strategy owner | Backtest source | Reliability |
| --- | --- | --- | --- | --- |
| Asia High/Low | ICT Sessions boxes/labels | AITRADE reads observable | Internal OHLC session calc | High (prices); timestamps partial |
| London High/Low | ICT Sessions boxes/labels | AITRADE reads observable | Internal OHLC session calc | High (prices); timestamps partial |
| Session Sweep | AITRADE engine vs ICT levels | AITRADE | AITRADE on historical OHLC + session levels | High if sweep rule locked later |
| MSS / Structure Shift | LuxAlgo **CHoCH** (dir-aligned) | AITRADE sequences; LuxAlgo supplies event | LuxAlgo history if indexes OK, else internal MSS fallback later | High live; medium historical time-join |
| CHoCH | LuxAlgo label+dashed line | Confirmation input to engine | Same | High live |
| BOS | LuxAlgo label+solid line | Context only (v1) | Same | High live |
| IDM | LuxAlgo label+dotted accent line | Optional context (v1); ≠ CISD | Same | Medium (direction via style only) |
| FVG | AITRADE internal 3-candle | AITRADE | AITRADE | High (deterministic) |
| Strategy Sequencing | AITRADE setup engine | AITRADE always | AITRADE | n/a |

Validated architecture:

```text
Session High/Low     → ICT Sessions (live) + internal OHLC (backtest/fallback)
Session Sweep        → AITRADE
Structure confirm    → LuxAlgo CHoCH (direction-aligned)
BOS / IDM            → context only
FVG                  → AITRADE internal
Strategy sequencing  → AITRADE always
```

---

## 6. Reliability concerns

1. **Timestamps / bar indexes** — most historical LuxAlgo/ICT drawings still map to `-2000000`. Recent events often resolve. Live sequencing by arrival/order + price is fine; strict historical time-join is fragile.  
2. **Historical object retention** — LuxAlgo currently retains many events (189/189), but TradingView may prune; do not assume infinite history.  
3. **Opaque / sparse plots** — Swing High/Low plots are almost empty; do not use plots as event source. Use labels+lines.  
4. **Internal TV APIs** — `_primitivesDataById` is undocumented internal surface; stable in practice here, but version-sensitive.  
5. **Repaint / confirmation delay** — CHoCH Detection Period = 50. Structure labels can appear with lag relative to the sweep candle; engine must allow post-sweep wait window (exact expiry still unlocked).  
6. **LuxAlgo sweeps ≠ session sweeps** — label `x` must not open setups.  
7. **IDM direction** — accent-only color; side from `lup`/`ldn` only. Fine for context, weak as a hard filter.  
8. **Backtest inconsistency risk** — live path can read LuxAlgo; backtests should prefer internal OHLC session levels + either replayed LuxAlgo exports or a later internal MSS, because drawing-index history is incomplete.  
9. **Hidden-study behavior** — not an issue now (both visible); empty runtime if hidden (learned from earlier FXN/AMD).  
10. **Free-plan coupling** — v1 depends on these exact two indicators remaining loaded.

---

## 7. Recommended Phase 2 starting point

Do **not** implement yet. When approved, Phase 2 should build first:

### Phase 2.1 — Session level adapter (ICT)
- Read Asia/London High/Low from ICT `_primitivesDataById` boxes/labels  
- Normalize timezone from ICT input (`GMT`)  
- Emit structured `SessionLevel` objects (session, side, price, optional time)

### Phase 2.2 — Internal OHLC session model (parallel)
- Config-driven Asia/London windows  
- Deterministic High/Low for backtest/fallback  
- Compare/optionally reconcile with ICT live levels

### Phase 2.3 — Session sweep engine (AITRADE)
- Detect when price takes Asia/London High or Low  
- Own the sweep definition (still unlocked details: wick vs close, etc.)  
- Emit `SessionSweepEvent` that unlocks confirmation listening

**Only after those:** LuxAlgo CHoCH confirmation consumer → then FVG module.

Suggested first mergeable slice: **2.1 + 2.2 + 2.3 skeleton** (no entry/SL/RR yet).

---

## Evidence files

- `phase1_v1_extract.json`  
- `phase1_v1_lux_deep.json`  
- `phase1_v1_lux_analysis.json`  
- `phase1_v1_lux_probe.py` / `phase1_v1_lux_analyze.py` / `phase1_v1_extract.py`  

No Phase 2 implementation. No MCP bridge refactor. No trade execution.
