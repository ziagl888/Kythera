# 23_market_tracker.py — PATCH für Regime-Fit-Zeile
#
# Exakt diese Stelle ersetzen (Zeilen ~1015–1030 in job_per_bot_performance):
#
# VORHER:
#         kelly_lines.append(f"<b>{strategy}</b>")
#
#         if status == 'insufficient_data':
#             kelly_lines.append("  --- insufficient data (need ≥10 wins & losses)")
#         elif status == 'neg_edge':
#             kelly_lines.append("  ⛔ NEGATIVE EDGE — do not trade")
#         elif status == 'ok':
#             hk = k['half_kelly_pct']
#             ms = k['margin_safe_pct']
#             mp = k['margin_pure_pct']
#             kelly_lines.append(f"  Half-Kelly:   {hk:>5.1f}% of account")
#             kelly_lines.append(f"  Safe Margin:  {ms:>5.2f}%  (Half-Kelly / Lev)")
#             kelly_lines.append(f"  Pure Margin:  {mp:>5.1f}%  (Half-Kelly / (avg_loss × Lev))")
#         else:
#             kelly_lines.append("  ---")
#         kelly_lines.append("")  # Leerzeile als Abtrennung zwischen Bots
#
# NACHHER (Regime-Fit-Zeile hinzugefügt):
#
#         kelly_lines.append(f"<b>{strategy}</b>")
#
#         if status == 'insufficient_data':
#             kelly_lines.append("  --- insufficient data (need ≥10 wins & losses)")
#         elif status == 'neg_edge':
#             kelly_lines.append("  ⛔ NEGATIVE EDGE — do not trade")
#         elif status == 'ok':
#             hk = k['half_kelly_pct']
#             ms = k['margin_safe_pct']
#             mp = k['margin_pure_pct']
#             kelly_lines.append(f"  Half-Kelly:   {hk:>5.1f}% of account")
#             kelly_lines.append(f"  Safe Margin:  {ms:>5.2f}%  (Half-Kelly / Lev)")
#             kelly_lines.append(f"  Pure Margin:  {mp:>5.1f}%  (Half-Kelly / (avg_loss × Lev))")
#         else:
#             kelly_lines.append("  ---")
#         kelly_lines.append(f"  Regime Fit:   {_get_regime_fit_label(conn_for_kelly, strategy)}")
#         kelly_lines.append("")  # Leerzeile als Abtrennung zwischen Bots
#
# AUSSERDEM: Diese Hilfsfunktion VOR job_per_bot_performance einfügen:

def _get_regime_fit_label(conn, bot_name: str) -> str:
    """
    Returns a human-readable regime fit label for a bot in the current regime.
    Graceful degradation: returns '---' if tables don't exist or orchestrator
    is not yet deployed.

    Examples:
        'CHOP 58% (n=145), Overall 59% → NEUTRAL'
        'TREND_UP 72% (n=80), Overall 61% → STARK'
        '--- (zu wenig Daten)'
        '---'  ← wenn Orchestrator nicht deployt
    """
    try:
        # Read current regime
        with conn.cursor() as cur:
            cur.execute(
                "SELECT regime FROM regime_current WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None:
            return "---"
        cur_regime = row[0]

        # Read bot WR in current regime (window=30, BOTH directions)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n_trades, win_rate FROM bot_regime_performance
                WHERE bot_name = %s AND regime = %s
                  AND alt_context = 'ALL' AND direction = 'BOTH'
                  AND window_days = 30
                """,
                (bot_name, cur_regime),
            )
            regime_row = cur.fetchone()

        # Read overall WR (ALL regimes)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n_trades, win_rate FROM bot_regime_performance
                WHERE bot_name = %s AND regime = 'ALL'
                  AND alt_context = 'ALL' AND direction = 'BOTH'
                  AND window_days = 30
                """,
                (bot_name,),
            )
            overall_row = cur.fetchone()

        if regime_row is None or overall_row is None:
            return "---"

        n_regime, wr_regime = regime_row
        _, wr_overall = overall_row
        if wr_regime is None or wr_overall is None:
            return "---"

        if n_regime < 30:
            return f"{cur_regime} n={n_regime} → --- (zu wenig Daten)"

        diff = wr_regime - wr_overall
        if diff >= 10.0:
            label = "STARK ↑"
        elif diff <= -10.0:
            label = "SCHWACH ↓"
        else:
            label = "NEUTRAL"

        return (
            f"{cur_regime} {wr_regime:.0f}% (n={n_regime}), "
            f"Overall {wr_overall:.0f}% → {label}"
        )

    except Exception:
        # Graceful degradation: Orchestrator not deployed or tables missing
        return "---"
