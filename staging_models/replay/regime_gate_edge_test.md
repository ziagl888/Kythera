# Regime-Gate Edge Test (Phase B) — T-2026-KYT-9050-032

_generated 2026-07-23 06:42:50.388954+00:00 · read-only · regime_history 73436 rows (2026-01-18→2026-07-23) · 276221 regime-joined trades · legs with n≥150: 61_

**Gate-Test:** günstige BTC-Regimes (mean-net>0, cell-n≥20) auf der ERSTEN Trade-Hälfte gelernt, auf der ZWEITEN angewandt (OUT-OF-SAMPLE). `ungated`/`gated net%` = mean gestaffelter unlevered Move − Fee auf dem Test-Split; `Δ`=gated−ungated; `kept`=Anteil des Test-Flows, den das Gate durchlässt. RULE-Regime = debounced RULE_recon (T-031, 91.85% fidelity). RESCUED = ungated<0→gated>0. **Empfehlung, kein Rollout.**

## Executive Summary

- **RESCUED (Negativ→Positiv durch Gate): 0** — KEIN Leg. Kein Regime-Gate flippt einen Negativ-Edge-Leg out-of-sample ins Plus.
- **Retire bestätigt (kein günstiges Regime existiert, Gate blockt alles): 15** — AIM1/S, BB_4H/S, BR1H/S, BR2H/S, BR4H/S, QM_1H/S, ATB1/S, MIS1-8h/L, 5Percent/L, 5Percent/S, FastInOut/L, FastInOut/S, VolIndic/L, VolIndic/S, Main Channel/L. Diese bluten in JEDEM Regime → Gating hilft nicht, Retire/Richtungs-Abschaltung steht.
- **Negativ-Edge nur verbessert, bleibt aber negativ: 4** — BB_1H/S (-2.49→-2.33), BR1Hv2/S (-0.97→-0.83), QM_4H/S (-1.40→-1.19), SR/S (-0.22→-0.16). Gate mildert, rettet aber nicht.
- **Positiv-Edge durch Gate verbessert (OOS): 14** — BB_1H/L (Δ+0.27), BB_4H/L (Δ+0.25), BR1Hv2/L (Δ+0.52), MIS1-168h/L (Δ+0.20), MIS1-72h/L (Δ+0.28), QM_1H/L (Δ+0.18), ROM1/S (Δ+0.07), RUB1/L (Δ+0.02), TD_1H/L (Δ+0.02), EPD1/L (Δ+1.03), TD_4H/L (Δ+0.29), TD_4H/S (Δ+1.86), MIS1-24h/S (Δ+0.53), MIS1-8h/S (Δ+0.08). Meist bescheiden (<+0.3%/Trade) und/oder bei niedriger kept-fraction; das existierende Whitelist-v2-Vehikel (T-069) ist der Live-Weg, kein neues Gate.
- **Kernbefund:** Der Edge der Verlust-Legs ist RICHTUNGS-, nicht regime-bedingt (Pattern/Sniper/Rubberband-Familien: LONG-Edge, SHORT-Blutung über ALLE Regimes) → der Hebel ist die Richtungs-/Retire-Entscheidung, nicht ein BTC-Regime-Gate. Deckt sich mit T-029/T-031 (η²≈0, Regime trennt Churn, nicht Richtung).

## RULE-Gate — Negativ-Edge-Legs (rettet ein Gate sie?)

