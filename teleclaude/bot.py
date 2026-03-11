#!/usr/bin/env python3
"""Telegram bot to remotely launch Claude Code on macOS."""

import fcntl
import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Logging with rotation (#11)
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("teleclaude")
logger.setLevel(logging.INFO)

# 5MB per file, keep 3 backups
file_handler = RotatingFileHandler(
    LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3
)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(file_handler)
logger.addHandler(logging.StreamHandler())

# ---------------------------------------------------------------------------
# Config with file locking (#10)
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "")  # (#4) required first owner
CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "allowed_chat_ids": [],
    "base_directories": [],
    "preferred_terminal": "auto",  # (#6) "auto", "iterm", "terminal"
    "show_dotdirs": False,  # (#13) whether to show hidden directories
}


def load_config() -> dict:
    """Load config with shared file lock."""
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            cfg = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        # Merge defaults for any missing keys
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load config: %s", e)
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """Save config with exclusive file lock and atomic write."""
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    try:
        with open(tmp_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)
        tmp_path.replace(CONFIG_PATH)
    except OSError as e:
        logger.error("Failed to save config: %s", e)
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Path cache for callback_data (#2)
# ---------------------------------------------------------------------------
# Telegram limits callback_data to 64 bytes.
# We store paths in a dict keyed by short hash.

_path_cache: dict[str, str] = {}


def path_to_id(path: str) -> str:
    """Map a filesystem path to a short 8-char hash for callback_data."""
    short_id = hashlib.sha256(path.encode()).hexdigest()[:8]
    _path_cache[short_id] = path
    return short_id


def id_to_path(short_id: str) -> str | None:
    """Resolve a short hash back to a filesystem path."""
    return _path_cache.get(short_id)


# ---------------------------------------------------------------------------
# Security helpers (#3, #4)
# ---------------------------------------------------------------------------


def is_allowed(update: Update) -> bool:
    cfg = load_config()
    allowed = cfg.get("allowed_chat_ids", [])

    # (#4) If no owner is registered yet, only OWNER_CHAT_ID from .env can access
    if not allowed:
        if OWNER_CHAT_ID:
            return update.effective_chat.id == int(OWNER_CHAT_ID)
        # No OWNER_CHAT_ID set — reject everyone until configured
        return False

    return update.effective_chat.id in allowed


def validate_path(dirpath: str, cfg: dict) -> str | None:
    """Return resolved path string if it exists and is under a registered base dir."""
    try:
        resolved = Path(dirpath).expanduser().resolve()
    except Exception:
        return None

    if not resolved.is_dir():
        return None

    bases = [Path(b).expanduser().resolve() for b in cfg.get("base_directories", [])]
    if not bases:
        return None

    for base in bases:
        # Allow the base directory itself or any child
        if resolved == base:
            return str(resolved)
        try:
            resolved.relative_to(base)
            return str(resolved)
        except ValueError:
            continue
    return None


def is_under_base(dirpath: str, cfg: dict) -> bool:
    """Check if path is a base directory or under one. (#3)"""
    return validate_path(dirpath, cfg) is not None


# ---------------------------------------------------------------------------
# macOS helpers (#1, #6, #7, #12)
# ---------------------------------------------------------------------------


def escape_for_applescript(s: str) -> str:
    """Escape a string for safe embedding in AppleScript double-quoted strings. (#1)"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def is_mac_awake() -> bool:
    """Check if the Mac display is awake. (#12)"""
    try:
        result = subprocess.run(
            ["pmset", "-g", "powerstate", "IODisplayWrangler"],
            capture_output=True, text=True, timeout=5,
        )
        # Power state 4 = display on
        for line in result.stdout.split("\n"):
            if "IODisplayWrangler" in line:
                return "4" in line.split()
        # Fallback: check if any display is active
        result2 = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True, text=True, timeout=10,
        )
        return "Display Asleep: Yes" not in result2.stdout
    except Exception:
        return True  # Assume awake if detection fails


def get_terminal(cfg: dict) -> str:
    """Determine which terminal to use. (#6)"""
    pref = cfg.get("preferred_terminal", "auto")
    if pref in ("iterm", "terminal"):
        return pref
    # auto-detect
    if os.path.isdir("/Applications/iTerm.app"):
        return "iterm"
    return "terminal"


def open_claude_in_terminal(dirpath: str, cfg: dict) -> tuple[bool, str]:
    """
    Open a new terminal window and run `claude` in dirpath.
    Returns (success, message). (#1 fix, #7 feedback)
    """
    terminal = get_terminal(cfg)
    safe_path = escape_for_applescript(dirpath)
    shell_path = shlex.quote(dirpath)

    if terminal == "iterm":
        script = (
            'tell application "iTerm"\n'
            "    activate\n"
            "    set newWindow to (create window with default profile)\n"
            "    tell current session of newWindow\n"
            f'        write text "cd {shell_path} && claude"\n'
            "    end tell\n"
            "end tell"
        )
    else:
        script = (
            'tell application "Terminal"\n'
            "    activate\n"
            f'    do script "cd {shell_path} && claude"\n'
            "end tell"
        )

    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip()
            logger.error("osascript failed: %s", err)
            return False, f"Terminal launch failed: {err}"
        return True, f"Claude Code launched in {terminal} at {dirpath}"
    except subprocess.TimeoutExpired:
        return False, "Terminal launch timed out (10s)"
    except Exception as e:
        logger.error("Launch error: %s", e)
        return False, f"Launch error: {e}"


# ---------------------------------------------------------------------------
# Pagination helper (#8)
# ---------------------------------------------------------------------------

DIRS_PER_PAGE = 15


def paginate_dirs(
    subdirs: list[Path], page: int, parent_path_id: str, cfg: dict
) -> tuple[list[list[InlineKeyboardButton]], str]:
    """Build paginated inline keyboard for directory listing."""
    total = len(subdirs)
    total_pages = max(1, (total + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * DIRS_PER_PAGE
    end = min(start + DIRS_PER_PAGE, total)
    page_dirs = subdirs[start:end]

    keyboard: list[list[InlineKeyboardButton]] = []

    # Launch button
    keyboard.append([InlineKeyboardButton(
        "🚀 Launch Claude here", callback_data=f"L:{parent_path_id}"
    )])

    # Subdirectory buttons
    for subdir in page_dirs:
        sid = path_to_id(str(subdir))
        keyboard.append([InlineKeyboardButton(
            f"📂 {subdir.name}", callback_data=f"B:{sid}"
        )])

    # Pagination row
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(
                "⬅️ Prev", callback_data=f"P:{parent_path_id}:{page - 1}"
            ))
        nav_row.append(InlineKeyboardButton(
            f"{page + 1}/{total_pages}", callback_data="noop"
        ))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(
                "➡️ Next", callback_data=f"P:{parent_path_id}:{page + 1}"
            ))
        keyboard.append(nav_row)

    # Truncation info
    info = ""
    if total > DIRS_PER_PAGE:
        info = f" (showing {start + 1}-{end} of {total})"

    return keyboard, info


# ---------------------------------------------------------------------------
# Bot command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text(
            "Access denied.\nAsk the owner to add your chat ID, "
            "or set OWNER_CHAT_ID in .env if this is your first setup."
        )
        return

    chat_id = update.effective_chat.id
    cfg = load_config()
    allowed = cfg.get("allowed_chat_ids", [])
    if chat_id not in allowed:
        allowed.append(chat_id)
        cfg["allowed_chat_ids"] = allowed
        save_config(cfg)
        logger.info("Registered chat_id: %s", chat_id)

    await update.message.reply_text(
        "Claude Code Launcher\n\n"
        "Commands:\n"
        "/register <path> - Register a base directory\n"
        "/unregister <path> - Remove a base directory\n"
        "/dirs - Browse registered directories\n"
        "/launch <path> - Launch Claude Code in directory\n"
        "/status - Show running Claude sessions\n"
        "/terminal <auto|iterm|terminal> - Set preferred terminal\n"
        "/dotdirs <on|off> - Toggle hidden directory visibility\n"
        f"\nYour chat ID: {chat_id}"
    )


