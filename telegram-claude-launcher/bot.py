#!/usr/bin/env python3
"""Telegram bot to remotely launch Claude Code on macOS."""

import json
import os
import subprocess
import sys
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
# Config
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"allowed_chat_ids": [], "base_directories": []}

def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def is_allowed(update: Update) -> bool:
    cfg = load_config()
    allowed = cfg.get("allowed_chat_ids", [])
    if not allowed:
        # First user becomes the owner automatically
        return True
    return update.effective_chat.id in allowed

def validate_path(dirpath: str, cfg: dict) -> str | None:
    """Return resolved path if valid, else None."""
    try:
        resolved = Path(dirpath).expanduser().resolve()
    except Exception:
        return None

    if not resolved.is_dir():
        return None

    # Must be under one of the registered base directories
    bases = [Path(b).expanduser().resolve() for b in cfg.get("base_directories", [])]
    if not bases:
        return None

    for base in bases:
        try:
            resolved.relative_to(base)
            return str(resolved)
        except ValueError:
            continue
    return None

# ---------------------------------------------------------------------------
# Terminal launcher (macOS)
# ---------------------------------------------------------------------------

def detect_terminal() -> str:
    """Detect whether iTerm2 is installed; fall back to Terminal.app."""
    iterm_path = "/Applications/iTerm.app"
    if os.path.isdir(iterm_path):
        return "iterm"
    return "terminal"

def open_claude_in_terminal(dirpath: str) -> None:
    """Open a new terminal window/tab and run `claude` in dirpath."""
    terminal = detect_terminal()

    if terminal == "iterm":
        script = f'''
        tell application "iTerm"
            activate
            set newWindow to (create window with default profile)
            tell current session of newWindow
                write text "cd {dirpath} && claude"
            end tell
        end tell
        '''
    else:
        script = f'''
        tell application "Terminal"
            activate
            do script "cd {dirpath} && claude"
        end tell
        '''

    subprocess.Popen(["osascript", "-e", script])

# ---------------------------------------------------------------------------
# Bot command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    chat_id = update.effective_chat.id
    cfg = load_config()
    allowed = cfg.get("allowed_chat_ids", [])
    if chat_id not in allowed:
        allowed.append(chat_id)
        cfg["allowed_chat_ids"] = allowed
        save_config(cfg)

    await update.message.reply_text(
        "Claude Code Launcher\n\n"
        "Commands:\n"
        "/register <path> - Register a base directory\n"
        "/dirs - Browse registered directories\n"
        "/launch <path> - Launch Claude Code in directory\n"
        "/status - Show running Claude sessions\n"
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
    await update.message.reply_text(f"Registered: {resolved_str}")

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

    # Show base directories as inline keyboard
    keyboard = []
    for base in bases:
        label = Path(base).name or base
        keyboard.append([InlineKeyboardButton(f"📂 {label}", callback_data=f"browse:{base}")])

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

    open_claude_in_terminal(resolved)
    await update.message.reply_text(f"Launching Claude Code in:\n`{resolved}`", parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Access denied.")
        return

    try:
        result = subprocess.run(
            ["pgrep", "-af", "claude"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l for l in result.stdout.strip().split("\n") if l and "pgrep" not in l]
        if lines:
            msg = "Running Claude processes:\n\n" + "\n".join(f"• `{l}`" for l in lines[:10])
        else:
            msg = "No Claude processes found."
    except Exception as e:
        msg = f"Error checking status: {e}"

    await update.message.reply_text(msg, parse_mode="Markdown")

# ---------------------------------------------------------------------------
# Inline keyboard callback handler
# ---------------------------------------------------------------------------

async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        await query.edit_message_text("Access denied.")
        return

    data = query.data
    cfg = load_config()

    if data.startswith("browse:"):
        dirpath = data[len("browse:"):]
        resolved = Path(dirpath).expanduser().resolve()

        if not resolved.is_dir():
            await query.edit_message_text(f"Directory not found: {resolved}")
            return

        # List subdirectories
        try:
            subdirs = sorted([d for d in resolved.iterdir() if d.is_dir() and not d.name.startswith(".")])
        except PermissionError:
            await query.edit_message_text(f"Permission denied: {resolved}")
            return

        keyboard = []

        # Launch button for current directory
        keyboard.append([InlineKeyboardButton(
            f"🚀 Launch Claude here", callback_data=f"launch:{resolved}"
        )])

        # Subdirectory buttons
        for subdir in subdirs[:20]:  # Limit to 20 to avoid Telegram limits
            label = subdir.name
            keyboard.append([InlineKeyboardButton(
                f"📂 {label}", callback_data=f"browse:{subdir}"
            )])

        # Back button (go to parent if it's under a base dir)
        parent = resolved.parent
        bases = [Path(b).expanduser().resolve() for b in cfg.get("base_directories", [])]
        for base in bases:
            try:
                parent.relative_to(base)
                keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"browse:{parent}")])
                break
            except ValueError:
                if parent == base:
                    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"browse:{parent}")])
                    break

        dir_label = str(resolved)
        if not subdirs:
            msg = f"📂 `{dir_label}`\n\nNo subdirectories."
        else:
            msg = f"📂 `{dir_label}`\n\n{len(subdirs)} subdirectories:"

        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("launch:"):
        dirpath = data[len("launch:"):]
        resolved = validate_path(dirpath, cfg)

        if not resolved:
            await query.edit_message_text("Invalid or unregistered path.")
            return

        open_claude_in_terminal(resolved)
        await query.edit_message_text(
            f"🚀 Launching Claude Code in:\n`{resolved}`", parse_mode="Markdown"
        )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env file")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("dirs", cmd_dirs))
    app.add_handler(CommandHandler("launch", cmd_launch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
