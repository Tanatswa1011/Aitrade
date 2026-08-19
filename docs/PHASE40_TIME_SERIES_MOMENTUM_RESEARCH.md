# Phase 40 — Medium-horizon time-series momentum

Research only. `DRY_RUN`. No broker execution. Nothing frozen.

## 1. Verdict

- **Overall:** `TREND_EDGE_WEAK`
- **ES_TREND_STATUS:** `TREND_EDGE_WEAK`
- **NQ_TREND_STATUS:** `TREND_EDGE_WEAK`
- **GC_TREND_STATUS:** `TREND_EDGE_REJECTED`
- **Recommendation:** `DO_NOT_FREEZE_WEAK_TSMOM`
- **Branch:** `DO_NOT_FREEZE_WEAK_TSMOM`

Primary candidate locked before P&L: `TSMOM_20D_5D` (20-session roll-cleaned return sign, enter next open, hold 5 sessions, 1 contract, 1-tick adverse each side, commissions).

## 2. Frozen integrity

Verified before and after this phase. Frozen files were not modified.

- GC VWAP V2 config hash: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ DVP config hash: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- File SHA GC: `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f`
- File SHA NQ: `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541`

## 3. Repository / data audit

Daily research uses Databento `GLBX.MDP3` `ohlcv-1d` on `.v.0` volume-continuous **unadjusted** series.

- **SIGNAL_SERIES:** roll-cleaned close-to-close. Suspected roll nights (overnight move > max(4× prior-60d median |overnight|, 15 ticks) and gap > 15 ticks) use that day's open-to-close only.
- **EXECUTION_SERIES:** same daily OHLC. Roll overnight is removed from P&L; genuine gaps remain.
- Databento 1d bars are Globex session OHLC, not reconstructed 09:30–16:00 RTH. Mode 2 is same-session open→close.
- No CFDs, ETFs, or cash substitutes.

- **ES:** n=4196 2010-06-07 → 2026-08-14; roll flags=67 (0.016); remap_weekend_dates=False
- **NQ:** n=4196 2010-06-07 → 2026-08-14; roll flags=94 (0.022); remap_weekend_dates=False
- **GC:** n=4198 2010-06-07 → 2026-08-14; roll flags=73 (0.017); remap_weekend_dates=False

Chronology predeclared: TRAIN through 2022-12-30; HOLDOUT from 2023-01-03. Walk-forward folds WF1–WF5 also predeclared.

## 4. Raw momentum predictability

Signal = sign of past N-session roll-cleaned return at completed close t. Forward return starts at t+1 (no current-session leak). Long and short are **not** pooled. Observations overlap; iid t-stats are secondary; block-bootstrap CIs preferred.

