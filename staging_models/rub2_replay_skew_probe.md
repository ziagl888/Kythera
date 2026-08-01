# RUB2 Replay-vs-Live parity probe (T-2026-KYT-9050-008)

Window 2026-07-01 00:00:00+00:00 → 2026-08-01 00:00:00+00:00, 229 matched (symbol, candle) pairs, 128 symbols.

Replay: `C:\Users\Michael\Documents\_X\staging_models\replay\rub_replay_365d.jsonl`  
Artifacts: `rub2_model_SHORT.pkl` (thr 0.7929), `max1_model_SHORT.pkl` (thr 0.829)

## Agreement per day

| day | n | pearson | mean_abs_diff | pct_identical | best_artifact |
| --- | --- | --- | --- | --- | --- |
| 2026-07-06 | 26 | -0.4523 | 0.16063 | 0.0 | rub2_model_SHORT.pkl |
| 2026-07-07 | 19 | 0.6015 | 0.02964 | 0.0 | max1_model_SHORT.pkl |
| 2026-07-08 | 21 | 0.98 | 0.01538 | 0.0 | max1_model_SHORT.pkl |
| 2026-07-09 | 38 | 0.9801 | 0.01114 | 0.0 | max1_model_SHORT.pkl |
| 2026-07-10 | 59 | 0.9738 | 0.00949 | 3.4 | max1_model_SHORT.pkl |
| 2026-07-11 | 38 | 0.9695 | 0.014 | 0.0 | max1_model_SHORT.pkl |
| 2026-07-12 | 25 | 1.0 | 0.00025 | 92.0 | max1_model_SHORT.pkl |
| 2026-07-13 | 3 | 1.0 | 0.0 | 100.0 | max1_model_SHORT.pkl |


## Pooled agreement

| artifact | n | pearson | spearman | mean_abs_diff | pct_identical |
| --- | --- | --- | --- | --- | --- |
| rub2_model_SHORT.pkl | 229 | 0.4663 | 0.5066 | 0.06562 | 0.0 |
| max1_model_SHORT.pkl | 229 | 0.5841 | 0.7329 | 0.02875 | 12.2 |


## Threshold curve — replay test slice

1844 events, 2026-05-11 20:00:00+00:00 → 2026-07-13 03:00:00+00:00 (62.3 d), scored with `max1_model_SHORT.pkl`. p99 prob 0.8412, max 0.8744.

| threshold | n | per_day | wr_pct | avg_pnl_pct | sum_pnl_pct |
| --- | --- | --- | --- | --- | --- |
| 0.829 | 44 | 0.71 | 93.2 | 2.76 | 121.3 |
| 0.85 | 11 | 0.18 | 100.0 | 3.53 | 38.9 |
| 0.88 | 0 | 0.0 |  |  |  |
| 0.9 | 0 | 0.0 |  |  |  |
| 0.91 | 0 | 0.0 |  |  |  |
| 0.93 | 0 | 0.0 |  |  |  |
| 0.94 | 0 | 0.0 |  |  |  |

