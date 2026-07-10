-- R3 / T-2026-CU-9050-032 — Zeitspalten auf timestamptz.
--
-- STATUS: VORBEREITET, NICHT AUSGEFÜHRT. Es gibt keinen Migration-Runner.
-- Ein `ALTER TABLE` auf einer Live-Tabelle ist eine Operator-Entscheidung
-- (Eskalation, docs/OPUS-HANDOFF.md §6). Vor der Ausführung lesen:
-- docs/UTC_POLICY.md §4 — insbesondere die Kopplung an die Bootstrap-DDLs
-- in den Bots und die Cutover-Frage für die Altzeilen.
--
-- Vorbedingung — HEUTE NICHT ERFÜLLT: die Fleet muss bereits mit
-- `-c timezone=UTC` laufen (core/database.py, siehe docs/UTC_POLICY.md §4) UND
-- alle Writer müssen UTC schreiben. Sonst altert man Lokalzeit zu falschem UTC.
--
-- Das USING interpretiert jeden bestehenden naiven Wert als UTC. Zeilen aus der
-- Zeit VOR dem R3-Restart tragen Serverlokalzeit (Europe/Bucharest, +2/+3h) und
-- verschieben sich dabei um genau diesen Offset. Für die kurzen Trade-Fenster
-- irrelevant, für die Trainings-Historie nicht — siehe UTC_POLICY.md §5/§6
-- (Backfill vs. Cutover-Konstante, Operator-Entscheidung).

BEGIN;

ALTER TABLE active_trades_master
    ALTER COLUMN "time" TYPE timestamptz USING "time" AT TIME ZONE 'UTC',
    ALTER COLUMN posted TYPE timestamptz USING posted AT TIME ZONE 'UTC';

ALTER TABLE closed_trades_master
    ALTER COLUMN "time" TYPE timestamptz USING "time" AT TIME ZONE 'UTC',
    ALTER COLUMN posted TYPE timestamptz USING posted AT TIME ZONE 'UTC';

-- trade_cooldowns.last_posted_at steht hier BEWUSST NICHT drin.
--
-- Live ist die Spalte bereits timestamptz (P2.2). Ein `ALTER … TYPE timestamptz
-- USING last_posted_at AT TIME ZONE 'UTC'` wäre darauf KEIN No-op: auf einer
-- timestamptz-Spalte liefert `AT TIME ZONE 'UTC'` einen naiven Wert, den der
-- TYPE-Cast mit der Session-TZ zurückwandelt. Nur unter einer UTC-Session ist
-- das die Identität — unter jeder anderen verschiebt es eine Live-Geldspalte
-- still um den Offset. Jeder andere ALTER hier ist session-unabhängig.
--
-- Umgebungen, die aus der naiven Bootstrap-DDL von 26_regime_detector.py
-- entstanden sind, brauchen den ALTER trotzdem. Vorher den Ist-Typ prüfen:
--   SELECT data_type FROM information_schema.columns
--    WHERE table_name = 'trade_cooldowns' AND column_name = 'last_posted_at';

ALTER TABLE regime_history
    ALTER COLUMN ts TYPE timestamptz USING ts AT TIME ZONE 'UTC';

ALTER TABLE regime_current
    ALTER COLUMN since TYPE timestamptz USING since AT TIME ZONE 'UTC',
    ALTER COLUMN alt_context_since TYPE timestamptz USING alt_context_since AT TIME ZONE 'UTC',
    ALTER COLUMN last_raw_ts TYPE timestamptz USING last_raw_ts AT TIME ZONE 'UTC';

-- Spalten MIT Default: erst DROP DEFAULT, dann TYPE, dann SET DEFAULT. Ein
-- kombiniertes `TYPE … , SET DEFAULT …` in einer Anweisung lässt Postgres den
-- alten Default-Ausdruck mitcasten — unnötig fragil.
ALTER TABLE bot_regime_performance ALTER COLUMN last_computed DROP DEFAULT;
ALTER TABLE bot_regime_performance
    ALTER COLUMN last_computed TYPE timestamptz USING last_computed AT TIME ZONE 'UTC';
ALTER TABLE bot_regime_performance ALTER COLUMN last_computed SET DEFAULT NOW();

ALTER TABLE bot_regime_whitelist
    ALTER COLUMN computed_at TYPE timestamptz USING computed_at AT TIME ZONE 'UTC';

ALTER TABLE orchestrator_open_trades
    ALTER COLUMN opened_at TYPE timestamptz USING opened_at AT TIME ZONE 'UTC',
    ALTER COLUMN closed_at TYPE timestamptz USING closed_at AT TIME ZONE 'UTC';

ALTER TABLE orchestrator_suppressed_signals ALTER COLUMN ts DROP DEFAULT;
ALTER TABLE orchestrator_suppressed_signals
    ALTER COLUMN ts TYPE timestamptz USING ts AT TIME ZONE 'UTC';
ALTER TABLE orchestrator_suppressed_signals ALTER COLUMN ts SET DEFAULT NOW();

ALTER TABLE pump_dump_events
    ALTER COLUMN spike_time TYPE timestamptz USING spike_time AT TIME ZONE 'UTC';

-- ml_predictions_master.time ist naiv und hat keine DDL im Repo (R2/B3-Lücke) —
-- Spaltenname vor der Ausführung gegen `information_schema.columns` prüfen.
ALTER TABLE ml_predictions_master
    ALTER COLUMN "time" TYPE timestamptz USING "time" AT TIME ZONE 'UTC';

-- closed_ai_signals.close_time ist bereits timestamptz (8_ai_trade_monitor.py:27)
-- und steht deshalb NICHT in dieser Migration.
--
-- ai_signals hat keine DDL im Repo; die einzige `CREATE TABLE ai_signals` liegt
-- in legacy_trainers/zzz.py und trägt eine Spalte `timestamp`, nicht `time`. Typ
-- der Live-Spalte ist repo-seitig nicht belegbar — erst verifizieren, dann
-- entkommentieren.
-- ALTER TABLE ai_signals
--     ALTER COLUMN "time" TYPE timestamptz USING "time" AT TIME ZONE 'UTC';

COMMIT;