| Instrument | Lookback | Fwd | Side | N | Mean | Median | Hit | t (iid) | Block-boot mean CI | Spearman |mag| vs signed fwd |
|---|---:|---:|---|---:|---:|---:|---:|---:|---|---:|
| ES | 5 | 1 | LONG | 2544 | 0.00034 | 0.00058 | 0.547 | 2.01 | [0.00003, 0.00063] | -0.012 |
| ES | 5 | 1 | SHORT | 1641 | -0.00100 | -0.00124 | 0.446 | -3.11 | [-0.00153, -0.00048] | -0.066 |
| ES | 5 | 3 | LONG | 2542 | 0.00096 | 0.00200 | 0.579 | 3.45 | [0.00026, 0.00160] | 0.002 |
| ES | 5 | 3 | SHORT | 1641 | -0.00297 | -0.00390 | 0.403 | -5.71 | [-0.00437, -0.00154] | -0.080 |
| ES | 5 | 5 | LONG | 2541 | 0.00166 | 0.00311 | 0.599 | 4.62 | [0.00048, 0.00273] | 0.031 |
| ES | 5 | 5 | SHORT | 1640 | -0.00482 | -0.00617 | 0.381 | -7.50 | [-0.00665, -0.00290] | -0.133 |
| ES | 5 | 10 | LONG | 2536 | 0.00401 | 0.00679 | 0.643 | 7.83 | [0.00204, 0.00626] | 0.042 |
| ES | 5 | 10 | SHORT | 1640 | -0.00860 | -0.01047 | 0.361 | -10.70 | [-0.01181, -0.00577] | -0.136 |
| ES | 10 | 1 | LONG | 2681 | 0.00038 | 0.00063 | 0.549 | 2.39 | [0.00008, 0.00067] | -0.015 |
| ES | 10 | 1 | SHORT | 1501 | -0.00096 | -0.00122 | 0.454 | -2.72 | [-0.00158, -0.00027] | -0.064 |
| ES | 10 | 3 | LONG | 2679 | 0.00124 | 0.00219 | 0.592 | 4.68 | [0.00066, 0.00199] | 0.006 |
| ES | 10 | 3 | SHORT | 1501 | -0.00269 | -0.00340 | 0.424 | -4.74 | [-0.00408, -0.00101] | -0.071 |
| ES | 10 | 5 | LONG | 2677 | 0.00203 | 0.00338 | 0.608 | 5.85 | [0.00104, 0.00320] | 0.007 |
| ES | 10 | 5 | SHORT | 1501 | -0.00453 | -0.00618 | 0.392 | -6.53 | [-0.00672, -0.00236] | -0.123 |
| ES | 10 | 10 | LONG | 2672 | 0.00436 | 0.00686 | 0.647 | 9.03 | [0.00220, 0.00650] | 0.038 |
| ES | 10 | 10 | SHORT | 1501 | -0.00855 | -0.01069 | 0.367 | -9.73 | [-0.01194, -0.00482] | -0.138 |
| ES | 20 | 1 | LONG | 2832 | 0.00046 | 0.00066 | 0.552 | 3.05 | [0.00013, 0.00077] | -0.014 |
| ES | 20 | 1 | SHORT | 1343 | -0.00094 | -0.00122 | 0.455 | -2.39 | [-0.00173, -0.00017] | -0.097 |
| ES | 20 | 3 | LONG | 2830 | 0.00145 | 0.00225 | 0.591 | 5.77 | [0.00082, 0.00206] | -0.015 |
| ES | 20 | 3 | SHORT | 1343 | -0.00255 | -0.00329 | 0.423 | -4.03 | [-0.00409, -0.00112] | -0.119 |
| ES | 20 | 5 | LONG | 2828 | 0.00213 | 0.00354 | 0.612 | 6.67 | [0.00100, 0.00309] | -0.006 |
| ES | 20 | 5 | SHORT | 1343 | -0.00476 | -0.00637 | 0.400 | -6.08 | [-0.00733, -0.00187] | -0.142 |
| ES | 20 | 10 | LONG | 2824 | 0.00429 | 0.00679 | 0.647 | 9.62 | [0.00253, 0.00647] | -0.004 |
| ES | 20 | 10 | SHORT | 1342 | -0.00918 | -0.01128 | 0.368 | -9.20 | [-0.01341, -0.00479] | -0.162 |
| ES | 60 | 1 | LONG | 3206 | 0.00034 | 0.00063 | 0.545 | 2.38 | [0.00006, 0.00065] | -0.007 |
| ES | 60 | 1 | SHORT | 929 | -0.00152 | -0.00170 | 0.437 | -2.92 | [-0.00246, -0.00058] | -0.055 |
| ES | 60 | 3 | LONG | 3204 | 0.00119 | 0.00217 | 0.585 | 4.96 | [0.00051, 0.00183] | -0.034 |
| ES | 60 | 3 | SHORT | 929 | -0.00389 | -0.00501 | 0.409 | -4.64 | [-0.00639, -0.00140] | -0.142 |
| ES | 60 | 5 | LONG | 3202 | 0.00190 | 0.00343 | 0.604 | 6.19 | [0.00087, 0.00281] | -0.041 |
| ES | 60 | 5 | SHORT | 929 | -0.00666 | -0.00813 | 0.374 | -6.48 | [-0.00980, -0.00272] | -0.154 |
| ES | 60 | 10 | LONG | 3197 | 0.00402 | 0.00664 | 0.633 | 9.65 | [0.00216, 0.00583] | -0.055 |
| ES | 60 | 10 | SHORT | 929 | -0.01220 | -0.01563 | 0.326 | -9.17 | [-0.01747, -0.00575] | -0.219 |
| NQ | 5 | 1 | LONG | 2507 | 0.00043 | 0.00084 | 0.552 | 2.01 | [0.00007, 0.00082] | -0.002 |
| NQ | 5 | 1 | SHORT | 1681 | -0.00133 | -0.00175 | 0.434 | -3.54 | [-0.00198, -0.00057] | -0.054 |
| NQ | 5 | 3 | LONG | 2505 | 0.00129 | 0.00276 | 0.576 | 3.55 | [0.00038, 0.00225] | 0.008 |
| NQ | 5 | 3 | SHORT | 1681 | -0.00388 | -0.00518 | 0.398 | -6.57 | [-0.00536, -0.00230] | -0.039 |
| NQ | 5 | 5 | LONG | 2504 | 0.00248 | 0.00474 | 0.595 | 5.25 | [0.00100, 0.00401] | 0.000 |
| NQ | 5 | 5 | SHORT | 1680 | -0.00590 | -0.00736 | 0.398 | -8.17 | [-0.00807, -0.00358] | -0.082 |
| NQ | 5 | 10 | LONG | 2499 | 0.00542 | 0.00845 | 0.622 | 8.17 | [0.00285, 0.00844] | 0.036 |
| NQ | 5 | 10 | SHORT | 1680 | -0.01096 | -0.01372 | 0.347 | -11.80 | [-0.01471, -0.00734] | -0.055 |
| NQ | 10 | 1 | LONG | 2655 | 0.00057 | 0.00088 | 0.554 | 2.84 | [0.00022, 0.00098] | -0.008 |
| NQ | 10 | 1 | SHORT | 1528 | -0.00114 | -0.00166 | 0.438 | -2.74 | [-0.00194, -0.00040] | -0.047 |
| NQ | 10 | 3 | LONG | 2653 | 0.00163 | 0.00318 | 0.589 | 4.79 | [0.00066, 0.00245] | 0.025 |
| NQ | 10 | 3 | SHORT | 1528 | -0.00354 | -0.00462 | 0.418 | -5.38 | [-0.00521, -0.00199] | -0.033 |
| NQ | 10 | 5 | LONG | 2651 | 0.00275 | 0.00482 | 0.595 | 6.18 | [0.00131, 0.00442] | 0.014 |
| NQ | 10 | 5 | SHORT | 1528 | -0.00581 | -0.00753 | 0.396 | -7.30 | [-0.00829, -0.00322] | -0.080 |
| NQ | 10 | 10 | LONG | 2649 | 0.00629 | 0.00912 | 0.635 | 10.09 | [0.00356, 0.00922] | 0.033 |
| NQ | 10 | 10 | SHORT | 1525 | -0.01024 | -0.01357 | 0.365 | -10.02 | [-0.01465, -0.00600] | -0.082 |
| NQ | 20 | 1 | LONG | 2792 | 0.00066 | 0.00093 | 0.560 | 3.46 | [0.00030, 0.00103] | 0.010 |
| NQ | 20 | 1 | SHORT | 1383 | -0.00111 | -0.00168 | 0.443 | -2.41 | [-0.00203, -0.00033] | -0.082 |
| NQ | 20 | 3 | LONG | 2791 | 0.00199 | 0.00318 | 0.591 | 6.18 | [0.00112, 0.00277] | 0.027 |
| NQ | 20 | 3 | SHORT | 1382 | -0.00317 | -0.00491 | 0.420 | -4.38 | [-0.00538, -0.00142] | -0.081 |
| NQ | 20 | 5 | LONG | 2791 | 0.00333 | 0.00512 | 0.605 | 8.03 | [0.00192, 0.00460] | 0.025 |
| NQ | 20 | 5 | SHORT | 1380 | -0.00516 | -0.00746 | 0.412 | -5.79 | [-0.00768, -0.00241] | -0.113 |
| NQ | 20 | 10 | LONG | 2791 | 0.00663 | 0.00950 | 0.638 | 11.42 | [0.00387, 0.00923] | -0.010 |
| NQ | 20 | 10 | SHORT | 1375 | -0.01004 | -0.01367 | 0.370 | -8.73 | [-0.01492, -0.00487] | -0.120 |
| NQ | 60 | 1 | LONG | 3162 | 0.00056 | 0.00093 | 0.556 | 2.88 | [0.00016, 0.00093] | -0.018 |
| NQ | 60 | 1 | SHORT | 973 | -0.00162 | -0.00211 | 0.435 | -2.86 | [-0.00268, -0.00066] | -0.076 |
| NQ | 60 | 3 | LONG | 3162 | 0.00163 | 0.00309 | 0.581 | 5.10 | [0.00069, 0.00242] | -0.021 |
| NQ | 60 | 3 | SHORT | 971 | -0.00483 | -0.00602 | 0.391 | -5.39 | [-0.00716, -0.00275] | -0.141 |
| NQ | 60 | 5 | LONG | 3162 | 0.00259 | 0.00479 | 0.590 | 6.29 | [0.00102, 0.00402] | -0.028 |
| NQ | 60 | 5 | SHORT | 969 | -0.00833 | -0.01013 | 0.369 | -7.71 | [-0.01191, -0.00456] | -0.135 |
| NQ | 60 | 10 | LONG | 3162 | 0.00545 | 0.00842 | 0.622 | 9.62 | [0.00244, 0.00800] | -0.053 |
| NQ | 60 | 10 | SHORT | 964 | -0.01537 | -0.01812 | 0.320 | -11.06 | [-0.02245, -0.00857] | -0.122 |
| GC | 5 | 1 | LONG | 2259 | 0.00035 | 0.00038 | 0.516 | 1.62 | [-0.00004, 0.00077] | -0.029 |
| GC | 5 | 1 | SHORT | 1933 | -0.00016 | -0.00036 | 0.482 | -0.65 | [-0.00063, 0.00021] | -0.040 |
| GC | 5 | 3 | LONG | 2257 | 0.00091 | 0.00122 | 0.530 | 2.49 | [-0.00001, 0.00186] | -0.032 |
| GC | 5 | 3 | SHORT | 1933 | -0.00063 | -0.00054 | 0.483 | -1.51 | [-0.00177, 0.00038] | -0.026 |
| GC | 5 | 5 | LONG | 2255 | 0.00170 | 0.00230 | 0.544 | 3.61 | [0.00030, 0.00322] | -0.023 |
| GC | 5 | 5 | SHORT | 1933 | -0.00080 | -0.00139 | 0.467 | -1.53 | [-0.00251, 0.00099] | -0.059 |
| GC | 5 | 10 | LONG | 2251 | 0.00306 | 0.00239 | 0.537 | 4.62 | [0.00049, 0.00588] | -0.004 |
| GC | 5 | 10 | SHORT | 1932 | -0.00183 | -0.00240 | 0.466 | -2.58 | [-0.00470, 0.00108] | -0.053 |
| GC | 10 | 1 | LONG | 2243 | 0.00020 | 0.00014 | 0.504 | 0.91 | [-0.00018, 0.00057] | 0.018 |
| GC | 10 | 1 | SHORT | 1944 | -0.00033 | -0.00055 | 0.468 | -1.37 | [-0.00087, 0.00020] | -0.031 |
| GC | 10 | 3 | LONG | 2241 | 0.00066 | 0.00044 | 0.510 | 1.81 | [-0.00039, 0.00153] | 0.038 |
| GC | 10 | 3 | SHORT | 1944 | -0.00091 | -0.00111 | 0.459 | -2.17 | [-0.00217, 0.00026] | -0.016 |
| GC | 10 | 5 | LONG | 2239 | 0.00142 | 0.00178 | 0.534 | 3.04 | [-0.00005, 0.00290] | 0.043 |
| GC | 10 | 5 | SHORT | 1944 | -0.00112 | -0.00201 | 0.455 | -2.12 | [-0.00279, 0.00065] | -0.036 |
| GC | 10 | 10 | LONG | 2236 | 0.00234 | 0.00168 | 0.526 | 3.52 | [-0.00112, 0.00540] | 0.026 |
| GC | 10 | 10 | SHORT | 1942 | -0.00270 | -0.00282 | 0.454 | -3.82 | [-0.00569, 0.00002] | -0.060 |
| GC | 20 | 1 | LONG | 2207 | 0.00027 | 0.00032 | 0.515 | 1.18 | [-0.00015, 0.00074] | 0.004 |
| GC | 20 | 1 | SHORT | 1970 | -0.00026 | -0.00038 | 0.481 | -1.14 | [-0.00069, 0.00018] | -0.057 |
| GC | 20 | 3 | LONG | 2205 | 0.00055 | 0.00039 | 0.509 | 1.44 | [-0.00038, 0.00152] | 0.033 |
| GC | 20 | 3 | SHORT | 1970 | -0.00107 | -0.00111 | 0.456 | -2.72 | [-0.00207, 0.00009] | -0.055 |
| GC | 20 | 5 | LONG | 2203 | 0.00089 | 0.00149 | 0.528 | 1.82 | [-0.00070, 0.00233] | 0.031 |
| GC | 20 | 5 | SHORT | 1970 | -0.00181 | -0.00241 | 0.448 | -3.61 | [-0.00336, -0.00038] | -0.083 |
| GC | 20 | 10 | LONG | 2200 | 0.00216 | 0.00213 | 0.529 | 3.06 | [-0.00160, 0.00541] | 0.028 |
| GC | 20 | 10 | SHORT | 1968 | -0.00304 | -0.00271 | 0.455 | -4.62 | [-0.00636, 0.00062] | -0.108 |
| GC | 60 | 1 | LONG | 2338 | 0.00034 | 0.00048 | 0.526 | 1.55 | [-0.00012, 0.00074] | -0.024 |
| GC | 60 | 1 | SHORT | 1799 | -0.00016 | -0.00008 | 0.495 | -0.65 | [-0.00062, 0.00032] | -0.011 |
| GC | 60 | 3 | LONG | 2338 | 0.00097 | 0.00107 | 0.532 | 2.64 | [-0.00008, 0.00196] | -0.054 |
| GC | 60 | 3 | SHORT | 1797 | -0.00053 | -0.00043 | 0.485 | -1.25 | [-0.00180, 0.00069] | -0.008 |
| GC | 60 | 5 | LONG | 2338 | 0.00153 | 0.00244 | 0.541 | 3.28 | [-0.00017, 0.00298] | -0.067 |
| GC | 60 | 5 | SHORT | 1795 | -0.00098 | -0.00133 | 0.467 | -1.81 | [-0.00280, 0.00089] | -0.035 |
| GC | 60 | 10 | LONG | 2338 | 0.00304 | 0.00252 | 0.538 | 4.77 | [0.00061, 0.00585] | -0.080 |
| GC | 60 | 10 | SHORT | 1790 | -0.00181 | -0.00174 | 0.469 | -2.38 | [-0.00514, 0.00269] | -0.028 |

