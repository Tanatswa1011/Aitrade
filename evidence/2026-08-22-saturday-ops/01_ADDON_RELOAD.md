# 01 — AddOn compile / reload

Verdict: **`ADDON_RELOAD_CONFIRMED`**, later **`COLD_RESTART_SAFE`** (auto-load without F5).

## Compile

Operator confirmed 2026-08-22: NinjaScript Editor open, **F5 compile completed, no compilation errors**.

NT maps F5 to Compile (`NinjaScriptEditorHotKeys: Compile='F5'` in `trace.20260822.00002.txt`).

No `error CS*` lines in Saturday NT logs searched under `Documents\NinjaTrader 8\log`.

## New schema actually loaded (not just compiled)

Pre-compile dump (old AddOn): `sim101.excluded=true`, no `sim101.position`, no `nq_bars_1m*`.

Post-compile live dump: schema `AITRADE_NT_READONLY_V1`, `sim101_excluded=false`, `sim101.position` present, `nq_bars_1m` present (`WAITING` / count 0 on closed market). See `03_TELEMETRY_SCHEMA.json`.

## Source vs DLL at compile

| Artifact | Path | mtime (UTC) | size | SHA256 |
|---|---|---|---|---|
| Source (repo) | `ninjascript/AITRADEReadOnlySnapshot.cs` | 2026-08-22T10:38:02Z | 36147 | `eefa412903784b520fa0feefd5c4230bdc093fd941bb1bc9778aa3483b3af8bc` |
| Source (NT AddOns copy) | `Documents\NinjaTrader 8\bin\Custom\AddOns\AITRADEReadOnlySnapshot.cs` | 2026-08-22T10:38:02Z | 36147 | same as repo |
| Compiled | `Documents\NinjaTrader 8\bin\Custom\NinjaTrader.Custom.dll` | 2026-08-22T12:03:22.050292Z | 1306112 | `1cc5f74ec5cd9c48b3e08fb10f3200f2ea048daab7a718f4fb2b5855b771912d` |

DLL mtime **is later than source** (`dll_mtime > repo_cs_mtime: True`). Repo CS SHA256 equals the NT AddOns copy (same file content).

Full records: `02_DLL_HASHES.txt`.

## Sessions that loaded this DLL

| Session | NT PID | Start (local) | Proof |
|---|---|---|---|
| Post-F5 verification | 19580 | 12:57:30 | `ADDON_RELOAD_CONFIRMED`; dump already new schema |
| Pre-restart baseline (A1) | 6020 | 13:55:09 | dump alive, new schema |
| Cold restart (A4–A7, still running) | **15376** | 15:38:54 | Control Center after Welcome; **no F5** |

Cold-restart load (no F5):

```
2026-08-22 15:56:04:154 Loading C:\Users\tanam\OneDrive\Documents\NinjaTrader 8\bin\Custom\NinjaTrader.Custom.dll...
```

from `Documents\NinjaTrader 8\trace\trace.20260822.00002.txt`.

## DLL timestamp unchanged during auto-load

A1 baseline recorded DLL `mtime_unix=1787400202.0502925`, size 1306112.  
A4/A7 and this pack still read **the same mtime and size**. No recompile, no F5 after Welcome.

That is the auto-load proof: new process 15376, dump refreshing with new schema, DLL file identity unchanged.