| tag | dir | lc | n | ungated net% | gated net% | Δ | kept | favorable regimes | verdict |
|---|---|---|--:|--:|--:|--:|--:|---|---|
| QM_4H | SHORT | act | 401 | -1.399 | -1.188 | +0.211 | 0.41 | TRAN | IMPROVED |
| BB_1H | SHORT | act | 2362 | -2.493 | -2.326 | +0.167 | 0.21 | HIGH | IMPROVED |
| BR1Hv2 | SHORT | act | 811 | -0.967 | -0.832 | +0.135 | 0.59 | CHOP | IMPROVED |
| SR | SHORT | act | 2010 | -0.221 | -0.156 | +0.066 | 0.84 | CHOP,HIGH,TRAN | IMPROVED |
| ROM1 | LONG | act | 1449 | -0.437 | -0.434 | +0.003 | 0.74 | CHOP,TRAN | NO-HELP |
| AIM1 | SHORT | ret | 2190 | -0.866 | — | — | 0.00 | — | NO-FAV-REGIME |
| BB_4H | SHORT | act | 1681 | -0.438 | — | — | 0.00 | — | NO-FAV-REGIME |
| BR1H | SHORT | act | 3328 | -3.741 | — | — | 0.00 | — | NO-FAV-REGIME |
| BR2H | SHORT | act | 2242 | -1.942 | — | — | 0.00 | — | NO-FAV-REGIME |
| BR4H | SHORT | act | 908 | -2.358 | — | — | 0.00 | — | NO-FAV-REGIME |
| EPD3 | SHORT | act | 3580 | -0.455 | -0.455 | +0.000 | 1.00 | CHOP,HIGH,TRAN,TREN,TREN | NO-HELP |
| QM_1H | SHORT | act | 1561 | -0.427 | — | — | 0.00 | — | NO-FAV-REGIME |
| ATB1 | SHORT | ret | 268 | -0.653 | — | — | 0.00 | — | NO-FAV-REGIME |
| MIS1-8h | LONG | ret | 221 | -0.036 | — | — | 0.00 | — | NO-FAV-REGIME |
| 5Percent | LONG | act | 1252 | -0.202 | — | — | 0.00 | — | NO-FAV-REGIME |
| 5Percent | SHORT | act | 18618 | -0.338 | — | — | 0.00 | — | NO-FAV-REGIME |
| FastInOut | LONG | act | 10320 | -0.192 | — | — | 0.00 | — | NO-FAV-REGIME |
| FastInOut | SHORT | act | 104191 | -0.218 | — | — | 0.00 | — | NO-FAV-REGIME |
| VolIndic | LONG | act | 29535 | -0.197 | — | — | 0.00 | — | NO-FAV-REGIME |
| VolIndic | SHORT | act | 27354 | -0.007 | — | — | 0.00 | — | NO-FAV-REGIME |
| Main Channel | LONG | act | 216 | -0.610 | — | — | 0.00 | — | NO-FAV-REGIME |
| Main Channel | SHORT | act | 172 | -0.463 | -0.546 | -0.083 | 0.60 | CHOP,HIGH | WORSE |
| SR | LONG | act | 2240 | -0.237 | -0.346 | -0.108 | 0.54 | CHOP,TREN | WORSE |
| ATS1 | LONG | ret | 1738 | -0.367 | -0.535 | -0.169 | 0.70 | CHOP,TRAN | WORSE |
| TSM1 | SHORT | act | 376 | -0.433 | -0.677 | -0.244 | 0.09 | HIGH | WORSE |

## RULE-Gate — Positiv-Edge-Legs (verbessert ein Gate sie?)