## 5. 5-day momentum

Strategy candidates using this lookback are in the candidate tables below. Raw predictability is in section 4.
- **ES TSMOM_5D_1D:** n=4185 E[pts]=-1.0845 hit=0.486 PF=0.91 DD=5785.8900 train=-0.8986 holdout=-1.7501
- **NQ TSMOM_5D_1D:** n=4188 E[pts]=-2.9891 hit=0.499 PF=0.93 DD=18264.5500 train=-1.7002 holdout=-7.5060
- **GC TSMOM_5D_1D:** n=4192 E[pts]=0.0162 hit=0.493 PF=1.00 DD=1849.8800 train=-0.3269 holdout=1.1987

## 6. 10-day momentum

Strategy candidates using this lookback are in the candidate tables below. Raw predictability is in section 4.
- **ES TSMOM_10D_3D:** n=1395 E[pts]=-2.2047 hit=0.525 PF=0.89 DD=3689.7100 train=-2.0781 holdout=-2.6405
- **NQ TSMOM_10D_3D:** n=1395 E[pts]=-4.1428 hit=0.520 PF=0.95 DD=9425.4500 train=-4.0818 holdout=-4.3529
- **GC TSMOM_10D_3D:** n=1395 E[pts]=0.3738 hit=0.501 PF=1.03 DD=1366.1400 train=0.3806 holdout=0.2606