async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /register <directory_path>")
        return

    dirpath = " ".join(ctx.args)
    resolved = Path(dirpath).expanduser().resolve()

    if not resolved.is_dir():
        await update.message.reply_text(f"Directory not found: {resolved}")
        return

    cfg = load_config()
    bases = cfg.get("base_directories", [])
    resolved_str = str(resolved)

    if resolved_str in bases:
        await update.message.reply_text(f"Already registered: {resolved_str}")
        return

    bases.append(resolved_str)
    cfg["base_directories"] = bases
    save_config(cfg)
    logger.info("Registered base directory: %s", resolved_str)
    await update.message.reply_text(f"Registered: {resolved_str}")


async def cmd_unregister(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a registered base directory. (#5)"""
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    cfg = load_config()
    bases = cfg.get("base_directories", [])

    if not ctx.args:
        # Show current bases as numbered list for easy removal
        if not bases:
            await update.message.reply_text("No base directories registered.")
            return
        lines = [f"{i+1}. `{b}`" for i, b in enumerate(bases)]
        await update.message.reply_text(
            "Registered directories:\n" + "\n".join(lines)
            + "\n\nUsage: /unregister <path or number>",
            parse_mode="Markdown",
        )
        return

    arg = " ".join(ctx.args)

    # Allow removal by index number
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(bases):
            removed = bases.pop(idx)
            cfg["base_directories"] = bases
            save_config(cfg)
            logger.info("Unregistered base directory: %s", removed)
            await update.message.reply_text(f"Removed: {removed}")
            return
        await update.message.reply_text(f"Invalid index: {arg}")
        return

    # Allow removal by path
    resolved_str = str(Path(arg).expanduser().resolve())
    if resolved_str in bases:
        bases.remove(resolved_str)
        cfg["base_directories"] = bases
        save_config(cfg)
        logger.info("Unregistered base directory: %s", resolved_str)
        await update.message.reply_text(f"Removed: {resolved_str}")
    else:
        await update.message.reply_text(f"Not found in registered directories: {arg}")


async def cmd_dirs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    cfg = load_config()
    bases = cfg.get("base_directories", [])

    if not bases:
        await update.message.reply_text(
            "No base directories registered.\nUse /register <path> to add one."
        )
        return

    keyboard = []
    for base in bases:
        label = Path(base).name or base
        bid = path_to_id(base)
        keyboard.append([InlineKeyboardButton(f"📂 {label}", callback_data=f"B:{bid}")])

    await update.message.reply_text(
        "Registered directories:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_launch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /launch <directory_path>")
        return

    dirpath = " ".join(ctx.args)
    cfg = load_config()
    resolved = validate_path(dirpath, cfg)

    if not resolved:
        await update.message.reply_text(
            f"Invalid path: {dirpath}\n"
            "Path must exist and be under a registered base directory."
        )
        return

    # (#12) Check if Mac is awake
    if not is_mac_awake():
        await update.message.reply_text(
            "Mac appears to be in sleep mode.\n"
            "Terminal cannot be opened while the display is off."
        )
        return

    # (#7) Launch with feedback
    success, msg = open_claude_in_terminal(resolved, cfg)
    if success:
        await update.message.reply_text(f"🚀 {msg}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Failed: {msg}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    try:
        result = subprocess.run(
            ["pgrep", "-af", "claude"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [
            l for l in result.stdout.strip().split("\n")
            if l and "pgrep" not in l
        ]
        if lines:
            msg = "Running Claude processes:\n\n" + "\n".join(
                f"• `{l}`" for l in lines[:10]
            )
        else:
            msg = "No Claude processes found."
    except Exception as e:
        msg = f"Error checking status: {e}"

    # (#12) Add Mac wake status
    awake = is_mac_awake()
    msg += f"\n\nMac display: {'awake' if awake else 'asleep'}"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_terminal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set preferred terminal. (#6)"""
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    valid = ("auto", "iterm", "terminal")
    if not ctx.args or ctx.args[0].lower() not in valid:
        cfg = load_config()
        current = cfg.get("preferred_terminal", "auto")
        await update.message.reply_text(
            f"Current: {current}\nUsage: /terminal <{'|'.join(valid)}>"
        )
        return

    choice = ctx.args[0].lower()
    cfg = load_config()
    cfg["preferred_terminal"] = choice
    save_config(cfg)
    await update.message.reply_text(f"Terminal set to: {choice}")


async def cmd_dotdirs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle hidden directory visibility. (#13)"""
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    cfg = load_config()
    current = cfg.get("show_dotdirs", False)

    if not ctx.args:
        await update.message.reply_text(
            f"Hidden directories: {'visible' if current else 'hidden'}\n"
            "Usage: /dotdirs <on|off>"
        )
        return

    arg = ctx.args[0].lower()
    if arg in ("on", "true", "1", "yes"):
        cfg["show_dotdirs"] = True
    elif arg in ("off", "false", "0", "no"):
        cfg["show_dotdirs"] = False
    else:
        await update.message.reply_text("Usage: /dotdirs <on|off>")
        return

    save_config(cfg)
    status = "visible" if cfg["show_dotdirs"] else "hidden"
    await update.message.reply_text(f"Hidden directories: {status}")


# ---------------------------------------------------------------------------
# Inline keyboard callback handler (#2, #3, #8, #9)
# ---------------------------------------------------------------------------


async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        await query.edit_message_text("Access denied.")
        return

    data = query.data
    cfg = load_config()

    # --- Browse directory: B:<path_id> ---
    if data.startswith("B:"):
        path_id = data[2:]
        dirpath = id_to_path(path_id)
        if not dirpath:
            await query.edit_message_text("Session expired. Use /dirs again.")
            return

        # (#3) Validate path is under a registered base
        if not is_under_base(dirpath, cfg):
            await query.edit_message_text("Access denied: path is not under a registered directory.")
            return

        resolved = Path(dirpath).expanduser().resolve()
        if not resolved.is_dir():
            await query.edit_message_text(f"Directory not found: {resolved}")
            return

        show_dot = cfg.get("show_dotdirs", False)
        try:
            subdirs = sorted([
                d for d in resolved.iterdir()
                if d.is_dir() and (show_dot or not d.name.startswith("."))
            ])
        except PermissionError:
            await query.edit_message_text(f"Permission denied: {resolved}")
            return

        parent_id = path_to_id(str(resolved))
        keyboard, page_info = paginate_dirs(subdirs, 0, parent_id, cfg)

        # (#9) Register as base button — only show if not already a base
        bases = [Path(b).expanduser().resolve() for b in cfg.get("base_directories", [])]
        if resolved not in bases:
            keyboard.append([InlineKeyboardButton(
                "📌 Register as base directory",
                callback_data=f"R:{parent_id}",
            )])

        # Back button
        parent = resolved.parent
        for base in bases:
            if parent == base or _is_child_of(parent, base):
                pid = path_to_id(str(parent))
                keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"B:{pid}")])
                break

        dir_label = str(resolved)
        if not subdirs:
            msg = f"📂 `{dir_label}`\n\nNo subdirectories."
        else:
            msg = f"📂 `{dir_label}`\n\n{len(subdirs)} subdirectories{page_info}:"

        await query.edit_message_text(
            msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )

    # --- Paginate: P:<path_id>:<page> ---
    elif data.startswith("P:"):
        parts = data.split(":")
        if len(parts) != 3:
            return
        path_id, page_str = parts[1], parts[2]
        dirpath = id_to_path(path_id)
        if not dirpath:
            await query.edit_message_text("Session expired. Use /dirs again.")
            return

        if not is_under_base(dirpath, cfg):
            await query.edit_message_text("Access denied.")
            return

        resolved = Path(dirpath).expanduser().resolve()
        if not resolved.is_dir():
            await query.edit_message_text("Directory not found.")
            return

        show_dot = cfg.get("show_dotdirs", False)
        try:
            subdirs = sorted([
                d for d in resolved.iterdir()
                if d.is_dir() and (show_dot or not d.name.startswith("."))
            ])
        except PermissionError:
            await query.edit_message_text(f"Permission denied: {resolved}")
            return

        page = int(page_str) if page_str.isdigit() else 0
        keyboard, page_info = paginate_dirs(subdirs, page, path_id, cfg)

        # Back button
        parent = resolved.parent
        bases = [Path(b).expanduser().resolve() for b in cfg.get("base_directories", [])]
        for base in bases:
            if parent == base or _is_child_of(parent, base):
                pid = path_to_id(str(parent))
                keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"B:{pid}")])
                break

        msg = f"📂 `{resolved}`\n\n{len(subdirs)} subdirectories{page_info}:"
        await query.edit_message_text(
            msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )

    # --- Launch: L:<path_id> ---
    elif data.startswith("L:"):
        path_id = data[2:]
        dirpath = id_to_path(path_id)
        if not dirpath:
            await query.edit_message_text("Session expired. Use /dirs again.")
            return

        resolved = validate_path(dirpath, cfg)
        if not resolved:
            await query.edit_message_text("Invalid or unregistered path.")
            return

        if not is_mac_awake():
            await query.edit_message_text(
                "Mac appears to be in sleep mode.\n"
                "Terminal cannot be opened while the display is off."
            )
            return

        success, msg = open_claude_in_terminal(resolved, cfg)
        if success:
            await query.edit_message_text(f"🚀 {msg}", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"Failed: {msg}")

    # --- Register as base: R:<path_id> (#9) ---
    elif data.startswith("R:"):
        path_id = data[2:]
        dirpath = id_to_path(path_id)
        if not dirpath:
            await query.edit_message_text("Session expired. Use /dirs again.")
            return

        resolved = Path(dirpath).expanduser().resolve()
        if not resolved.is_dir():
            await query.edit_message_text("Directory not found.")
            return

        bases = cfg.get("base_directories", [])
        resolved_str = str(resolved)
        if resolved_str not in bases:
            bases.append(resolved_str)
            cfg["base_directories"] = bases
            save_config(cfg)
            logger.info("Registered base directory via UI: %s", resolved_str)

        await query.edit_message_text(f"📌 Registered as base directory:\n`{resolved_str}`", parse_mode="Markdown")

    # --- No-op (page indicator) ---
    elif data == "noop":
        pass


def _is_child_of(child: Path, parent: Path) -> bool:
    """Check if child is a descendant of parent."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env file")
        sys.exit(1)

    if not OWNER_CHAT_ID:
        print(
            "Warning: OWNER_CHAT_ID not set in .env file.\n"
            "No one will be able to use the bot until it is configured.\n"
            "Get your chat ID from @userinfobot on Telegram, then add:\n"
            "  OWNER_CHAT_ID=123456789\n"
            "to your .env file."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("unregister", cmd_unregister))
    app.add_handler(CommandHandler("dirs", cmd_dirs))
    app.add_handler(CommandHandler("launch", cmd_launch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("terminal", cmd_terminal))
    app.add_handler(CommandHandler("dotdirs", cmd_dotdirs))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