| tag | dir | lc | n | ungated net% | gated net% | Δ | kept | favorable regimes | verdict |
|---|---|---|--:|--:|--:|--:|--:|---|---|
| TD_4H | SHORT | act | 256 | +2.756 | +4.613 | +1.857 | 0.28 | TRAN | IMPROVED |
| EPD1 | LONG | act | 622 | +0.204 | +1.231 | +1.027 | 0.74 | HIGH,TRAN | IMPROVED |
| MIS1-24h | SHORT | ret | 239 | +1.706 | +2.233 | +0.527 | 0.40 | CHOP | IMPROVED |
| BR1Hv2 | LONG | act | 726 | +0.149 | +0.668 | +0.520 | 0.22 | TRAN,TREN | IMPROVED |
| TD_4H | LONG | act | 414 | +2.015 | +2.308 | +0.293 | 0.62 | HIGH,TRAN | IMPROVED |
| MIS1-72h | LONG | ret | 11869 | +1.768 | +2.051 | +0.283 | 0.42 | TRAN | IMPROVED |
| BB_1H | LONG | act | 1726 | +2.116 | +2.385 | +0.269 | 0.61 | HIGH,TRAN | IMPROVED |
| BB_4H | LONG | act | 1265 | +1.384 | +1.629 | +0.245 | 0.93 | CHOP,HIGH,TRAN | IMPROVED |
| MIS1-168h | LONG | ret | 7301 | +1.748 | +1.950 | +0.202 | 0.81 | CHOP,TRAN | IMPROVED |
| QM_1H | LONG | act | 1599 | +0.400 | +0.582 | +0.182 | 0.63 | HIGH,TRAN | IMPROVED |
| MIS1-8h | SHORT | ret | 385 | +1.581 | +1.657 | +0.075 | 0.99 | CHOP,HIGH,TRAN | IMPROVED |
| ROM1 | SHORT | act | 3141 | +0.428 | +0.502 | +0.073 | 0.77 | CHOP,TRAN | IMPROVED |
| TD_1H | LONG | act | 1366 | +1.612 | +1.637 | +0.025 | 0.98 | CHOP,HIGH,TRAN | IMPROVED |
| RUB1 | LONG | act | 1082 | +2.342 | +2.364 | +0.022 | 0.99 | CHOP,HIGH,TRAN | IMPROVED |
| SRA1 | SHORT | act | 388 | +0.052 | +0.067 | +0.015 | 0.67 | CHOP,TRAN | NO-HELP |
| BR1H | LONG | act | 3627 | +2.630 | +2.643 | +0.013 | 0.41 | TRAN | NO-HELP |
| EPD1 | SHORT | act | 4141 | +1.835 | +1.839 | +0.003 | 1.00 | CHOP,HIGH,TRAN | NO-HELP |
| ATS1 | SHORT | ret | 825 | +0.935 | — | — | 0.00 | — | NO-FAV-REGIME |
| ATS2 | LONG | sha | 612 | +1.167 | — | — | 0.00 | — | NO-FAV-REGIME |
| BR1D | SHORT | act | 186 | +2.692 | — | — | 0.00 | — | NO-FAV-REGIME |
| QM_4H | LONG | act | 155 | +3.455 | — | — | 0.00 | — | NO-FAV-REGIME |
| RUB1 | SHORT | act | 1492 | +2.722 | — | — | 0.00 | — | NO-FAV-REGIME |
| TD_1H | SHORT | act | 1049 | +1.554 | — | — | 0.00 | — | NO-FAV-REGIME |
| AIM1 | LONG | ret | 909 | +0.396 | — | — | 0.00 | — | NO-FAV-REGIME |
| AIM2 | LONG | act | 1230 | +0.139 | +0.126 | -0.014 | 0.40 | HIGH,TRAN,TREN,TREN | NO-HELP |
| SRA1 | LONG | act | 363 | +0.008 | -0.019 | -0.027 | 0.78 | CHOP,HIGH,TRAN | WORSE |
| AIM2 | SHORT | act | 1099 | +0.220 | +0.149 | -0.071 | 0.89 | CHOP,HIGH,TRAN,TREN | WORSE |
| SRA2 | LONG | act | 319 | +0.940 | +0.863 | -0.077 | 0.86 | CHOP,HIGH,TRAN | WORSE |
| RUB2 | SHORT | act | 240 | +0.358 | +0.068 | -0.290 | 0.61 | CHOP,HIGH | WORSE |
| SRA2 | SHORT | sha | 222 | +1.050 | +0.739 | -0.311 | 0.59 | CHOP,TREN | WORSE |
| EPD3 | LONG | sha | 2633 | +0.614 | +0.266 | -0.349 | 0.11 | TREN | WORSE |
| BR2H | LONG | act | 2193 | +1.571 | +0.990 | -0.581 | 0.22 | HIGH | WORSE |
| MIS1-72h | SHORT | ret | 302 | +3.541 | +2.911 | -0.631 | 0.66 | HIGH,TRAN | WORSE |
| BR4H | LONG | act | 949 | +1.983 | +0.982 | -1.001 | 0.38 | CHOP | WORSE |
| MIS2-8h | LONG | act | 245 | +1.126 | -0.471 | -1.597 | 0.22 | TRAN | WORSE |
| MIS1-24h | LONG | ret | 214 | +2.763 | +0.609 | -2.154 | 0.35 | TRAN | WORSE |