## 7. 20-day momentum

Strategy candidates using this lookback are in the candidate tables below. Raw predictability is in section 4.
- **ES TSMOM_20D_5D:** n=835 E[pts]=0.6990 hit=0.533 PF=1.03 DD=1108.9600 train=0.8535 holdout=0.1673
- **NQ TSMOM_20D_5D:** n=835 E[pts]=1.3695 hit=0.541 PF=1.01 DD=8807.4000 train=1.9480 holdout=-0.6215
- **GC TSMOM_20D_5D:** n=835 E[pts]=-0.9400 hit=0.471 PF=0.94 DD=2089.9200 train=-1.7545 holdout=1.8825

## 8. 60-day momentum

Strategy candidates using this lookback are in the candidate tables below. Raw predictability is in section 4.
- **ES TSMOM_60D_5D:** n=827 E[pts]=-0.8796 hit=0.544 PF=0.96 DD=2716.7400 train=-3.3144 holdout=7.3961
- **NQ TSMOM_60D_5D:** n=827 E[pts]=0.5025 hit=0.551 PF=1.01 DD=6092.7000 train=-1.1315 holdout=6.0566
- **GC TSMOM_60D_5D:** n=827 E[pts]=1.0022 hit=0.495 PF=1.06 DD=1389.1000 train=-1.2012 holdout=8.5434

## 9. Primary `TSMOM_20D_5D`

