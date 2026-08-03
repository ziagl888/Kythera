# Research report: model ideas for Kythera from external evidence (2026-07-12)

**Brief:** Michi, 2026-07-12 — "using our data collection and discussions on
TradingView and Reddit, develop further model ideas that we can test and
possibly implement."
**Method:** Deep-research workflow (run `wf_d0266aa1-319`, 101 sub-agents):
5 search angles (academic-funding-carry, academic-cross-sectional,
practitioner-tradingview-pine, reddit-community-consensus,
microstructure-from-OHLCV) → 19 sources fetched → 91 claims extracted →
top 25 adversarially verified (3 independent refutation votes per claim;
2/3 refutes kill). Result: **20 confirmed, 5 refuted, 0 unverifiable.**
**Task:** T-2026-CU-9050-102. The implementation-ready specs per candidate
are in `docs/MODEL_CANDIDATES_SPEC_2026-07.md`.

**Constraints the research was run against:** OHLCV 5m–1w for ~530
Binance USDT perps (~430d), ~120 indicators, full funding history,
pump_dump_events since Feb 2026, ticker_10s + whale prints (>$25k, top 20) only
since ~05./07.07.2026; **no** OI archive, no liquidation feeds, no
orderbook, no on-chain. Execution: Telegram → Cornix → Binance (entry
market/limit, TP ladder, fixed SL; close command possible; no HFT, no
ATR trailing).

---

## 1. Confirmed findings (confidence, source, verify vote)

### 1.1 Funding/carry — the strongest evidence base

**F1 · The crypto carry premium is real, large, persistent — but pure
harvesting needs a spot leg.** Avg. ~7% p.a. across exchanges (Apr 2019–Jul
2024; BTC basis 8.2%/6.4% OKEx/CME, peaks 45–55%), ~10x the S&P 500 carry.
The short-futures leg is so volatile (17%/month stddev vs. 2–3% mean) that
10x isolated would have been liquidated in >50% of months. Post-ETF
(Jan 2024) compressed by ~3–5pp.
— *high*, BIS WP 1087 (Schmeling/Schrimpf/Todorov), verified verbatim
against the primary PDF, votes 3-0/3-0.

**F2 · Elevated funding is a forward risk indicator for short squeezes:**
+10% standardized carry ⇒ +22% short-side liquidations (% of OI) in the
following month; the long side is not predicted. Directly implementable as
a direction/risk filter on our 430d×530 funding history. **Convention
warning:** BIS "sell liquidations" = short side — inverted relative to the
Coinglass/Binance vendor convention. — *high*, BIS WP 1087 p. 25, vote 3-0.

**F3 · Perp-spot deviations are structural** (closed no-arbitrage form,
band below trading costs): historically 60–90%/year mean absolute deviation
(2020–2022), spot-hedged funding arb Sharpe ~1.8 (retail) to 3.5 (MM).
**Implementability gap:** what's documented is delta-neutral long-spot/
short-perp — not 1:1 replicable with our perp-only stack; directional
funding capture is only a partial proxy. Sample ends Dec 2022; post-ETF
heavily compressed. The counter-thesis (CEX funding arb 2024/25 negative)
was disputed 1-2 — **current profitability is open in both directions.**
— *high* (for the historical claim), arXiv 2212.06888v5 + SSRN 4301150,
votes 3× 3-0.

### 1.2 Cross-section over the large universe

**F4 · Crypto momentum lives only 1–4 weeks and flips into significant
reversal from ~1 month on** — much faster than equities. The 1–2-week
return sort is the most extreme signal (short-held momentum up to ~70%
p.a.; 10–12 weeks held is the strongest reversal). **But:** the specific
parametrization "2w/2w is the best spec, 1w/1w insignificant" was
**refuted** 0-3 — only the horizon structure holds up, not the parameters.
Pre-2021 **spot** data (~2.000 coins, incl. illiquid micro-caps), gross,
loser leg hard to short. — *high* (structure), Dobrynskaya SSRN 3913263
(peer-reviewed JAI 2023), votes 3-0/3-0.

**F5 · Anchored reversal:** a reversal sort signal decomposed at the
**formation low as a behavioural anchor** beats plain-past-return reversal;
authors claim cost robustness across 30–360d formations. Only the abstract
was verifiable (paywall), single study, FRL short format, spot data.
— *medium*, Nakagawa & Sakemoto FRL 2025, votes 3-0/3-0.

**F6 · The MAX/lottery effect is inverted (INVERTIERT) in crypto:** a
higher max daily return predicts a higher subsequent return ("lottery-like momentum"),
independent of conventional momentum. The "short the lottery coins" reflex
imported from equities by trading communities contradicts the crypto
literature. Size-dependent (counter-finding in top-20 caps), pre-2021,
20–64 coins. — *medium*, NAJEF 2021 + IRFA 2021 (+3.03%/week H-L MAX
decile) + Financial Innovation 2021, votes 2-1/3-0.

