import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

CONFIG_FILE = "bot_config.json"
LOG_FILE = "command_logs.json"


def load_config():
    try:
        # FIX P1.36: encoding explizit — ohne utf-8 crasht cp1252 auf Emojis
        # in der Config und riss vorher das Permission-System mit.
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        # FIX P1.36: fail-CLOSED. Vorher war der Fallback {"*": ["*"]} —
        # ein kaputtes/fehlendes bot_config.json gab damit JEDEM User JEDEN
        # Command frei. Jetzt deny-all; der Fehler steht laut im Log.
        logger.error(f"🛑 Config nicht ladbar — Permission-System fällt auf DENY-ALL zurück: {e}")
        return {"channels": {}, "permissions": {}}


def check_permission(username, command):
    """Checks if the user is allowed to execute the command."""
    config = load_config()
    perms = config.get("permissions", {})

    # 1. Is everyone allowed everything?
    if "*" in perms and "*" in perms["*"]:
        return True

    # 2. Is the user in the list?
    user_perms = perms.get(username, [])
    if "*" in user_perms or command in user_perms:
        return True

    return False


def get_target_channel(channel_key):
    """Maps '1' or '2' to the real channel ID."""
    config = load_config()
    channels = config.get("channels", {})
    return channels.get(str(channel_key))


def log_command_atomic(username, command, full_text):
    """Atomically and safely saves the command call to a JSON file."""
    try:
        logs = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, encoding="utf-8") as f:
                logs = json.load(f)

        logs.append(
            {
                "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "user": username,
                "command": command,
                "full_text": full_text,
            }
        )

        # Atomic write via .tmp
        tmp_file = LOG_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(logs, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, LOG_FILE)

    except Exception as e:
        logger.error(f"Error logging command: {e}")