| Instrument | N | E[pts] cost | Hit | PF | Max DD | Train E | Holdout E | Mode2 E | Overnight E | Session E | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| ES | 835 | 0.6990 | 0.533 | 1.03 | 1108.9600 | 0.8535 | 0.1673 | -0.5180 | 0.0114 | 1.2677 | `TREND_EDGE_WEAK` |
| NQ | 835 | 1.3695 | 0.541 | 1.01 | 8807.4000 | 1.9480 | -0.6215 | -0.6929 | 1.0859 | 0.9835 | `TREND_EDGE_WEAK` |
| GC | 835 | -0.9400 | 0.471 | 0.94 | 2089.9200 | -1.7545 | 1.8825 | -0.1316 | -0.3289 | -0.3711 | `TREND_EDGE_REJECTED` |

## 10. Fixed-hold vs daily-refresh

- **ES:** 20d/5d E=0.6990 n=835; daily-refresh E=0.1102 n=372 avg hold=11.2; 20d flip_rate=0.089 avg_run=11.2
- **NQ:** 20d/5d E=1.3695 n=835; daily-refresh E=0.5064 n=390 avg hold=10.7; 20d flip_rate=0.093 avg_run=10.7
- **GC:** 20d/5d E=-0.9400 n=835; daily-refresh E=0.1870 n=426 avg hold=9.8; 20d flip_rate=0.102 avg_run=9.8

## 11. Long / short

Sides are scored separately on the locked primary.

- **ES long:** {'n': 558, 'expectancy_points': 6.9907885304659505, 'hit_rate': 0.6093189964157706, 'total_points': 3900.86}
- **ES short:** {'n': 277, 'expectancy_points': -11.975306859205777, 'hit_rate': 0.37906137184115524, 'total_points': -3317.16}
- **NQ long:** {'n': 555, 'expectancy_points': 25.84099099099099, 'hit_rate': 0.618018018018018, 'total_points': 14341.749999999998}
- **NQ short:** {'n': 280, 'expectancy_points': -47.136607142857144, 'hit_rate': 0.3892857142857143, 'total_points': -13198.250000000002}
- **GC long:** {'n': 440, 'expectancy_points': 1.6888636363637666, 'hit_rate': 0.5136363636363637, 'total_points': 743.1000000000573}
- **GC short:** {'n': 395, 'expectancy_points': -3.868354430379578, 'hit_rate': 0.42278481012658226, 'total_points': -1527.9999999999334}

## 12. Signal magnitude

Absolute past return quintiles vs signed forward 5d return after a 20d signal. Broad monotonicity only; no threshold search.

| Instrument | Side | Bucket | N | Mean |sig| | Mean signed fwd | Hit |
|---|---|---|---:|---:|---:|---:|
| ES | LONG | Q1_weakest | 565 | 0.0062 | 0.00256 | 0.619 |
| ES | LONG | Q2 | 566 | 0.0166 | 0.00226 | 0.615 |
| ES | LONG | Q3 | 565 | 0.0266 | 0.00088 | 0.582 |
| ES | LONG | Q4 | 566 | 0.0378 | 0.00267 | 0.647 |
| ES | LONG | Q5_strongest | 566 | 0.0694 | 0.00228 | 0.595 |
| ES | SHORT | Q1_weakest | 268 | 0.0037 | -0.00058 | 0.474 |
| ES | SHORT | Q2 | 269 | 0.0115 | -0.00269 | 0.409 |
| ES | SHORT | Q3 | 268 | 0.0217 | -0.00523 | 0.396 |
| ES | SHORT | Q4 | 269 | 0.0378 | -0.00554 | 0.375 |
| ES | SHORT | Q5_strongest | 269 | 0.0754 | -0.00974 | 0.346 |
| NQ | LONG | Q1_weakest | 558 | 0.0076 | 0.00354 | 0.588 |
| NQ | LONG | Q2 | 558 | 0.0214 | 0.00262 | 0.586 |
| NQ | LONG | Q3 | 558 | 0.0361 | 0.00280 | 0.593 |
| NQ | LONG | Q4 | 558 | 0.0510 | 0.00342 | 0.627 |
| NQ | LONG | Q5_strongest | 559 | 0.0901 | 0.00425 | 0.631 |
| NQ | SHORT | Q1_weakest | 276 | 0.0044 | -0.00235 | 0.395 |
| NQ | SHORT | Q2 | 276 | 0.0153 | -0.00363 | 0.431 |
| NQ | SHORT | Q3 | 276 | 0.0292 | 0.00021 | 0.475 |
| NQ | SHORT | Q4 | 276 | 0.0484 | -0.00591 | 0.417 |
| NQ | SHORT | Q5_strongest | 276 | 0.0874 | -0.01411 | 0.341 |
| GC | LONG | Q1_weakest | 440 | 0.0057 | -0.00097 | 0.491 |
| GC | LONG | Q2 | 441 | 0.0171 | 0.00038 | 0.524 |
| GC | LONG | Q3 | 440 | 0.0305 | 0.00088 | 0.518 |
| GC | LONG | Q4 | 441 | 0.0475 | 0.00363 | 0.599 |
| GC | LONG | Q5_strongest | 441 | 0.0812 | 0.00053 | 0.510 |
| GC | SHORT | Q1_weakest | 394 | 0.0044 | -0.00077 | 0.477 |
| GC | SHORT | Q2 | 394 | 0.0136 | 0.00010 | 0.503 |
| GC | SHORT | Q3 | 394 | 0.0239 | -0.00094 | 0.411 |
| GC | SHORT | Q4 | 394 | 0.0389 | -0.00080 | 0.459 |
| GC | SHORT | Q5_strongest | 394 | 0.0722 | -0.00663 | 0.391 |