**F7 · Realized moments predict the cross-section:** RV and kurtosis
positive, **skewness negative** (highly positively-skewed coins
underperform). If lottery-short, then via realized skewness — not via MAX.
The mechanism story ("driven by extreme positive returns") was refuted 0-3.
Moments are computable from intraday candles. — *medium*, FRL 2021
(84 coins) + IRFA 2024 (corroboration), votes 2-1/2-1.

### 1.3 Backtests directly matching our universe

**F8 · TSMOM on 6h candles, 150+ Binance USDT perps:** ROC lookback ×
entry threshold, long/short, ATR trailing exit; claims **2.41 Sharpe net**
(40.5% p.a., −12.7% MDD, Jan 22–Dec 24, 4bps fees+slippage+funding; >2.0 at
8bps). **Two hard caveats:** (a) single-author preprint, monthly grid
re-optimization of all 3 parameters = classic overfitting vector,
survivorship untreated; (b) ATR trailing exit is not natively representable
with Cornix. It is nonetheless the best data match in the whole search
space (our universe, our timeframe) and cheap to falsify in-house.
— *medium* (claims verified verbatim, confidence in the performance
limited), arXiv 2602.11708v1, votes 3-0/3-0.

### 1.4 Time and event effects

**F9 · Settlement-bound intraday structure exists:** cross-exchange
spreads peak ~2h after the 8h settlements (00/08/16 UTC), daily max
02:00 UTC. Measured on cross-venue **dispersion**, not single-exchange
returns; only ~2 months of data; ~2.5bps peak-to-trough. Motivates entry
**timing**, not a standalone edge. — *medium*, Zhivkov et al., IJFS (MDPI)
14(5):103, Apr 2026, vote 3-0.

**F10 · New Binance listings drift systematically negative:** the n=31
sample is only ~16% positive after 6 months; corroborated by a 44-listing
2024 study (−22.7% @3M, −37.6% @6M, 5.5% positive) and a 2025 sample (89%
negative). The quantified short edge (the −18% drawdown claim) was
disputed 1-2 — direction yes, size/timing open; no beta adjustment in the
sources. — *medium*, FXStreet 2024 + Traders Union + BeInCrypto, vote 3-0
(companion claim 1-2 ✗).

### 1.5 Community mechanisms (evidence gradient)

**F11 · BB-inside-KC squeeze (TTM):** mechanically clean, OHLCV-only,
closed-candle-capable — but **zero credible performance evidence**; the
one "optimization" found is a 243-combination sweep (textbook
overfitting). Community-popular ≠ proven. — *low*,
PyQuantLab/StockCharts/TrendSpider, vote 3-0 (mechanics only).

**F12 · TradingView repaint warning (confirms our Report-16 trap):**
>95% of Pine indicators technically repaint (live value before bar close
is not final; `lookahead_on` on HTF requests creates impossible
backtests). Published TradingView "win rates" are worthless as evidence;
the mechanics can still serve as hypotheses. — Context finding from the
practitioner angle.

**F13 · Negative/scoping finding:** the funding mechanism-design
literature (Kim & Park, arXiv 2506.08573) is purely theoretical — no
backtests, no strategy proof. Only useful as an explanation of why
(WARUM) funding exists. — *high*, full text checked, vote 3-0.

## 2. Refuted claims (killed in verification — do not build on these)

| Claim | Vote | Source |
|---|---|---|
| CEX funding arb 2024/25 had NEGATIVE Sharpe (−7.3/−7.9) | 1-2 ✗ | ScienceDirect S2096720925000818 |
| Funding-arb absolute returns tiny (2.2% Binance vs. 113% HODL) | 1-2 ✗ | ditto |
| "2w/2w is the best momentum spec; 1w/1w insignificant" | 0-3 ✗ | SSRN 3913263 |
| "Higher-moment prediction comes from extreme positive returns (equity-MAX analogy)" | 0-3 ✗ | FRL S1544612320303135 |
| "Listing-day long = −18% avg. drawdown ⇒ fade has positive expected value" | 1-2 ✗ | FXStreet |

1-2 votes mean: disputed, not certainly wrong — but unusable as a
foundation.

## 3. Open questions (unresearched externally, not refuted)

1. Do the pre-2021 spot effects (momentum→reversal structure, MAX sign,
   skewness sign) replicate on 2024–26 Binance perps after realistic
   Cornix fees? → exactly what our studies in
   `docs/MODEL_CANDIDATES_SPEC_2026-07.md` test.
2. Is directional perp-only funding capture still profitable after the
   post-ETF compression? (Evidence disputed in both directions.)
3. Does the TSMOM edge survive substituting ATR trailing → fixed SL/TP
   ladder plus survivorship correction?
