-- Kythera — canonical live-DB schema reference (R2(b), T-2026-CU-9050-098).
--
-- Source: pg_dump 17.6 --schema-only --no-owner --no-privileges against the live
-- database `cryptodata` on 2026-07-12. This file is a REFERENCE, not a migration:
-- the executing DDL remains the CREATE TABLE IF NOT EXISTS sites in the bots, and
-- any live ALTER stays an operator decision (OPUS-HANDOFF §6). It exists because
-- CREATE TABLE statements are scattered over ~10 files with drift, and two of the
-- busiest tables (ai_signals: 13 writers, ml_predictions_master: 9 writers) had
-- no DDL anywhere in the repo until this dump (AUDIT_TODO R2).
--
-- Scope: all 44 application (singleton) tables, followed by ONE representative
-- pair of the per-coin family. NOT dumped individually (9,789 tables at dump
-- time, names generated from coins.json — see core/coins.py):
--   {SYM}_{tf}                  OHLCV candles      (~530 symbols x ~9 timeframes)
--   {SYM}_{tf}_indicators       ~120 indicators    (written by 2_indicator_engine)
--   {SYM}_{YYMMDD}_{tf}[_ind.]  quarterly futures  (BTCUSDT_260925_*, ETHUSDT_*)
--   {PAIR}=X_{tf}               yfinance forex     (EURUSD=X_1h, ... via bot 16)
--   {SYM}_{tf}_GOLD             metals variants    (XAUUSDT/PAXGUSDT via bot 16)
--   CJK-named leftovers         leaked junk symbols (币安人生USDT_*, 我踏马来了USDT_*,
--                               龙虾USDT_* — the P2.16 double-writer leak class;
--                               deletion is a D5 operator gate, kept out of scope)
--
-- Regenerate (fleet host, read-only):
--   pg_dump -h localhost -U dbfiller -d cryptodata --schema-only --no-owner \
--     --no-privileges -t public.<table> ... ; strip the \restrict token lines.
--

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: ticker_10s; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ticker_10s (
    ts timestamp with time zone NOT NULL,
    symbol character varying(20) NOT NULL,
    price double precision NOT NULL,
    vol_10s double precision NOT NULL,
    vol_valid boolean NOT NULL
);


--
-- Name: active_smc_zones; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.active_smc_zones (
    id bigint NOT NULL,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    zone_type text NOT NULL,
    top_edge numeric NOT NULL,
    bottom_edge numeric NOT NULL,
    created_time timestamp with time zone NOT NULL,
    mitigated boolean DEFAULT false,
    mitigated_time timestamp with time zone,
    inserted_at timestamp with time zone DEFAULT now()
);


--
-- Name: active_smc_zones_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.active_smc_zones_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: active_smc_zones_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.active_smc_zones_id_seq OWNED BY public.active_smc_zones.id;


--
-- Name: active_trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.active_trades (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text
);


--
-- Name: active_trades2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.active_trades2 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text
);


--
-- Name: active_trades2_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.active_trades2_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: active_trades2_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.active_trades2_lfd_seq OWNED BY public.active_trades2.lfd;


--
-- Name: active_trades3; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.active_trades3 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text
);


--
-- Name: active_trades3_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.active_trades3_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: active_trades3_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.active_trades3_lfd_seq OWNED BY public.active_trades3.lfd;


--
-- Name: active_trades4; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.active_trades4 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text
);


--
-- Name: active_trades4_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.active_trades4_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: active_trades4_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.active_trades4_lfd_seq OWNED BY public.active_trades4.lfd;


--
-- Name: active_trades5; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.active_trades5 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text
);


--
-- Name: active_trades5_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.active_trades5_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: active_trades5_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.active_trades5_lfd_seq OWNED BY public.active_trades5.lfd;


--
-- Name: active_trades_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.active_trades_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: active_trades_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.active_trades_lfd_seq OWNED BY public.active_trades.lfd;


--
-- Name: active_trades_master; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.active_trades_master (
    id integer NOT NULL,
    strategy text,
    "time" timestamp without time zone,
    coin text,
    direction text,
    lev text,
    entry real,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text
);


--
-- Name: active_trades_master_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.active_trades_master_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: active_trades_master_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.active_trades_master_id_seq OWNED BY public.active_trades_master.id;


--
-- Name: ai_br_trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_br_trades (
    id integer NOT NULL,
    model text NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL,
    confidence double precision NOT NULL,
    threshold double precision NOT NULL,
    retest_time timestamp with time zone NOT NULL,
    creation_time timestamp with time zone DEFAULT now()
);


--
-- Name: ai_br_trades_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_br_trades_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_br_trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_br_trades_id_seq OWNED BY public.ai_br_trades.id;


--
-- Name: ai_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_signals (
    id bigint NOT NULL,
    symbol text NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    price numeric NOT NULL,
    model text NOT NULL,
    direction text NOT NULL,
    confidence numeric NOT NULL,
    inserted_at timestamp with time zone DEFAULT now(),
    open_time timestamp without time zone DEFAULT now(),
    current_target_hit integer DEFAULT 0,
    targets json,
    sl real,
    entry2 real,
    entry1 real,
    entry_filled boolean DEFAULT true,
    expiry_hours integer
);


--
-- Name: ai_signals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_signals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_signals_id_seq OWNED BY public.ai_signals.id;