## 13. Volatility scaling

Diagnostic weight = expanding-median 20d realized vol / current 20d vol, capped [0.25, 4]. Not a second strategy.

- **ES:** fixed 1-contract E=0.6990; vol-scaled E=0.8513
  - low_vol: n=1391 mean_signed_fwd=0.00026 hit=0.551
  - mid_vol: n=1390 mean_signed_fwd=0.00142 hit=0.581
  - high_vol: n=1390 mean_signed_fwd=-0.00193 hit=0.499
- **NQ:** fixed 1-contract E=1.3695; vol-scaled E=5.6883
  - low_vol: n=1391 mean_signed_fwd=0.00206 hit=0.571
  - mid_vol: n=1390 mean_signed_fwd=0.00132 hit=0.563
  - high_vol: n=1390 mean_signed_fwd=-0.00182 hit=0.489
- **GC:** fixed 1-contract E=-0.9400; vol-scaled E=0.0669
  - low_vol: n=1392 mean_signed_fwd=0.00093 hit=0.527
  - mid_vol: n=1391 mean_signed_fwd=0.00040 hit=0.502
  - high_vol: n=1390 mean_signed_fwd=-0.00248 hit=0.443

## 14. Overnight decomposition

Question: is the premium earned overnight, during the Globex session, or both?

- **ES:** Mode1 E=0.6990; overnight E=0.0114; session E=1.2677; weekend E=0.0850; Mode2 (no overnight) E=-0.5180
- **NQ:** Mode1 E=1.3695; overnight E=1.0859; session E=0.9835; weekend E=1.1599; Mode2 (no overnight) E=-0.6929
- **GC:** Mode1 E=-0.9400; overnight E=-0.3289; session E=-0.3711; weekend E=-0.3829; Mode2 (no overnight) E=-0.1316

## 15. Prop-compatible intraday expression

Mode 2 uses the same 20d signal and a same-session open→close. If Mode 2 loses the edge, overnight exposure is required.

- **ES Mode 2:** n=4175 E=-0.5180 hit=0.501 PF=0.95
- **NQ Mode 2:** n=4175 E=-0.6929 hit=0.516 PF=0.98
- **GC Mode 2:** n=4177 E=-0.1316 hit=0.489 PF=0.98

## 16. Year-by-year

| Instrument | Year | N | E[pts] | Hit | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| ES | 2010 | 26 | -2.3973 | 0.615 | 0.79 | 200.8000 |
| ES | 2011 | 52 | -5.5367 | 0.442 | 0.63 | 375.5200 |
| ES | 2012 | 51 | 3.2631 | 0.549 | 1.46 | 85.9900 |
| ES | 2013 | 52 | -2.3829 | 0.538 | 0.78 | 292.6300 |
| ES | 2014 | 51 | -3.8986 | 0.549 | 0.73 | 317.4600 |
| ES | 2015 | 52 | 1.2710 | 0.423 | 1.09 | 267.5600 |
| ES | 2016 | 51 | -3.2957 | 0.451 | 0.77 | 410.5700 |
| ES | 2017 | 52 | 4.2662 | 0.596 | 1.78 | 147.2100 |
| ES | 2018 | 52 | 4.8623 | 0.558 | 1.21 | 402.3200 |
| ES | 2019 | 52 | 3.9825 | 0.596 | 1.25 | 270.8000 |
| ES | 2020 | 52 | 6.0642 | 0.615 | 1.18 | 659.0500 |
| ES | 2021 | 52 | 8.0690 | 0.577 | 1.33 | 300.1400 |
| ES | 2022 | 52 | -4.9213 | 0.500 | 0.92 | 648.0300 |
| ES | 2023 | 52 | 5.1556 | 0.519 | 1.17 | 326.7400 |
| ES | 2024 | 52 | -8.0608 | 0.538 | 0.81 | 1083.3700 |
| ES | 2025 | 52 | 2.3335 | 0.500 | 1.05 | 801.3400 |
| ES | 2026 | 32 | 1.9122 | 0.531 | 1.04 | 513.4100 |
| NQ | 2010 | 26 | -1.8538 | 0.654 | 0.91 | 366.7500 |
| NQ | 2011 | 52 | -3.1423 | 0.481 | 0.87 | 423.5500 |
| NQ | 2012 | 51 | 13.7363 | 0.647 | 2.05 | 196.0500 |
| NQ | 2013 | 52 | 0.3144 | 0.519 | 1.02 | 334.1500 |
| NQ | 2014 | 51 | -1.5824 | 0.569 | 0.94 | 403.5000 |
| NQ | 2015 | 52 | 3.6750 | 0.442 | 1.09 | 825.3500 |
| NQ | 2016 | 51 | -4.8225 | 0.490 | 0.87 | 883.3500 |
| NQ | 2017 | 52 | 11.9298 | 0.596 | 1.40 | 620.6500 |
| NQ | 2018 | 52 | -0.3683 | 0.538 | 1.00 | 1681.4500 |
| NQ | 2019 | 52 | 13.1413 | 0.615 | 1.25 | 1019.2500 |
| NQ | 2020 | 52 | 31.7904 | 0.596 | 1.26 | 3076.9500 |
| NQ | 2021 | 52 | -39.3202 | 0.519 | 0.76 | 3694.2000 |
| NQ | 2022 | 52 | -0.0462 | 0.538 | 1.00 | 3312.8500 |
| NQ | 2023 | 52 | 11.8913 | 0.519 | 1.09 | 2319.2500 |
| NQ | 2024 | 52 | 1.3096 | 0.519 | 1.01 | 3147.7500 |
| NQ | 2025 | 52 | -49.2577 | 0.481 | 0.79 | 4954.6000 |
| NQ | 2026 | 32 | 54.9406 | 0.531 | 1.20 | 2523.0500 |
| GC | 2010 | 26 | 6.5715 | 0.577 | 2.03 | 54.8800 |
| GC | 2011 | 52 | -4.3842 | 0.385 | 0.78 | 476.1400 |
| GC | 2012 | 52 | -1.8746 | 0.423 | 0.86 | 351.4600 |
| GC | 2013 | 52 | -3.5900 | 0.423 | 0.77 | 274.8800 |
| GC | 2014 | 51 | -0.9361 | 0.510 | 0.91 | 200.2800 |
| GC | 2015 | 52 | -0.2265 | 0.462 | 0.97 | 121.4200 |
| GC | 2016 | 51 | -0.5380 | 0.471 | 0.95 | 267.4000 |
| GC | 2017 | 52 | -2.1688 | 0.462 | 0.76 | 180.5400 |
| GC | 2018 | 52 | -1.3265 | 0.538 | 0.80 | 140.8200 |
| GC | 2019 | 52 | 0.5831 | 0.538 | 1.07 | 138.7200 |
| GC | 2020 | 52 | -7.9650 | 0.462 | 0.64 | 544.2800 |
| GC | 2021 | 52 | -0.6188 | 0.462 | 0.95 | 204.6400 |
| GC | 2022 | 52 | -2.1323 | 0.481 | 0.86 | 266.7800 |
| GC | 2023 | 51 | -8.9361 | 0.353 | 0.52 | 614.0400 |
| GC | 2024 | 53 | 1.8185 | 0.528 | 1.13 | 327.8800 |
| GC | 2025 | 52 | 9.0985 | 0.519 | 1.27 | 566.4400 |
| GC | 2026 | 31 | 7.6858 | 0.452 | 1.11 | 1410.7200 |