## SOFT-Gate (hl=192, T-031-Anschluss) — alle Legs

| tag | dir | lc | n | ungated net% | gated net% | Δ | kept | favorable regimes | verdict |
|---|---|---|--:|--:|--:|--:|--:|---|---|
| ATB1 | SHORT | ret | 268 | -0.653 | +2.249 | +2.902 | 0.01 | TRAN | RESCUED |
| RUB1 | SHORT | act | 1492 | +2.722 | +5.482 | +2.760 | 0.07 | TRAN | IMPROVED |
| ATS1 | SHORT | ret | 825 | +0.935 | +3.210 | +2.275 | 0.26 | HIGH,TREN | IMPROVED |
| BB_1H | SHORT | act | 2362 | -2.493 | -0.258 | +2.235 | 0.13 | HIGH,TRAN,TREN | IMPROVED |
| BR1H | SHORT | act | 3328 | -3.741 | -1.801 | +1.941 | 0.14 | HIGH,TREN | IMPROVED |
| RUB1 | LONG | act | 1082 | +2.342 | +3.636 | +1.294 | 0.45 | CHOP,TREN | IMPROVED |
| BR1H | LONG | act | 3627 | +2.630 | +3.529 | +0.899 | 0.01 | TREN | IMPROVED |
| BR4H | SHORT | act | 908 | -2.358 | -1.594 | +0.764 | 0.13 | TREN | IMPROVED |
| BR2H | SHORT | act | 2242 | -1.942 | -1.238 | +0.705 | 0.03 | TRAN,TREN | IMPROVED |
| Main Channel | LONG | act | 216 | -0.610 | +0.012 | +0.623 | 0.06 | TREN | RESCUED |
| 5Percent | SHORT | act | 18618 | -0.338 | +0.070 | +0.409 | 0.19 | HIGH | RESCUED |
| 5Percent | LONG | act | 1252 | -0.202 | +0.111 | +0.313 | 0.05 | TREN | RESCUED |
| BR4H | LONG | act | 949 | +1.983 | +2.274 | +0.291 | 0.74 | CHOP | IMPROVED |
| Main Channel | SHORT | act | 172 | -0.463 | -0.252 | +0.211 | 0.78 | CHOP | IMPROVED |
| SRA1 | SHORT | act | 388 | +0.052 | +0.126 | +0.074 | 0.70 | CHOP,TREN | IMPROVED |
| MIS1-8h | SHORT | ret | 385 | +1.581 | +1.637 | +0.055 | 0.82 | CHOP,HIGH,TREN | IMPROVED |
| BB_1H | LONG | act | 1726 | +2.116 | +2.167 | +0.051 | 0.98 | CHOP,HIGH,TRAN,TREN | IMPROVED |
| ROM1 | SHORT | act | 3141 | +0.428 | +0.475 | +0.047 | 0.82 | CHOP,HIGH | IMPROVED |
| BB_4H | LONG | act | 1265 | +1.384 | +1.420 | +0.036 | 0.88 | CHOP,HIGH | IMPROVED |
| QM_1H | LONG | act | 1599 | +0.400 | +0.407 | +0.007 | 1.00 | CHOP,HIGH,TREN | NO-HELP |
| AIM1 | SHORT | ret | 2190 | -0.866 | — | — | 0.00 | — | NO-FAV-REGIME |
| AIM2 | LONG | act | 1230 | +0.139 | — | — | 0.00 | TREN,TREN | NO-FAV-REGIME |
| AIM2 | SHORT | act | 1099 | +0.220 | +0.220 | +0.000 | 1.00 | CHOP,HIGH,TREN,TREN | NO-HELP |
| ATS2 | LONG | sha | 612 | +1.167 | — | — | 0.00 | — | NO-FAV-REGIME |
| BB_4H | SHORT | act | 1681 | -0.438 | — | — | 0.00 | — | NO-FAV-REGIME |
| BR1D | SHORT | act | 186 | +2.692 | — | — | 0.00 | — | NO-FAV-REGIME |
| BR1Hv2 | LONG | act | 726 | +0.149 | — | — | 0.00 | TREN | NO-FAV-REGIME |
| BR1Hv2 | SHORT | act | 811 | -0.967 | -0.967 | +0.000 | 1.00 | CHOP,HIGH | NO-HELP |
| EPD3 | LONG | sha | 2633 | +0.614 | — | — | 0.00 | HIGH | NO-FAV-REGIME |
| EPD3 | SHORT | act | 3580 | -0.455 | -0.455 | +0.000 | 1.00 | CHOP,HIGH | NO-HELP |
| QM_1H | SHORT | act | 1561 | -0.427 | — | — | 0.00 | — | NO-FAV-REGIME |
| QM_4H | LONG | act | 155 | +3.455 | — | — | 0.00 | — | NO-FAV-REGIME |
| RUB2 | SHORT | act | 240 | +0.358 | +0.358 | +0.000 | 1.00 | CHOP,HIGH | NO-HELP |
| SRA2 | LONG | act | 319 | +0.940 | +0.940 | +0.000 | 1.00 | CHOP | NO-HELP |
| TSM1 | SHORT | act | 376 | -0.433 | — | — | 0.00 | — | NO-FAV-REGIME |
| QM_4H | SHORT | act | 401 | -1.399 | — | — | 0.00 | — | NO-FAV-REGIME |
| SRA2 | SHORT | sha | 222 | +1.050 | +1.050 | +0.000 | 1.00 | CHOP | NO-HELP |
| TD_4H | SHORT | act | 256 | +2.756 | — | — | 0.00 | — | NO-FAV-REGIME |
| MIS1-24h | LONG | ret | 214 | +2.763 | — | — | 0.00 | — | NO-FAV-REGIME |
| MIS2-8h | LONG | act | 245 | +1.126 | — | — | 0.00 | TREN,TREN | NO-FAV-REGIME |
| MIS1-8h | LONG | ret | 221 | -0.036 | — | — | 0.00 | — | NO-FAV-REGIME |
| FastInOut | LONG | act | 10320 | -0.192 | — | — | 0.00 | — | NO-FAV-REGIME |
| FastInOut | SHORT | act | 104191 | -0.218 | — | — | 0.00 | — | NO-FAV-REGIME |
| SR | LONG | act | 2240 | -0.237 | -0.246 | -0.009 | 0.07 | TRAN,TREN | NO-HELP |
| MIS1-168h | LONG | ret | 7301 | +1.748 | +1.718 | -0.030 | 0.92 | CHOP,HIGH,TREN | WORSE |
| VolIndic | SHORT | act | 27354 | -0.007 | -0.041 | -0.034 | 0.03 | TRAN | WORSE |
| MIS1-24h | SHORT | ret | 239 | +1.706 | +1.663 | -0.043 | 0.78 | CHOP,TREN | WORSE |
| SR | SHORT | act | 2010 | -0.221 | -0.279 | -0.058 | 0.82 | CHOP,HIGH | WORSE |
| EPD1 | LONG | act | 622 | +0.204 | +0.119 | -0.085 | 0.33 | HIGH | WORSE |
| VolIndic | LONG | act | 29535 | -0.197 | -0.379 | -0.182 | 0.06 | TREN | WORSE |
| SRA1 | LONG | act | 363 | +0.008 | -0.239 | -0.247 | 0.69 | CHOP,TREN | WORSE |
| MIS1-72h | LONG | ret | 11869 | +1.768 | +1.419 | -0.350 | 0.35 | HIGH,TRAN,TREN,TREN | WORSE |
| ATS1 | LONG | ret | 1738 | -0.367 | -0.737 | -0.370 | 0.70 | CHOP,TREN | WORSE |
| TD_4H | LONG | act | 414 | +2.015 | +1.628 | -0.387 | 0.67 | CHOP | WORSE |
| ROM1 | LONG | act | 1449 | -0.437 | -0.886 | -0.449 | 0.10 | HIGH,TRAN,TREN | WORSE |
| BR2H | LONG | act | 2193 | +1.571 | +0.595 | -0.976 | 0.10 | TREN | WORSE |
| TD_1H | LONG | act | 1366 | +1.612 | +0.608 | -1.004 | 0.42 | CHOP | WORSE |
| EPD1 | SHORT | act | 4141 | +1.835 | +0.770 | -1.065 | 0.89 | CHOP,HIGH,TRAN,TREN | WORSE |
| TD_1H | SHORT | act | 1049 | +1.554 | +0.169 | -1.385 | 0.19 | HIGH | WORSE |
| AIM1 | LONG | ret | 909 | +0.396 | -2.200 | -2.597 | 0.09 | TREN | WORSE |
| MIS1-72h | SHORT | ret | 302 | +3.541 | -1.316 | -4.857 | 0.20 | HIGH | WORSE |

