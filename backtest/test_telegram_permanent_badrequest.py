"""Guard for the permanent-BadRequest classification in 4_telegram_bot.py.

T-2026-KYT-9050-130. In python-telegram-bot ``BadRequest`` is a ``NetworkError``
SUBCLASS (22.5: BadRequest -> NetworkError -> TelegramError), so the sender's
``except NetworkError`` clause already catches every 400 and the terminal
``except TelegramError`` below it never sees one. Until this fix each permanently
undeliverable message was therefore retried ``MAX_ATTEMPTS`` times, and every
retry entered ``failed_channels`` — stalling that channel for the rest of the
poll cycle — and burned a global send slot.

The part worth pinning is the CARVE-OUT, not the happy path: a malformed-HTML
rejection must stay retryable, because P2.11 drops ``parse_mode`` on the last
attempt and that recovery path is the reason those messages get through at all.
A blanket "terminate on every BadRequest" would be the same misclassification as
the original, only in the other direction — and it would silently disable P2.11.

DB-free: only the pure predicate and the allowlist are exercised.
"""

from __future__ import annotations

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# core.config raises at import when its _required() vars are unset.
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

_spec = importlib.util.spec_from_file_location(
    "telegram_bot_under_test", os.path.join(ROOT, "4_telegram_bot.py")
)
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)


# ---------- the carve-out: these MUST stay retryable ----------

def test_malformed_html_stays_retryable():
    """P2.11 retries the last attempt without parse_mode — terminating here
    would disable that recovery and drop messages that do get through today."""
    assert not tg.is_permanent_bad_request(
        "Can't parse entities: unsupported start tag \"foo\" at byte offset 42"
    )


def test_unknown_bad_request_stays_retryable():
    assert not tg.is_permanent_bad_request("Bad Request: something we have never seen")


def test_empty_and_none_stay_retryable():
    assert not tg.is_permanent_bad_request("")
    assert not tg.is_permanent_bad_request(None)


# ---------- the permanent set ----------

def test_message_too_long_is_permanent():
    """The only entry evidenced in this deployment (490 rows in telegram_outbox)."""
    assert tg.is_permanent_bad_request("Message is too long")


def test_matching_is_case_insensitive_and_substring():
    """Telegram wraps the reason in varying prefixes; the real rows read
    'NetworkError (unknown outcome): Message is too long'."""
    assert tg.is_permanent_bad_request("NetworkError (unknown outcome): Message is too long")
    assert tg.is_permanent_bad_request("MESSAGE IS TOO LONG")


def test_every_allowlist_entry_classifies_as_permanent():
    for reason in tg.PERMANENT_BAD_REQUEST_REASONS:
        assert tg.is_permanent_bad_request(reason), reason


def test_allowlist_is_lowercase_or_matching_breaks():
    """The predicate lowercases the input and compares against the constants
    verbatim — an uppercase entry would silently never match."""
    for reason in tg.PERMANENT_BAD_REQUEST_REASONS:
        assert reason == reason.lower(), reason


# ---------- the classification the whole fix hinges on ----------

def test_bad_request_really_is_a_networkerror_subclass():
    """If this ever stops holding, the `isinstance` check in the NetworkError
    clause becomes dead code and permanent 400s silently go back to being
    retried three times."""
    from telegram.error import BadRequest, NetworkError

    assert issubclass(BadRequest, NetworkError)