## 17. Train / holdout

TRAIN ≤ 2022-12-30. HOLDOUT ≥ 2023-01-03. No holdout-based parameter selection.

- **ES:** train n=647 E=0.8535; holdout n=188 E=0.1673
- **NQ:** train n=647 E=1.9480; holdout n=188 E=-0.6215
- **GC:** train n=648 E=-1.7545; holdout n=187 E=1.8825

## 18. Walk-forward

Predeclared expanding folds. Test-block expectancy only.

| Instrument | Fold | Test window | N | E[pts] |
|---|---|---|---:|---:|
| ES | WF1 | 2015-01-02–2016-12-30 | 103 | -0.9902 |
| ES | WF2 | 2017-01-03–2018-12-31 | 103 | 4.3229 |
| ES | WF3 | 2019-01-02–2020-12-31 | 104 | 5.0234 |
| ES | WF4 | 2021-01-04–2022-12-30 | 104 | 1.5738 |
| ES | WF5 | 2023-01-03–2026-08-17 | 188 | 0.1673 |
| NQ | WF1 | 2015-01-02–2016-12-30 | 103 | -0.5325 |
| NQ | WF2 | 2017-01-03–2018-12-31 | 103 | 4.6010 |
| NQ | WF3 | 2019-01-02–2020-12-31 | 104 | 22.4659 |
| NQ | WF4 | 2021-01-04–2022-12-30 | 104 | -19.6832 |
| NQ | WF5 | 2023-01-03–2026-08-17 | 188 | -0.6215 |
| GC | WF1 | 2015-01-02–2016-12-30 | 103 | -0.3808 |
| GC | WF2 | 2017-01-03–2018-12-31 | 103 | -1.5536 |
| GC | WF3 | 2019-01-02–2020-12-31 | 104 | -3.6910 |
| GC | WF4 | 2021-01-04–2022-12-30 | 104 | -1.3756 |
| GC | WF5 | 2023-01-03–2026-08-17 | 187 | 1.8825 |

## 19. Parameter stability

Tiny predeclared family only. A robust effect should appear across neighbors, not one cell.

| Instrument | Candidate | N | Full E | Holdout E |
|---|---|---:|---:|---:|
| ES | TSMOM_5D_1D | 4185 | -1.0845 | -1.7501 |
| ES | TSMOM_10D_3D | 1395 | -2.2047 | -2.6405 |
| ES | TSMOM_20D_5D | 835 | 0.6990 | 0.1673 |
| ES | TSMOM_60D_5D | 827 | -0.8796 | 7.3961 |
| ES | TSMOM_20D_DAILY_REFRESH | 372 | 0.1102 | 2.1588 |
| NQ | TSMOM_5D_1D | 4188 | -2.9891 | -7.5060 |
| NQ | TSMOM_10D_3D | 1395 | -4.1428 | -4.3529 |
| NQ | TSMOM_20D_5D | 835 | 1.3695 | -0.6215 |
| NQ | TSMOM_60D_5D | 827 | 0.5025 | 6.0566 |
| NQ | TSMOM_20D_DAILY_REFRESH | 390 | 0.5064 | -35.2262 |
| GC | TSMOM_5D_1D | 4192 | 0.0162 | 1.1987 |
| GC | TSMOM_10D_3D | 1395 | 0.3738 | 0.2606 |
| GC | TSMOM_20D_5D | 835 | -0.9400 | 1.8825 |
| GC | TSMOM_60D_5D | 827 | 1.0022 | 8.5434 |
| GC | TSMOM_20D_DAILY_REFRESH | 426 | 0.1870 | 0.0986 |