--
-- Name: bot_regime_performance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_regime_performance (
    id integer NOT NULL,
    bot_name text NOT NULL,
    regime text NOT NULL,
    alt_context text NOT NULL,
    direction text NOT NULL,
    window_days integer NOT NULL,
    n_trades integer NOT NULL,
    win_rate real,
    avg_pnl_pct real,
    median_pnl_pct real,
    pnl_stddev real,
    sharpe_like real,
    worst_trade_pct real,
    best_trade_pct real,
    last_computed timestamp without time zone DEFAULT (now() AT TIME ZONE 'UTC'::text)
)
WITH (fillfactor='50');


--
-- Name: bot_regime_performance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_regime_performance_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bot_regime_performance_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_regime_performance_id_seq OWNED BY public.bot_regime_performance.id;


--
-- Name: bot_regime_whitelist; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_regime_whitelist (
    bot_name text NOT NULL,
    regime text NOT NULL,
    alt_context text NOT NULL,
    direction text NOT NULL,
    whitelisted boolean NOT NULL,
    reason text,
    computed_at timestamp without time zone NOT NULL,
    whitelisted_v2 boolean,
    reason_v2 text
);


--
-- Name: bot_trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_trades (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real
);


--
-- Name: bot_trades2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_trades2 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real
);


--
-- Name: bot_trades2_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_trades2_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bot_trades2_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_trades2_lfd_seq OWNED BY public.bot_trades2.lfd;


--
-- Name: bot_trades3; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_trades3 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real
);


--
-- Name: bot_trades3_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_trades3_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bot_trades3_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_trades3_lfd_seq OWNED BY public.bot_trades3.lfd;


--
-- Name: bot_trades4; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_trades4 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry real,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real
);


--
-- Name: bot_trades4_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_trades4_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bot_trades4_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_trades4_lfd_seq OWNED BY public.bot_trades4.lfd;


--
-- Name: bot_trades5; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_trades5 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry real,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real
);


--
-- Name: bot_trades5_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_trades5_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bot_trades5_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_trades5_lfd_seq OWNED BY public.bot_trades5.lfd;


--
-- Name: bot_trades_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_trades_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bot_trades_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_trades_lfd_seq OWNED BY public.bot_trades.lfd;


--
-- Name: closed_ai_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.closed_ai_signals (
    id integer NOT NULL,
    symbol character varying(20),
    model text,
    direction character varying(10),
    entry real,
    close_price real,
    targets_hit integer,
    open_time timestamp without time zone,
    close_time timestamp without time zone DEFAULT now(),
    status text
);


--
-- Name: closed_ai_signals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.closed_ai_signals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: closed_ai_signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.closed_ai_signals_id_seq OWNED BY public.closed_ai_signals.id;


--
-- Name: closed_trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.closed_trades (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text,
    strategy text,
    close_price real
);


--
-- Name: closed_trades2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.closed_trades2 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text,
    strategy text,
    close_price real
);


--
-- Name: closed_trades2_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.closed_trades2_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: closed_trades2_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.closed_trades2_lfd_seq OWNED BY public.closed_trades2.lfd;


--
-- Name: closed_trades3; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.closed_trades3 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text,
    strategy text,
    close_price real
);


--
-- Name: closed_trades3_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.closed_trades3_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: closed_trades3_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.closed_trades3_lfd_seq OWNED BY public.closed_trades3.lfd;


--
-- Name: closed_trades4; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.closed_trades4 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text,
    strategy text,
    close_price real
);


--
-- Name: closed_trades4_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.closed_trades4_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: closed_trades4_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.closed_trades4_lfd_seq OWNED BY public.closed_trades4.lfd;


--
-- Name: closed_trades5; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.closed_trades5 (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text,
    strategy text,
    close_price real
);


--
-- Name: closed_trades5_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.closed_trades5_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: closed_trades5_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.closed_trades5_lfd_seq OWNED BY public.closed_trades5.lfd;


--
-- Name: closed_trades_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.closed_trades_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: closed_trades_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.closed_trades_lfd_seq OWNED BY public.closed_trades.lfd;


--
-- Name: closed_trades_master; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.closed_trades_master (
    id integer NOT NULL,
    strategy text,
    "time" timestamp without time zone,
    coin text,
    direction text,
    lev text,
    entry real,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    close_price real,
    posted timestamp without time zone,
    status text
);


--
-- Name: closed_trades_master_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.closed_trades_master_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: closed_trades_master_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.closed_trades_master_id_seq OWNED BY public.closed_trades_master.id;


--
-- Name: conv_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conv_signals (
    id bigint NOT NULL,
    source_bot text NOT NULL,
    source_time timestamp with time zone NOT NULL,
    coin text NOT NULL,
    direction text NOT NULL,
    entry_price numeric,
    lev text,
    inserted_at timestamp with time zone DEFAULT now()
);


--
-- Name: conv_signals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.conv_signals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: conv_signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.conv_signals_id_seq OWNED BY public.conv_signals.id;


--
-- Name: execution_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.execution_queue (
    id integer NOT NULL,
    script_id integer,
    script_path text NOT NULL,
    scheduled_time timestamp with time zone DEFAULT now(),
    status text DEFAULT 'pending'::text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    exit_code integer,
    priority integer DEFAULT 0
);