4. Search angles **without** surviving claims: whale-print/volume
   signals, BTC-dominance/breadth gating, OHLCV wick liquidation
   cascades, OI strategies. Unresearched ≠ refuted — areas that map
   directly onto data we already collect (pump_dump_events, whale_data)
   or should start collecting now (OI).

## 4. Sources (19 fetched, quality label from the workflow)

Primary: BIS WP 1087 · arXiv 2212.06888v5 · SSRN 3913263 · arXiv 2602.11708v1 ·
FRL S154461232501058X (Anchored Reversal) · NAJEF S1062940821001625 (MAX) ·
FRL S1544612320303135 (Moments) · MDPI IJFS 14(5):103 (Settlement) ·
arXiv 2506.08573 (theory) · ScienceDirect S2096720925000818 (funding-arb
critique) · SSRN 4301150. Secondary/blog/forum: FXStreet (listings) ·
PyQuantLab · FMZQuant-TTM · crosstrade.io (repaint) · techacademies-Medium ·
awesome-pinescript (GitHub) · TrendSpider · TradingView "Liquidation
Cascade Detector [QuantAlgo]".

Note: the additional corroboration sources named in §1 (IRFA 2021/2024,
Financial Innovation 2021, Traders Union, BeInCrypto) come from the
verify runs or from citations within (INNERHALB) the 19 fetched sources —
they do not count towards the fetch list.

## 5. Positioning against the internal body of work

Overlaps: F2 externally validates the ABR2 funding gate/SHORT veto
(Report 21 Addendum 2, cross-validated on 33,5k events). F3 motivates
FMR2 (design exists, NEW_IDEAS_BOTS.md) as the cleanest test of the
perp-only capture thesis. F8/F4 are falsifiable with our replay
infrastructure (walkforward_sim, simulate_exit, retrain_from_replay) at
~1 study-day each. What has already been falsified internally is not
NOT rehabilitated by the research: PEX1 (1h features uninformative), EPD2
(no alt-pump window), RUB2-LONG as an event gate (regime problem), FMR1
(wrong label geometry — F3/FMR2 is the correct retest).

## 6. Addendum 2026-07-12: leaderboard research + operator videos (T-2026-CU-9050-105)

**Second deep-research round** (run `wf_907acab0-13f`, 103 agents, same
methodology) on the operator's question "analyze top traders on
Hyperliquid/BitMEX & co. and reverse-engineer their strategies":

**F14 · Only Hyperliquid is durably publicly inspectable** — *high*, 3-0:
the unauthenticated `/info` API returns open positions for any wallet
address (entryPx, signed size, leverage, liq price, ROE, cumFunding),
fills (WS push, sub-second), funding payments, cash flows; ~600 position
polls/min/IP (weight 2 of 1200/min); a copy ecosystem exists (Hyperdash,
HypurrScan, CoinGlass, open-source bots). Caveats: agent wallets empty,
sub-accounts not enumerable, entryPx average only, WS cap ~10
user-subscriptions/IP (verifier evidence only — check before
architecture).
**F15 · Binance leaderboard: only collapsing gray-market scrapers** —
*high*, 3-0 (endpoint has required auth, opt-in only, since ~early 2024;
RapidAPI vendor in migration/EOL). **F16 · Bybit V5 copy API has ZERO
master read endpoints** — *high*, 3-0; OKX/Bitget export and BitMEX
usability: no surviving claims. **F17 · Skill persistence exists, but
only in the tiny top tail** — *high* (Barber/Lee/Liu/Odean, Taiwan): >80%
of day traders lose (2-1), <1% are predictably net-profitable; the top
partition stayed ~66% profitable and mimicking would have been
OOS-profitable (3-0) — never replicated for crypto perps.
**F18 · Style reverse-engineering is fragile** — *high*: labels from
aggregate stats hold up only 36–40% over 4 weeks (CFTC); the 96.5%
identification accuracy of the IRL study was refuted (WIDERLEGT) 0-3; no
published imitation system with proven OOS profitability.
**F19 · Copy trading causally increases followers' risk appetite** —
*high* (Management Science 2020, experiment). Whale-copy hype (James
Wynn etc.): no verified evidence. → Consequence: candidate **K13**
(collector + modest feature/lag study) in the spec, no copy bot.

**Operator videos** (YouTube 5NR4urEIw9Q + d5KlwDnJAAc, transcripts via
yt-dlp auto-captions, rule extracts in KB `ingest-c1e5112dea7f` /
`ingest-9f6511a5f951`): the "most hated line" is a cross-and-retest entry
annotation (= our ABR concept); new and testable are the **scratch-reload
exit scheme** (→ candidate **K15**) and the **TOTAL3 alt gate** (→ K6
mandatory feature). The title "Ichimoku" is misleading — the transcript
contains no Ichimoku rules.
