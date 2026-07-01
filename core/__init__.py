"""
core — Shared infrastructure for all bots.

    from core.database import get_db_connection, db_connection
    from core.charting import generate_minichart_image
    from core.logging_setup import setup_logging
    from core.market_utils import (
        SignalDict, get_max_leverage, load_coins,
        is_trade_already_active, check_cooldown,
        update_cooldown, calculate_obv,
        find_support_resistance_zones,
    )
    from core.config import TELEGRAM_CHANNELS, MAIN_CHANNEL_COINS
    from core.trade_utils import calculate_smart_targets
    from core.bot_utils import get_target_channel
"""