MA(20) and Donchian-20 are robustness checks, not new families.

- **ES MA20 hold5 E=-3.9123; Donchian20 hold5 E=-2.3262**
- **NQ MA20 hold5 E=-6.1186; Donchian20 hold5 E=-1.5355**
- **GC MA20 hold5 E=0.1711; Donchian20 hold5 E=2.8489**

## 20. Costs

Commission + 0 / 1 / 2 ticks adverse entry and exit.

- **ES:** ideal (0 tick, no comm in `ideal` column uses gross points) E=1.2790; 1-tick+comm E=0.6990; 2-tick+comm E=0.1990
- **NQ:** ideal (0 tick, no comm in `ideal` column uses gross points) E=2.0695; 1-tick+comm E=1.3695; 2-tick+comm E=0.8695
- **GC:** ideal (0 tick, no comm in `ideal` column uses gross points) E=-0.7000; 1-tick+comm E=-0.9400; 2-tick+comm E=-1.1400

## 21. Drawdown / Monte Carlo

Trade-order shuffle on non-overlapping primary trades. This destroys residual serial dependence; documented as a limitation.

- **ES:** sample maxDD=1108.9600 consec_loss=8; MC p95 DD=1392.8500 p05 terminal=583.7000 p95 consec=11
- **NQ:** sample maxDD=8807.4000 consec_loss=6; MC p95 DD=6447.3000 p05 terminal=1143.5000 p95 consec=11
- **GC:** sample maxDD=2089.9200 consec_loss=10; MC p95 DD=1333.1800 p05 terminal=-784.9000 p95 consec=14

## 22. Portfolio relationship

Read-only overlap vs frozen books. No combination search.

- **ES:** {'instrument': 'ES'}
- **NQ:** {'instrument': 'NQ', 'dvp': {'n_trend_active_days': 835, 'n_dvp_days': 1690, 'overlap_days': 342, 'daily_pnl_correlation': -0.0663388863462882, 'note': 'Read-only vs Phase 29 NQ DVP historical trades. No combination.'}}
- **GC:** {'instrument': 'GC', 'gc_vwap': {'journal': None, 'n_trend_active_days': 835, 'n_gc_days': 0, 'overlap_days': 0, 'daily_pnl_correlation': None, 'note': 'Read-only vs frozen GC VWAP journal if present.'}}

## 23. Multi-market diagnostic

{
  "instruments": [
    "ES",
    "NQ",
    "GC"
  ],
  "weights_vol_scaled": {
    "ES": 1.0,
    "NQ": 0.22732860492124995,
    "GC": 1.3850267767324818
  },
  "equal_weight": {
    "n_days": 1259,
    "total_points_unit": 314.1000000000413,
    "mean": 0.24948371723593432,
    "max_dd": 3427.679999999989,
    "sharpe_daily": 0.03796194339552676,
    "sortino_daily": 0.05314291301061215,
    "pct_exposed": 1.0
  },
  "equal_risk": {
    "n_days": 1259,
    "total_points_unit": -81.15241910990105,
    "mean": -0.0644578388482137,
    "max_dd": 1264.8045909849875,
    "sharpe_daily": -0.024243816098018102,
    "sortino_daily": -0.03386613910638294,
    "pct_exposed": 1.0
  },
  "correlation": {
    "ES_NQ": 0.712270185324781,
    "ES_GC": 0.005641856960201178,
    "NQ_GC": 0.007443453034563825
  },
  "note": "Diagnostic only. Points are not dollar-normalized across products; equal-risk uses daily P&L vol."
}

## 24. Prop geometry

- **ES:** {'avg_overnight_points': 0.011377245508982036, 'largest_overnight_path_abs_points': 70.25, 'worst_signed_entry_gap_points': -50.25, 'avg_usd_per_contract_per_trade': 34.95209580838325, 'worst_trade_points': -276.83, 'worst_trade_usd': -13841.5, 'max_consec_losses': 8, 'avg_hold_days': 5.0, 'pct_time_exposed_approx': 0.9949952335557674, 'weekend_path_abs_mean': 4.591616766467066, 'n_trades': 835}
- **NQ:** {'avg_overnight_points': 1.085928143712575, 'largest_overnight_path_abs_points': 142.25, 'worst_signed_entry_gap_points': -182.5, 'avg_usd_per_contract_per_trade': 27.38922155688618, 'worst_trade_points': -1649.7, 'worst_trade_usd': -32994.0, 'max_consec_losses': 6, 'avg_hold_days': 5.0, 'pct_time_exposed_approx': 0.9949952335557674, 'weekend_path_abs_mean': 14.073053892215569, 'n_trades': 835}
- **GC:** {'avg_overnight_points': -0.3288622754491063, 'largest_overnight_path_abs_points': 34.100000000000364, 'worst_signed_entry_gap_points': -10.399999999999864, 'avg_usd_per_contract_per_trade': -93.99999999998515, 'worst_trade_points': -457.6400000000013, 'worst_trade_usd': -45764.00000000013, 'max_consec_losses': 10, 'avg_hold_days': 5.0, 'pct_time_exposed_approx': 0.9945212005717008, 'weekend_path_abs_mean': 2.5941317365269496, 'n_trades': 835}

## 25. Recommendation

Aggregate returns are only mildly positive or fail holdout / year / neighborhood tests. Do not freeze. Do not optimize lookbacks. Close or park this family unless a later independent sample arrives.

Execution remained `DRY_RUN`. `strategy_frozen/` was not written.

No candidate JSON (edge not found at the freeze gate).
