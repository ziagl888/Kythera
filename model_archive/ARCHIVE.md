# Modell-/Code-Archiv (auto-generiert)

> Generiert von `tools/bot_variants/archive.py` (T-2026-KYT-9050-039). **Nicht von Hand editieren** — regenerieren mit `python -m tools.bot_variants.archive --write`.
>
> Reference-based: die Artefakt-Bytes liegen git-getrackt in root/staging; je Generation hält `manifest.json` md5 + `source_commit` ⇒ Retrieval via `git show <source_commit>:<path>`. `--copy-binaries` erzeugt ein self-contained Export.

**Generationen:** 48

| Family | Tag | Lifecycle | code_ref | Artefakte (Richtung:Datei@source_commit) | Manifest |
|---|---|---|---|---|---|
| ABR | `ABR2` | LONG:shadow, SHORT:shadow | HEAD | LONG:`bt2_model_LONG.json`@9ddaa8d0<br>SHORT:`bt2_model_SHORT.json`@14e1c6f3 | `model_archive/abr/ABR2/manifest.json` |
| AIM | `AIM1` | LONG:retired, SHORT:retired | `f760ba03` | — | `model_archive/aim/AIM1/manifest.json` |
| AIM | `AIM2` | LONG:live, SHORT:live | HEAD | LONG:`master_meta_model_aim2.pkl`@14e1c6f3<br>SHORT:`master_meta_model_aim2.pkl`@14e1c6f3 | `model_archive/aim/AIM2/manifest.json` |
| AIM | `AIM2-TOPN` | LONG:retired, SHORT:retired | `3a290931` | — | `model_archive/aim/AIM2-TOPN/manifest.json` |
| ATB | `ATB1` | LONG:silent, SHORT:silent | HEAD | — | `model_archive/atb/ATB1/manifest.json` |
| ATB | `ATB2` | LONG:live, SHORT:shadow | HEAD | LONG:`atb2_model_LONG.pkl`@a054b15c<br>SHORT:`atb2_model_SHORT.pkl`@14e1c6f3 | `model_archive/atb/ATB2/manifest.json` |
| ATS | `ATS1` | LONG:silent, SHORT:silent | HEAD | — | `model_archive/ats/ATS1/manifest.json` |
| ATS | `ATS1_ROBUST` | LONG:retired, SHORT:retired | `b6735d90` | LONG:`model_tsi_long_robust.pkl`@b6735d90<br>SHORT:`model_tsi_short_robust.pkl`@b6735d90 | `model_archive/ats/ATS1_ROBUST/manifest.json` |
| ATS | `ATS2` | LONG:live, SHORT:live | HEAD | LONG:`ats2_model_LONG.pkl`@14e1c6f3<br>SHORT:`ats2_model_SHORT.pkl`@14e1c6f3 | `model_archive/ats/ATS2/manifest.json` |
| BB | `BB2_4H` | LONG:shadow, SHORT:shadow | HEAD | — | `model_archive/bb/BB2_4H/manifest.json` |
| BB | `BB_1H` | LONG:live, SHORT:shadow | HEAD | LONG:`bb_xgboost_model_1h.pkl`@14e1c6f3<br>SHORT:`bb_xgboost_model_1h.pkl`@14e1c6f3 | `model_archive/bb/BB_1H/manifest.json` |
| BB | `BB_4H` | LONG:live, SHORT:shadow | HEAD | LONG:`bb_xgboost_model_4h.pkl`@14e1c6f3<br>SHORT:`bb_xgboost_model_4h.pkl`@14e1c6f3 | `model_archive/bb/BB_4H/manifest.json` |
| BR | `BR1D` | LONG:shadow, SHORT:shadow | HEAD | — | `model_archive/br/BR1D/manifest.json` |
| BR | `BR1H` | SHORT:shadow | HEAD | — | `model_archive/br/BR1H/manifest.json` |
| BR | `BR1HV2` | LONG:shadow, SHORT:shadow | HEAD | — | `model_archive/br/BR1HV2/manifest.json` |
| BR | `BR2H` | SHORT:shadow | HEAD | — | `model_archive/br/BR2H/manifest.json` |
| BR | `BR4H` | SHORT:shadow | HEAD | — | `model_archive/br/BR4H/manifest.json` |
| EPD | `EPD1` | LONG:shadow | HEAD | — | `model_archive/epd/EPD1/manifest.json` |
| EPD | `EPD2` | LONG:shadow, SHORT:shadow | HEAD | LONG:`epd2_model_LONG.pkl`@14e1c6f3<br>LONG:`pump_dump_model.pkl`@b6735d90<br>SHORT:`epd2_model_SHORT.pkl`@MISSING<br>SHORT:`pump_dump_model.pkl`@b6735d90 | `model_archive/epd/EPD2/manifest.json` |
| EPD | `EPD3` | LONG:live, SHORT:shadow | HEAD | LONG:`epd3_model_LONG.pkl`@a054b15c<br>SHORT:`epd3_model_SHORT.pkl`@5ed5d05e | `model_archive/epd/EPD3/manifest.json` |
| FIF | `FIF1` | LONG:shadow, SHORT:shadow | HEAD | LONG:`fif1_model.pkl`@14e1c6f3<br>SHORT:`fif1_model.pkl`@14e1c6f3 | `model_archive/fif/FIF1/manifest.json` |
| FMR | `FMR2` | LONG:shadow, SHORT:shadow | HEAD | LONG:`fmr2_model.pkl`@14e1c6f3<br>SHORT:`fmr2_model.pkl`@14e1c6f3 | `model_archive/fmr/FMR2/manifest.json` |
| LIS | `LIS1` | SHORT:shadow | HEAD | — | `model_archive/lis/LIS1/manifest.json` |
| MAX | `MAX1` | SHORT:live | HEAD | SHORT:`max1_model_SHORT.pkl`@14e1c6f3 | `model_archive/max/MAX1/manifest.json` |
| MAX | `MAX2` | LONG:live | HEAD | — | `model_archive/max/MAX2/manifest.json` |
| MIS | `MIS1-168H` | LONG:live, SHORT:shadow | HEAD | LONG:`pump_model_168h_pump_final.pkl`@b6735d90<br>SHORT:`pump_model_168h_dump_final.pkl`@b6735d90 | `model_archive/mis/MIS1-168H/manifest.json` |
| MIS | `MIS1-24H` | LONG:live, SHORT:shadow | HEAD | LONG:`pump_model_24h_pump_final.pkl`@b6735d90<br>SHORT:`pump_model_24h_dump_final.pkl`@b6735d90 | `model_archive/mis/MIS1-24H/manifest.json` |
| MIS | `MIS1-72H` | LONG:live, SHORT:shadow | HEAD | LONG:`pump_model_72h_pump_final.pkl`@b6735d90<br>SHORT:`pump_model_72h_dump_final.pkl`@b6735d90 | `model_archive/mis/MIS1-72H/manifest.json` |
| MIS | `MIS1-8H` | LONG:shadow, SHORT:live | HEAD | LONG:`pump_model_8h_pump_final.pkl`@b6735d90<br>SHORT:`pump_model_8h_dump_final.pkl`@b6735d90 | `model_archive/mis/MIS1-8H/manifest.json` |
| MIS | `MIS2-168H` | LONG:shadow, SHORT:live | HEAD | LONG:`mis2_model_168h_pump.pkl`@14e1c6f3<br>SHORT:`mis2_model_168h_dump.pkl`@14e1c6f3 | `model_archive/mis/MIS2-168H/manifest.json` |
| MIS | `MIS2-24H` | LONG:shadow, SHORT:live | HEAD | LONG:`mis2_model_24h_pump.pkl`@14e1c6f3<br>SHORT:`mis2_model_24h_dump.pkl`@14e1c6f3 | `model_archive/mis/MIS2-24H/manifest.json` |
| MIS | `MIS2-72H` | LONG:shadow, SHORT:live | HEAD | LONG:`mis2_model_72h_pump.pkl`@14e1c6f3<br>SHORT:`mis2_model_72h_dump.pkl`@14e1c6f3 | `model_archive/mis/MIS2-72H/manifest.json` |
| MIS | `MIS2-8H` | LONG:shadow, SHORT:shadow | HEAD | LONG:`mis2_model_8h_pump.pkl`@14e1c6f3<br>SHORT:`mis2_model_8h_dump.pkl`@14e1c6f3 | `model_archive/mis/MIS2-8H/manifest.json` |
| MIS | `MSI1` | LONG:retired, SHORT:retired | `f760ba03` | — | `model_archive/mis/MSI1/manifest.json` |
| PEX | `PEX1` | LONG:live, SHORT:live | HEAD | LONG:`pex1_model.pkl`@14e1c6f3<br>SHORT:`pex1_model.pkl`@14e1c6f3 | `model_archive/pex/PEX1/manifest.json` |
| QM | `QM_1H` | LONG:live, SHORT:shadow | HEAD | LONG:`qm_xgboost_model_1h.pkl`@b6735d90<br>SHORT:`qm_xgboost_model_1h.pkl`@b6735d90 | `model_archive/qm/QM_1H/manifest.json` |
| QM | `QM_4H` | LONG:live, SHORT:shadow | HEAD | LONG:`qm_xgboost_model_4h.pkl`@b6735d90<br>SHORT:`qm_xgboost_model_4h.pkl`@b6735d90 | `model_archive/qm/QM_4H/manifest.json` |
| ROM | `ROM1` | LONG:live, SHORT:live | HEAD | — | `model_archive/rom/ROM1/manifest.json` |
| RUB | `RUB1` | LONG:live, SHORT:live | HEAD | LONG:`long_reversion_model.joblib`@b6735d90<br>SHORT:`short_reversion_model.joblib`@b6735d90 | `model_archive/rub/RUB1/manifest.json` |
| RUB | `RUB2` | LONG:shadow, SHORT:shadow | HEAD | LONG:`rub2_model_LONG.pkl`@14e1c6f3<br>SHORT:`rub2_model_SHORT.pkl`@14e1c6f3 | `model_archive/rub/RUB2/manifest.json` |
| RUB | `RUB3` | LONG:shadow | HEAD | LONG:`rub2_model_LONG.pkl`@14e1c6f3 | `model_archive/rub/RUB3/manifest.json` |
| RUB | `RUB4` | LONG:shadow | HEAD | — | `model_archive/rub/RUB4/manifest.json` |
| SRA | `SRA1` | LONG:shadow, SHORT:shadow | HEAD | — | `model_archive/sra/SRA1/manifest.json` |
| SRA | `SRA2` | LONG:live, SHORT:live | HEAD | LONG:`sra2_model_LONG.json`@c6db433a<br>SHORT:`sra2_model_SHORT.json`@14e1c6f3 | `model_archive/sra/SRA2/manifest.json` |
| TD | `TD_1H` | LONG:live, SHORT:live | HEAD | LONG:`td_xgboost_model_1h.pkl`@14e1c6f3<br>SHORT:`td_xgboost_model_1h.pkl`@14e1c6f3 | `model_archive/td/TD_1H/manifest.json` |
| TD | `TD_4H` | LONG:live, SHORT:live | HEAD | LONG:`td_xgboost_model_4h.pkl`@14e1c6f3<br>SHORT:`td_xgboost_model_4h.pkl`@14e1c6f3 | `model_archive/td/TD_4H/manifest.json` |
| TRM | `TRM1` | LONG:live, SHORT:live | HEAD | — | `model_archive/trm/TRM1/manifest.json` |
| UFI | `UFI1` | LONG:live, SHORT:live | HEAD | — | `model_archive/ufi/UFI1/manifest.json` |