--
-- Name: execution_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.execution_queue_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: execution_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.execution_queue_id_seq OWNED BY public.execution_queue.id;


--
-- Name: funding_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.funding_rates (
    symbol text NOT NULL,
    funding_time timestamp with time zone NOT NULL,
    funding_rate double precision NOT NULL
);


--
-- Name: master_ai_processed_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.master_ai_processed_signals (
    signal_type text NOT NULL,
    signal_id bigint NOT NULL,
    processed_at timestamp with time zone DEFAULT now(),
    ml_confidence numeric(5,4)
);


--
-- Name: ml_predictions_master; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ml_predictions_master (
    id integer NOT NULL,
    trade_id integer,
    model_name character varying(50),
    "time" timestamp without time zone,
    coin character varying(20),
    direction character varying(10),
    entry real,
    confidence real,
    posted boolean,
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: ml_predictions_master_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ml_predictions_master_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ml_predictions_master_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ml_predictions_master_id_seq OWNED BY public.ml_predictions_master.id;


--
-- Name: ml_trend_trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ml_trend_trades (
    id bigint NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL,
    ml_probability double precision NOT NULL,
    close_price double precision NOT NULL,
    event_type text,
    trend_direction text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    entry_time timestamp with time zone,
    status text DEFAULT 'NEW'::text,
    exit_price double precision,
    exit_time timestamp with time zone,
    pnl_percent double precision,
    pnl_usd double precision
);


--
-- Name: ml_trend_trades_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ml_trend_trades_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ml_trend_trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ml_trend_trades_id_seq OWNED BY public.ml_trend_trades.id;


--
-- Name: ml_weighted_trades3; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ml_weighted_trades3 (
    id integer NOT NULL,
    lfd integer,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry real,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real,
    posted timestamp without time zone,
    status text,
    model text,
    confidence real,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: ml_weighted_trades3_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ml_weighted_trades3_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ml_weighted_trades3_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ml_weighted_trades3_id_seq OWNED BY public.ml_weighted_trades3.id;


--
-- Name: monitored_scripts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.monitored_scripts (
    id integer NOT NULL,
    name text NOT NULL,
    path text NOT NULL,
    enabled boolean DEFAULT true,
    restart_policy text DEFAULT 'always'::text,
    interval_minutes integer DEFAULT 15,
    align_to_clock boolean DEFAULT false,
    category text DEFAULT 'sequential'::text,
    sequence_order integer DEFAULT 0,
    last_enqueued_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: monitored_scripts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.monitored_scripts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: monitored_scripts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.monitored_scripts_id_seq OWNED BY public.monitored_scripts.id;


--
-- Name: orchestrator_open_trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orchestrator_open_trades (
    id integer NOT NULL,
    coin text NOT NULL,
    direction text NOT NULL,
    bot_name text NOT NULL,
    entry_price real,
    opened_at timestamp without time zone NOT NULL,
    regime_at_open text,
    alt_context_at_open text,
    original_outbox_id bigint,
    status text DEFAULT 'OPEN'::text,
    closed_at timestamp without time zone,
    close_reason text,
    wl_reason text,
    regime_close_action text,
    regime_action_at timestamp without time zone
);


--
-- Name: orchestrator_open_trades_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.orchestrator_open_trades_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: orchestrator_open_trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.orchestrator_open_trades_id_seq OWNED BY public.orchestrator_open_trades.id;


--
-- Name: orchestrator_suppressed_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orchestrator_suppressed_signals (
    id integer NOT NULL,
    ts timestamp without time zone DEFAULT (now() AT TIME ZONE 'UTC'::text),
    bot_name text,
    coin text,
    direction text,
    regime_at_signal text,
    reason text,
    original_outbox_id bigint
);


--
-- Name: orchestrator_suppressed_signals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.orchestrator_suppressed_signals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: orchestrator_suppressed_signals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.orchestrator_suppressed_signals_id_seq OWNED BY public.orchestrator_suppressed_signals.id;


--
-- Name: pump_dump_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pump_dump_events (
    id bigint NOT NULL,
    symbol text NOT NULL,
    spike_time timestamp with time zone NOT NULL,
    volume_ratio numeric,
    price_change_60s numeric,
    buy_pressure numeric,
    volatility numeric,
    rsi_14 numeric,
    tsi numeric,
    macd_dif numeric,
    ema9_distance_pct numeric,
    ema21_distance_pct numeric
);


--
-- Name: pump_dump_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pump_dump_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pump_dump_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pump_dump_events_id_seq OWNED BY public.pump_dump_events.id;


--
-- Name: regime_current; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.regime_current (
    id integer DEFAULT 1 NOT NULL,
    regime text NOT NULL,
    alt_context text NOT NULL,
    since timestamp without time zone NOT NULL,
    alt_context_since timestamp without time zone NOT NULL,
    confidence real,
    last_raw_regime text,
    last_raw_alt_context text,
    last_raw_ts timestamp without time zone,
    pending_regime text,
    pending_count integer DEFAULT 0,
    pending_alt_context text,
    pending_alt_count integer DEFAULT 0,
    CONSTRAINT singleton_check CHECK ((id = 1))
);


--
-- Name: regime_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.regime_history (
    id integer NOT NULL,
    ts timestamp without time zone NOT NULL,
    regime text NOT NULL,
    alt_context text NOT NULL,
    btc_price real,
    btc_return_1h real,
    btc_return_4h real,
    btc_atr_1h_pct real,
    btc_atr_4h_pct real,
    btcdom_value real,
    btcdom_return_24h real,
    confidence real,
    confidence_btc real,
    confidence_alt real,
    raw_features json
);


--
-- Name: regime_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.regime_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: regime_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.regime_history_id_seq OWNED BY public.regime_history.id;


--
-- Name: script_heartbeats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.script_heartbeats (
    script_id integer NOT NULL,
    last_heartbeat timestamp with time zone DEFAULT now(),
    pid integer,
    status text DEFAULT 'unknown'::text,
    last_error text
);


--
-- Name: telegram_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.telegram_outbox (
    id integer NOT NULL,
    channel_id bigint,
    message text,
    sent boolean DEFAULT false,
    image_path text,
    attempts integer DEFAULT 0,
    failed boolean DEFAULT false,
    last_error text,
    created_at timestamp with time zone DEFAULT now(),
    status text DEFAULT 'pending'::text,
    sending_at timestamp with time zone
)
WITH (fillfactor='70');


--
-- Name: telegram_outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.telegram_outbox_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: telegram_outbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.telegram_outbox_id_seq OWNED BY public.telegram_outbox.id;


--
-- Name: trade_candidates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trade_candidates (
    lfd integer NOT NULL,
    "time" timestamp without time zone,
    coin text,
    direction text,
    entry text,
    lev text,
    target1 real,
    target2 real,
    target3 real,
    target4 real,
    sl real
);


--
-- Name: trade_candidates_lfd_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trade_candidates_lfd_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trade_candidates_lfd_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trade_candidates_lfd_seq OWNED BY public.trade_candidates.lfd;


--
-- Name: trade_cooldowns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trade_cooldowns (
    id integer NOT NULL,
    module character varying(10) NOT NULL,
    coin character varying(20) NOT NULL,
    direction character varying(10) NOT NULL,
    last_posted_at timestamp with time zone DEFAULT now() NOT NULL,
    cooldown_hours integer DEFAULT 4
);


--
-- Name: trade_cooldowns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trade_cooldowns_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trade_cooldowns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trade_cooldowns_id_seq OWNED BY public.trade_cooldowns.id;


--
-- Name: trade_scanner_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trade_scanner_state (
    module character varying(10) NOT NULL,
    signal_type character varying(20) NOT NULL,
    last_processed_id bigint DEFAULT 0,
    last_scan_at timestamp with time zone DEFAULT now()
);


--
-- Name: trend_data; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trend_data (
    symbol text NOT NULL,
    trendline_data jsonb,
    open_time timestamp with time zone NOT NULL,
    trend_value real,
    trend_direction text,
    trend_touched text,
    touch_posted_datetime timestamp with time zone,
    trend_broken text,
    broken_posted_datetime timestamp with time zone,
    trend_break_and_retest text,
    trend_brare_posted_datetime timestamp with time zone,
    touch_candle_time timestamp with time zone,
    broken_candle_time timestamp with time zone,
    retest_candle_time timestamp with time zone
);


--
-- Name: trendmeet_rawdata; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trendmeet_rawdata (
    id integer NOT NULL,
    detection_time timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    coin text NOT NULL,
    event_type text NOT NULL,
    trend_direction text,
    close_price numeric,
    trend_value numeric,
    rel_distance_pct numeric,
    abs_distance numeric,
    rsi_9 numeric,
    rsi_14 numeric,
    rsi_24 numeric,
    volume_current numeric,
    volume_avg_20 numeric,
    volume_ratio_pct numeric,
    slope numeric,
    intercept numeric,
    tolerance numeric,
    prev_relation text,
    current_relation text,
    significant_dist_before boolean DEFAULT false,
    raw_json_data jsonb,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: trendmeet_rawdata_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trendmeet_rawdata_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trendmeet_rawdata_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trendmeet_rawdata_id_seq OWNED BY public.trendmeet_rawdata.id;


--
-- Name: active_smc_zones id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_smc_zones ALTER COLUMN id SET DEFAULT nextval('public.active_smc_zones_id_seq'::regclass);


--
-- Name: active_trades lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades ALTER COLUMN lfd SET DEFAULT nextval('public.active_trades_lfd_seq'::regclass);


--
-- Name: active_trades2 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades2 ALTER COLUMN lfd SET DEFAULT nextval('public.active_trades2_lfd_seq'::regclass);


--
-- Name: active_trades3 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades3 ALTER COLUMN lfd SET DEFAULT nextval('public.active_trades3_lfd_seq'::regclass);


--
-- Name: active_trades4 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades4 ALTER COLUMN lfd SET DEFAULT nextval('public.active_trades4_lfd_seq'::regclass);


--
-- Name: active_trades5 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades5 ALTER COLUMN lfd SET DEFAULT nextval('public.active_trades5_lfd_seq'::regclass);


--
-- Name: active_trades_master id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades_master ALTER COLUMN id SET DEFAULT nextval('public.active_trades_master_id_seq'::regclass);


--
-- Name: ai_br_trades id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_br_trades ALTER COLUMN id SET DEFAULT nextval('public.ai_br_trades_id_seq'::regclass);


--
-- Name: ai_signals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_signals ALTER COLUMN id SET DEFAULT nextval('public.ai_signals_id_seq'::regclass);


--
-- Name: bot_regime_performance id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_regime_performance ALTER COLUMN id SET DEFAULT nextval('public.bot_regime_performance_id_seq'::regclass);


--
-- Name: bot_trades lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades ALTER COLUMN lfd SET DEFAULT nextval('public.bot_trades_lfd_seq'::regclass);


--
-- Name: bot_trades2 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades2 ALTER COLUMN lfd SET DEFAULT nextval('public.bot_trades2_lfd_seq'::regclass);


--
-- Name: bot_trades3 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades3 ALTER COLUMN lfd SET DEFAULT nextval('public.bot_trades3_lfd_seq'::regclass);


--
-- Name: bot_trades4 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades4 ALTER COLUMN lfd SET DEFAULT nextval('public.bot_trades4_lfd_seq'::regclass);


--
-- Name: bot_trades5 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades5 ALTER COLUMN lfd SET DEFAULT nextval('public.bot_trades5_lfd_seq'::regclass);


--
-- Name: closed_ai_signals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_ai_signals ALTER COLUMN id SET DEFAULT nextval('public.closed_ai_signals_id_seq'::regclass);


--
-- Name: closed_trades lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades ALTER COLUMN lfd SET DEFAULT nextval('public.closed_trades_lfd_seq'::regclass);


--
-- Name: closed_trades2 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades2 ALTER COLUMN lfd SET DEFAULT nextval('public.closed_trades2_lfd_seq'::regclass);


--
-- Name: closed_trades3 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades3 ALTER COLUMN lfd SET DEFAULT nextval('public.closed_trades3_lfd_seq'::regclass);


--
-- Name: closed_trades4 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades4 ALTER COLUMN lfd SET DEFAULT nextval('public.closed_trades4_lfd_seq'::regclass);


--
-- Name: closed_trades5 lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades5 ALTER COLUMN lfd SET DEFAULT nextval('public.closed_trades5_lfd_seq'::regclass);


--
-- Name: closed_trades_master id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades_master ALTER COLUMN id SET DEFAULT nextval('public.closed_trades_master_id_seq'::regclass);


--
-- Name: conv_signals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conv_signals ALTER COLUMN id SET DEFAULT nextval('public.conv_signals_id_seq'::regclass);


--
-- Name: execution_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_queue ALTER COLUMN id SET DEFAULT nextval('public.execution_queue_id_seq'::regclass);


--
-- Name: ml_predictions_master id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_predictions_master ALTER COLUMN id SET DEFAULT nextval('public.ml_predictions_master_id_seq'::regclass);


--
-- Name: ml_trend_trades id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_trend_trades ALTER COLUMN id SET DEFAULT nextval('public.ml_trend_trades_id_seq'::regclass);


--
-- Name: ml_weighted_trades3 id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_weighted_trades3 ALTER COLUMN id SET DEFAULT nextval('public.ml_weighted_trades3_id_seq'::regclass);


--
-- Name: monitored_scripts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.monitored_scripts ALTER COLUMN id SET DEFAULT nextval('public.monitored_scripts_id_seq'::regclass);


--
-- Name: orchestrator_open_trades id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orchestrator_open_trades ALTER COLUMN id SET DEFAULT nextval('public.orchestrator_open_trades_id_seq'::regclass);


--
-- Name: orchestrator_suppressed_signals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orchestrator_suppressed_signals ALTER COLUMN id SET DEFAULT nextval('public.orchestrator_suppressed_signals_id_seq'::regclass);


--
-- Name: pump_dump_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pump_dump_events ALTER COLUMN id SET DEFAULT nextval('public.pump_dump_events_id_seq'::regclass);


--
-- Name: regime_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_history ALTER COLUMN id SET DEFAULT nextval('public.regime_history_id_seq'::regclass);


--
-- Name: telegram_outbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_outbox ALTER COLUMN id SET DEFAULT nextval('public.telegram_outbox_id_seq'::regclass);


--
-- Name: trade_candidates lfd; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_candidates ALTER COLUMN lfd SET DEFAULT nextval('public.trade_candidates_lfd_seq'::regclass);


--
-- Name: trade_cooldowns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_cooldowns ALTER COLUMN id SET DEFAULT nextval('public.trade_cooldowns_id_seq'::regclass);


--
-- Name: trendmeet_rawdata id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trendmeet_rawdata ALTER COLUMN id SET DEFAULT nextval('public.trendmeet_rawdata_id_seq'::regclass);


--
-- Name: active_smc_zones active_smc_zones_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_smc_zones
    ADD CONSTRAINT active_smc_zones_pkey PRIMARY KEY (id);


--
-- Name: active_smc_zones active_smc_zones_symbol_timeframe_created_time_zone_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_smc_zones
    ADD CONSTRAINT active_smc_zones_symbol_timeframe_created_time_zone_type_key UNIQUE (symbol, timeframe, created_time, zone_type);


--
-- Name: active_trades2 active_trades2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades2
    ADD CONSTRAINT active_trades2_pkey PRIMARY KEY (lfd);


--
-- Name: active_trades3 active_trades3_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades3
    ADD CONSTRAINT active_trades3_pkey PRIMARY KEY (lfd);


--
-- Name: active_trades4 active_trades4_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades4
    ADD CONSTRAINT active_trades4_pkey PRIMARY KEY (lfd);


--
-- Name: active_trades5 active_trades5_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades5
    ADD CONSTRAINT active_trades5_pkey PRIMARY KEY (lfd);


--
-- Name: active_trades_master active_trades_master_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades_master
    ADD CONSTRAINT active_trades_master_pkey PRIMARY KEY (id);


--
-- Name: active_trades active_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.active_trades
    ADD CONSTRAINT active_trades_pkey PRIMARY KEY (lfd);


--
-- Name: ai_br_trades ai_br_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_br_trades
    ADD CONSTRAINT ai_br_trades_pkey PRIMARY KEY (id);


--
-- Name: ai_signals ai_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_signals
    ADD CONSTRAINT ai_signals_pkey PRIMARY KEY (id);


--
-- Name: bot_regime_performance bot_regime_performance_bot_name_regime_alt_context_directio_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_regime_performance
    ADD CONSTRAINT bot_regime_performance_bot_name_regime_alt_context_directio_key UNIQUE (bot_name, regime, alt_context, direction, window_days);


--
-- Name: bot_regime_performance bot_regime_performance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_regime_performance
    ADD CONSTRAINT bot_regime_performance_pkey PRIMARY KEY (id);


--
-- Name: bot_regime_whitelist bot_regime_whitelist_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_regime_whitelist
    ADD CONSTRAINT bot_regime_whitelist_pkey PRIMARY KEY (bot_name, regime, alt_context, direction);


--
-- Name: bot_trades2 bot_trades2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades2
    ADD CONSTRAINT bot_trades2_pkey PRIMARY KEY (lfd);


--
-- Name: bot_trades3 bot_trades3_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades3
    ADD CONSTRAINT bot_trades3_pkey PRIMARY KEY (lfd);


--
-- Name: bot_trades4 bot_trades4_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades4
    ADD CONSTRAINT bot_trades4_pkey PRIMARY KEY (lfd);


--
-- Name: bot_trades5 bot_trades5_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades5
    ADD CONSTRAINT bot_trades5_pkey PRIMARY KEY (lfd);


--
-- Name: bot_trades bot_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_trades
    ADD CONSTRAINT bot_trades_pkey PRIMARY KEY (lfd);


--
-- Name: closed_ai_signals closed_ai_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_ai_signals
    ADD CONSTRAINT closed_ai_signals_pkey PRIMARY KEY (id);


--
-- Name: closed_trades2 closed_trades2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades2
    ADD CONSTRAINT closed_trades2_pkey PRIMARY KEY (lfd);


--
-- Name: closed_trades3 closed_trades3_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades3
    ADD CONSTRAINT closed_trades3_pkey PRIMARY KEY (lfd);


--
-- Name: closed_trades4 closed_trades4_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades4
    ADD CONSTRAINT closed_trades4_pkey PRIMARY KEY (lfd);


--
-- Name: closed_trades5 closed_trades5_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades5
    ADD CONSTRAINT closed_trades5_pkey PRIMARY KEY (lfd);


--
-- Name: closed_trades_master closed_trades_master_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades_master
    ADD CONSTRAINT closed_trades_master_pkey PRIMARY KEY (id);


--
-- Name: closed_trades closed_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.closed_trades
    ADD CONSTRAINT closed_trades_pkey PRIMARY KEY (lfd);


--
-- Name: conv_signals conv_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conv_signals
    ADD CONSTRAINT conv_signals_pkey PRIMARY KEY (id);


--
-- Name: conv_signals conv_signals_source_bot_source_time_coin_direction_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conv_signals
    ADD CONSTRAINT conv_signals_source_bot_source_time_coin_direction_key UNIQUE (source_bot, source_time, coin, direction);


--
-- Name: execution_queue execution_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_queue
    ADD CONSTRAINT execution_queue_pkey PRIMARY KEY (id);


--
-- Name: funding_rates funding_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funding_rates
    ADD CONSTRAINT funding_rates_pkey PRIMARY KEY (symbol, funding_time);


--
-- Name: master_ai_processed_signals master_ai_processed_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.master_ai_processed_signals
    ADD CONSTRAINT master_ai_processed_signals_pkey PRIMARY KEY (signal_type, signal_id);


--
-- Name: ml_predictions_master ml_predictions_master_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_predictions_master
    ADD CONSTRAINT ml_predictions_master_pkey PRIMARY KEY (id);


--
-- Name: ml_trend_trades ml_trend_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_trend_trades
    ADD CONSTRAINT ml_trend_trades_pkey PRIMARY KEY (id);


--
-- Name: ml_weighted_trades3 ml_weighted_trades3_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_weighted_trades3
    ADD CONSTRAINT ml_weighted_trades3_pkey PRIMARY KEY (id);


--
-- Name: monitored_scripts monitored_scripts_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.monitored_scripts
    ADD CONSTRAINT monitored_scripts_name_key UNIQUE (name);


--
-- Name: monitored_scripts monitored_scripts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.monitored_scripts
    ADD CONSTRAINT monitored_scripts_pkey PRIMARY KEY (id);


--
-- Name: orchestrator_open_trades orchestrator_open_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orchestrator_open_trades
    ADD CONSTRAINT orchestrator_open_trades_pkey PRIMARY KEY (id);


--
-- Name: orchestrator_suppressed_signals orchestrator_suppressed_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orchestrator_suppressed_signals
    ADD CONSTRAINT orchestrator_suppressed_signals_pkey PRIMARY KEY (id);


--
-- Name: pump_dump_events pump_dump_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pump_dump_events
    ADD CONSTRAINT pump_dump_events_pkey PRIMARY KEY (id);


--
-- Name: regime_current regime_current_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_current
    ADD CONSTRAINT regime_current_pkey PRIMARY KEY (id);


--
-- Name: regime_history regime_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_history
    ADD CONSTRAINT regime_history_pkey PRIMARY KEY (id);


--
-- Name: regime_history regime_history_ts_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_history
    ADD CONSTRAINT regime_history_ts_key UNIQUE (ts);


--
-- Name: script_heartbeats script_heartbeats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.script_heartbeats
    ADD CONSTRAINT script_heartbeats_pkey PRIMARY KEY (script_id);


--
-- Name: telegram_outbox telegram_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_outbox
    ADD CONSTRAINT telegram_outbox_pkey PRIMARY KEY (id);


--
-- Name: trade_candidates trade_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_candidates
    ADD CONSTRAINT trade_candidates_pkey PRIMARY KEY (lfd);


--
-- Name: trade_cooldowns trade_cooldowns_module_coin_direction_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_cooldowns
    ADD CONSTRAINT trade_cooldowns_module_coin_direction_key UNIQUE (module, coin, direction);


--
-- Name: trade_cooldowns trade_cooldowns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_cooldowns
    ADD CONSTRAINT trade_cooldowns_pkey PRIMARY KEY (id);


--
-- Name: trade_scanner_state trade_scanner_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_scanner_state
    ADD CONSTRAINT trade_scanner_state_pkey PRIMARY KEY (module, signal_type);


--
-- Name: trend_data trend_data_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_data
    ADD CONSTRAINT trend_data_pkey PRIMARY KEY (symbol, open_time);


--
-- Name: trendmeet_rawdata trendmeet_rawdata_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trendmeet_rawdata
    ADD CONSTRAINT trendmeet_rawdata_pkey PRIMARY KEY (id);


--
-- Name: idx_ai_signals_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_signals_model ON public.ai_signals USING btree (model, "timestamp" DESC);


--
-- Name: idx_ai_signals_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_signals_timestamp ON public.ai_signals USING btree ("timestamp" DESC);


--
-- Name: idx_atm_coin_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_atm_coin_strategy ON public.active_trades_master USING btree (coin, strategy);


--
-- Name: idx_atm_strategy_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_atm_strategy_time ON public.active_trades_master USING btree (strategy, "time");


--
-- Name: idx_brp_alt_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brp_alt_context ON public.bot_regime_performance USING btree (alt_context);


--
-- Name: idx_brp_bot_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brp_bot_name ON public.bot_regime_performance USING btree (bot_name);


--
-- Name: idx_brp_regime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brp_regime ON public.bot_regime_performance USING btree (regime);


--
-- Name: idx_cas_dedup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cas_dedup ON public.closed_ai_signals USING btree (symbol, model, direction, open_time);


--
-- Name: idx_cas_model_close; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cas_model_close ON public.closed_ai_signals USING btree (model, close_time);


--
-- Name: idx_conv_signals_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conv_signals_bot ON public.conv_signals USING btree (source_bot, source_time DESC);


--
-- Name: idx_conv_signals_coin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conv_signals_coin ON public.conv_signals USING btree (coin, source_time DESC);


--
-- Name: idx_ctm_strategy_posted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ctm_strategy_posted ON public.closed_trades_master USING btree (strategy, posted);


--
-- Name: idx_ctm_strategy_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ctm_strategy_time ON public.closed_trades_master USING btree (strategy, "time");


--
-- Name: idx_execution_queue_status_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_execution_queue_status_time ON public.execution_queue USING btree (status, scheduled_time);


--
-- Name: idx_ml_trend_highconf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ml_trend_highconf ON public.ml_trend_trades USING btree (ml_probability) WHERE (ml_probability > (0.73)::double precision);


--
-- Name: idx_ml_trend_symbol_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ml_trend_symbol_time ON public.ml_trend_trades USING btree (symbol, created_at);


--
-- Name: idx_mpm_model_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mpm_model_time ON public.ml_predictions_master USING btree (model_name, "time");


--
-- Name: idx_mpm_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mpm_time ON public.ml_predictions_master USING btree ("time");


--
-- Name: idx_oot_coin_dir; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_oot_coin_dir ON public.orchestrator_open_trades USING btree (coin, direction);


--
-- Name: idx_oot_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_oot_status ON public.orchestrator_open_trades USING btree (status);


--
-- Name: idx_outbox_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_outbox_created ON public.telegram_outbox USING btree (created_at);


--
-- Name: idx_outbox_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_outbox_pending ON public.telegram_outbox USING btree (id) WHERE ((sent = false) AND (failed = false));


--
-- Name: idx_pde_spike_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pde_spike_time ON public.pump_dump_events USING btree (spike_time);


--
-- Name: idx_processed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_processed_at ON public.master_ai_processed_signals USING btree (processed_at);


--
-- Name: idx_regime_history_alt_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_regime_history_alt_context ON public.regime_history USING btree (alt_context);


--
-- Name: idx_regime_history_regime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_regime_history_regime ON public.regime_history USING btree (regime);


--
-- Name: idx_regime_history_ts_desc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_regime_history_ts_desc ON public.regime_history USING btree (ts DESC);


--
-- Name: idx_trade_cooldowns_module_coin_dir; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trade_cooldowns_module_coin_dir ON public.trade_cooldowns USING btree (module, coin, direction);


--
-- Name: ticker_10s_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ticker_10s_ts_idx ON public.ticker_10s USING btree (ts DESC);


--
-- Name: uq_ticker_10s_symbol_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ticker_10s_symbol_ts ON public.ticker_10s USING btree (symbol, ts);


--
-- Name: execution_queue execution_queue_script_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_queue
    ADD CONSTRAINT execution_queue_script_id_fkey FOREIGN KEY (script_id) REFERENCES public.monitored_scripts(id) ON DELETE CASCADE;


--
-- Name: script_heartbeats script_heartbeats_script_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.script_heartbeats
    ADD CONSTRAINT script_heartbeats_script_id_fkey FOREIGN KEY (script_id) REFERENCES public.monitored_scripts(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--



--
-- Per-coin family templates (representative: BTCUSDT 1h + indicators)
--

-- Name: BTCUSDT_1h; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."BTCUSDT_1h" (
    symbol text NOT NULL,
    open_time timestamp with time zone NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision
);


--
-- Name: BTCUSDT_1h_indicators; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."BTCUSDT_1h_indicators" (
    symbol text NOT NULL,
    open_time timestamp with time zone NOT NULL,
    close real,
    rsi_6 real,
    rsi_9 real,
    rsi_12 real,
    rsi_14 real,
    rsi_24 real,
    ema_7 real,
    ema_9 real,
    ema_12 real,
    ema_21 real,
    ema_26 real,
    ema_34 real,
    ema_50 real,
    ema_55 real,
    ema_89 real,
    ema_99 real,
    ema_200 real,
    ma_7 real,
    ma_10 real,
    ma_20 real,
    ma_25 real,
    ma_50 real,
    ma_99 real,
    ma_100 real,
    ma_200 real,
    wma_7 real,
    wma_9 real,
    wma_12 real,
    wma_21 real,
    wma_26 real,
    wma_34 real,
    wma_50 real,
    wma_55 real,
    wma_89 real,
    wma_99 real,
    wma_200 real,
    smma_10 real,
    smma_20 real,
    smma_25 real,
    smma_50 real,
    smma_99 real,
    smma_100 real,
    smma_200 real,
    kama_7 real,
    kama_9 real,
    kama_12 real,
    kama_21 real,
    kama_26 real,
    kama_34 real,
    kama_50 real,
    kama_55 real,
    kama_89 real,
    kama_99 real,
    atr_9 real,
    atr_14 real,
    atr_21 real,
    tsi_25_13_13 real,
    tsi_25_13_13_signal real,
    tsi_fast_12_7_7 real,
    tsi_fast_12_7_7_signal real,
    hvn_1 real,
    hvn_2 real,
    hvn_3 real,
    poc real,
    macd_dif_fast_9_21_9 real,
    macd_dea_fast_9_21_9 real,
    macd_dif_normal_12_26_9 real,
    macd_dea_normal_12_26_9 real,
    boll_upper_20 real,
    boll_mid_20 real,
    boll_lower_20 real,
    trendline_slope real,
    trendline_intercept real,
    channel_upper_price real,
    channel_lower_price real,
    trendline_price real,
    mid_line real,
    r_squared real,
    trend_direction text,
    support_price real,
    resistance_price real,
    donchian_upper_4 real,
    donchian_lower_4 real,
    donchian_mid_4 real,
    donchian_upper_10 real,
    donchian_lower_10 real,
    donchian_mid_10 real,
    donchian_upper_12 real,
    donchian_lower_12 real,
    donchian_mid_12 real,
    donchian_upper_15 real,
    donchian_lower_15 real,
    donchian_mid_15 real,
    donchian_upper_20 real,
    donchian_lower_20 real,
    donchian_mid_20 real,
    fib_support_0_236 real,
    fib_resistance_0_236 real,
    fib_support_0_382 real,
    fib_resistance_0_382 real,
    fib_support_0_5 real,
    fib_resistance_0_5 real,
    fib_support_0_618 real,
    fib_resistance_0_618 real,
    fib_support_0_786 real,
    fib_resistance_0_786 real,
    fib_extension_1_272 real,
    fib_extension_1_618 real,
    fib_extension_2_618 real
);


--
-- Name: BTCUSDT_1h_indicators BTCUSDT_1h_indicators_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."BTCUSDT_1h_indicators"
    ADD CONSTRAINT "BTCUSDT_1h_indicators_pkey" PRIMARY KEY (symbol, open_time);


--
-- Name: BTCUSDT_1h BTCUSDT_1h_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."BTCUSDT_1h"
    ADD CONSTRAINT "BTCUSDT_1h_pkey" PRIMARY KEY (symbol, open_time);


--
-- Name: idx_btcusdt_1h_ot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_btcusdt_1h_ot ON public."BTCUSDT_1h_indicators" USING btree (open_time DESC);


--
-- Name: idx_btcusdt_1h_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_btcusdt_1h_time ON public."BTCUSDT_1h" USING btree (open_time DESC);


--
-- PostgreSQL database dump complete
--