## Per-Regime Mean-Net-Edge je Leg (Vollstichprobe, mean-net×n)

| tag | dir | lc | overall | TREN | TREN | CHOP | HIGH | TRAN |
|---|---|---|--:|--:|--:|--:|--:|--:|
| 5Percent | LONG | act | -0.338 | -1.61×13 | +0.02×10 | -0.33×456 | -0.32×210 | -0.33×563 |
| 5Percent | SHORT | act | -0.491 | -0.63×17 | +0.11×46 | -0.46×5038 | -0.12×4315 | -0.68×9202 |
| AIM2 | LONG | act | +0.089 | +0.71×88 | -0.24×126 | -0.01×512 | +0.01×194 | +0.26×310 |
| AIM2 | SHORT | act | +1.184 | +1.72×86 | +0.08×112 | +1.17×512 | +1.77×178 | +1.08×211 |
| BB_1H | LONG | act | +1.252 | +7.54×6 | +16.92×1 | +0.73×624 | +1.85×336 | +1.35×759 |
| BB_1H | SHORT | act | -1.544 | — | -6.01×4 | -1.41×1045 | -1.50×364 | -1.69×949 |
| BB_4H | LONG | act | +1.175 | -2.12×17 | -1.53×29 | +1.41×537 | +1.74×193 | +0.97×489 |
| BB_4H | SHORT | act | -0.794 | -0.39×43 | -0.36×65 | -0.37×644 | -0.82×269 | -1.26×660 |
| BR1D | SHORT | act | -0.473 | — | -0.10×1 | +0.53×55 | -2.00×58 | -0.01×72 |
| BR1H | LONG | act | +1.192 | +2.94×2 | — | +0.52×1118 | +1.99×792 | +1.26×1715 |
| BR1H | SHORT | act | -2.233 | -2.51×2 | -6.18×12 | -2.30×1014 | -2.88×565 | -1.95×1735 |
| BR1Hv2 | LONG | act | -0.159 | -0.17×91 | +0.95×38 | -0.21×325 | -1.14×138 | +0.67×134 |
| BR1Hv2 | SHORT | act | -0.588 | +1.33×22 | -1.51×103 | -0.25×371 | -1.00×109 | -0.71×206 |
| BR2H | LONG | act | +0.680 | -0.16×66 | -0.59×26 | +0.96×643 | +0.60×510 | +0.62×948 |
| BR2H | SHORT | act | -1.487 | +0.28×8 | -1.73×109 | -1.64×785 | -2.20×324 | -1.13×1016 |
| BR4H | LONG | act | +1.098 | +1.58×19 | -0.70×18 | +1.38×287 | +0.78×196 | +1.11×429 |
| BR4H | SHORT | act | -1.618 | -2.23×7 | -1.69×54 | -2.02×313 | -1.25×146 | -1.41×388 |
| EPD1 | LONG | act | -0.092 | +17.26×2 | — | -1.82×244 | -1.14×136 | +2.11×240 |
| EPD1 | SHORT | act | +3.358 | +0.98×8 | +2.99×13 | +3.25×1677 | +2.56×1179 | +4.26×1264 |
| EPD3 | SHORT | act | -0.062 | -0.17×397 | +0.16×263 | -0.12×1742 | +0.22×522 | -0.16×656 |
| FastInOut | LONG | act | -0.334 | +0.10×139 | -0.31×100 | -0.42×3154 | -0.29×2050 | -0.31×4877 |
| FastInOut | SHORT | act | -0.305 | -0.56×315 | -0.46×291 | -0.17×31508 | -0.36×22590 | -0.36×49487 |
| MIS2-8h | LONG | act | -0.034 | +1.26×30 | -1.92×26 | +1.25×83 | -2.51×42 | +0.09×64 |
| Main Channel | LONG | act | -0.529 | -0.01×19 | -0.87×20 | -0.59×56 | -1.08×52 | -0.11×69 |
| Main Channel | SHORT | act | -0.283 | +0.01×2 | +1.08×4 | +0.06×55 | -0.61×45 | -0.43×66 |
| QM_1H | LONG | act | +0.317 | -7.11×1 | +1.91×3 | -0.03×541 | +0.27×289 | +0.58×765 |
| QM_1H | SHORT | act | -0.413 | -1.04×1 | -2.60×3 | -0.25×560 | -1.03×288 | -0.29×709 |
| QM_4H | LONG | act | +1.051 | — | -6.04×1 | +1.40×44 | +2.24×28 | +0.54×82 |
| QM_4H | SHORT | act | -1.097 | — | — | -1.70×98 | -1.73×108 | -0.44×195 |
| ROM1 | LONG | act | -0.025 | +0.42×59 | +0.20×48 | -0.09×597 | -0.66×353 | +0.54×392 |
| ROM1 | SHORT | act | +0.476 | -0.98×94 | -0.26×81 | +0.71×1414 | +0.17×424 | +0.48×1128 |
| RUB1 | LONG | act | +2.483 | -1.58×3 | — | +3.37×325 | +0.35×264 | +3.07×490 |
| RUB1 | SHORT | act | +0.781 | -12.52×4 | — | +1.17×515 | -1.04×285 | +1.32×688 |
| RUB2 | SHORT | act | +0.664 | -0.37×32 | +1.76×22 | +0.57×102 | +1.35×45 | +0.35×39 |
| SR | LONG | act | -0.202 | +0.06×161 | -0.56×148 | -0.17×809 | -0.20×457 | -0.22×665 |
| SR | SHORT | act | -0.026 | -0.56×52 | -0.63×110 | +0.02×803 | +0.04×388 | +0.02×657 |
| SRA1 | LONG | act | +0.624 | +1.42×22 | -0.66×21 | +0.86×134 | +0.23×77 | +0.70×109 |
| SRA1 | SHORT | act | +0.047 | +0.14×8 | -0.98×26 | +0.89×131 | -1.11×100 | +0.30×123 |
| SRA2 | LONG | act | +0.853 | +2.15×24 | +0.65×18 | +0.70×163 | +0.96×49 | +0.74×65 |
| TD_1H | LONG | act | +1.480 | -8.60×3 | +2.90×11 | +1.46×388 | +2.26×397 | +0.98×567 |
| TD_1H | SHORT | act | -0.002 | -2.87×9 | -0.25×7 | -0.38×399 | +0.28×220 | +0.28×414 |
| TD_4H | LONG | act | +1.277 | +3.01×2 | -4.94×1 | +0.06×117 | +2.05×95 | +1.64×199 |
| TD_4H | SHORT | act | +0.717 | +4.48×6 | -1.56×8 | +1.04×91 | -2.66×58 | +2.46×93 |
| TSM1 | SHORT | act | -0.220 | +0.69×5 | -0.78×26 | -0.04×223 | +0.21×53 | -1.01×69 |
| VolIndic | LONG | act | -0.254 | +0.42×250 | +0.20×158 | -0.43×10026 | -0.10×6706 | -0.21×12395 |
| VolIndic | SHORT | act | -0.185 | -0.66×219 | -1.14×200 | -0.07×8411 | -0.11×6474 | -0.29×12050 |
| AIM1 | LONG | ret | -0.211 | -0.83×2 | +4.50×3 | -0.87×351 | +0.40×141 | +0.11×412 |
| AIM1 | SHORT | ret | -1.782 | -0.70×2 | -12.14×2 | -1.40×710 | -2.27×446 | -1.81×1030 |
| ATB1 | SHORT | ret | -0.392 | — | — | -0.25×63 | -0.55×100 | -0.32×105 |
| ATS1 | LONG | ret | +0.297 | +0.78×50 | +0.17×44 | -0.09×717 | -0.19×383 | +1.12×544 |
| ATS1 | SHORT | ret | +0.203 | -0.60×19 | -2.19×20 | +0.55×299 | +1.91×163 | -0.78×324 |
| MIS1-168h | LONG | ret | +0.974 | -6.34×2 | +6.75×3 | +1.30×2647 | +0.35×1130 | +0.93×3519 |
| MIS1-24h | LONG | ret | +2.170 | +2.77×2 | -12.10×1 | +1.47×77 | +3.33×42 | +2.37×92 |
| MIS1-24h | SHORT | ret | +0.663 | +30.07×2 | — | +1.61×92 | -1.46×39 | +0.07×106 |
| MIS1-72h | LONG | ret | +1.252 | — | — | +0.72×3497 | +1.18×1713 | +1.55×6659 |
| MIS1-72h | SHORT | ret | +1.554 | +35.06×1 | +17.48×1 | +0.01×81 | -0.71×79 | +3.37×140 |
| MIS1-8h | LONG | ret | -2.324 | -12.35×2 | — | -5.38×85 | -0.45×47 | -0.11×87 |
| MIS1-8h | SHORT | ret | +2.372 | — | -12.88×1 | +0.42×128 | +1.74×61 | +3.93×195 |
| ATS2 | LONG | sha | +0.309 | +0.49×52 | +0.04×58 | +0.14×260 | +0.23×130 | +0.85×112 |
| EPD3 | LONG | sha | +0.150 | +0.27×281 | -0.05×137 | +0.26×1382 | -0.48×326 | +0.24×507 |
| SRA2 | SHORT | sha | +0.997 | +1.31×15 | +0.63×36 | +1.15×102 | +0.75×23 | +0.98×46 |

## Join-Grenzen (ehrlich)

- Regime = RULE_recon (debounced) aus dem gespeicherten regime-Stream; T-031 validierte das zu 91.85% gegen aufgezeichnetes regime_at_open. Residual = Warm-up + Ingestion-Outage-Desync.
- As-of-Join setzt voraus, dass trade.open_time (AI) / time (classic) und regime_history.ts DIESELBE naive Uhr tragen (R3-TZ-Baustelle, P1.8/UTC_POLICY). Ein systematischer Offset (z.B. +3h) würde die Regime-Zuordnung zeitlich verschieben; da Gated+Ungated denselben Offset teilen, bleibt der DIFF (und damit RESCUED/IMPROVED) robust, nur die absolute Zell-Attribution kann verschmieren.
- Der OOS-Gate-Uplift misst die REGIME-Achse allein — NICHT die Live-Whitelist-Mechanik (nicht historisch rekonstruierbar, T-031), Cornix-Routing oder Regime-Auto-Close. Er ist eine Obergrenze dessen, was Regime-Konditionierung theoretisch bringt; Live-Gating kann darunter liegen.
- Outcome = realized status (TP1-Touch-Win) → gestaffelter Move, Monitor-Rauschen (P1.2/P2.7) trifft gated+ungated gleich → der DIFF ist robuster als das Absolutniveau.
- Günstige Regimes werden datengetrieben gewählt (mean-net>0 auf Train) — bei 5 Regimes ist die Multiple-Comparison-Gefahr gering, aber der OOS-Split ist die eigentliche Absicherung; ein In-Sample-Gate wäre wertlos.
- TREND_UP/DOWN sind selten (je ~3-4% der Zeit) → in vielen Legs unter MIN_CELL und damit weder als günstig noch ungünstig klassifizierbar (Gate lässt sie NICHT durch — konservativ, kept-frac zeigt es).
- alt_context bleibt außen vor (SOFT smoothed nur die BTC-Achse; die per-Bot-Whitelist über bot×regime×alt×dir ist der eigentliche Live-Gate, aber nicht rekonstruierbar).
