import json
import os
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ChatMember
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Logging setup
logging.basicConfig(
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
# Suppress noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._application").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._updater").setLevel(logging.WARNING)
logger = logging.getLogger("EliteMarket")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")
SECURITY_CHANNEL_ID = "-1003450478165"
MONITORED_GROUP_ID = -1003451490827

# Known member user IDs — additions by these users are considered trusted
KNOWN_MEMBER_IDS = {
    1166772148,
    1870644348,
    5651721135,
    5662585948,
    6526824979,
    6659288294,
    6864194951,
    7001100331,
    7090417167,
    7279906688,
    7338429782,
    7422906767,
    7707071842,
    7715451354,
}

# Auto-kick timeout in seconds (30 minutes)
AUTO_KICK_TIMEOUT = 30 * 60
# Security check interval in seconds (1 hour)
SECURITY_CHECK_INTERVAL = 60 * 60

# Persistent store for member display info (keyed by "member_id:group_id")
# Used to keep callback data under Telegram's 64-byte limit
_DATA_FILE = Path(__file__).parent / "member_data.json"
_member_info: dict[str, dict] = {}

# Username cache: lowercase username -> {"user_id": int, "first_name": str, "username": str}
_username_cache: dict[str, dict] = {}

# Dynamic known members added via !allow (persisted to JSON)
_dynamic_known: set[int] = set()


def _save_data() -> None:
    """Persist _member_info, _username_cache, and _dynamic_known to disk."""
    serializable_members = {}
    for key, val in _member_info.items():
        entry = dict(val)
        # Convert datetime to ISO string for JSON
        if isinstance(entry.get("added_at"), datetime):
            entry["added_at"] = entry["added_at"].isoformat()
        serializable_members[key] = entry
    try:
        data = {
            "members": serializable_members,
            "username_cache": _username_cache,
            "dynamic_known": list(_dynamic_known),
        }
        _DATA_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.error("Failed to save member data: %s", e)


def _load_data() -> None:
    """Load _member_info, _username_cache, and _dynamic_known from disk on startup."""
    global _member_info, _username_cache, _dynamic_known
    if not _DATA_FILE.exists():
        return
    try:
        raw = json.loads(_DATA_FILE.read_text())
        # Support both old format (flat dict) and new format ({"members": ..., "username_cache": ...})
        if "members" in raw and isinstance(raw["members"], dict):
            members_raw = raw["members"]
            _username_cache = raw.get("username_cache", {})
            _dynamic_known = set(raw.get("dynamic_known", []))
        else:
            # Old format: raw is the member dict directly
            members_raw = raw
        for key, val in members_raw.items():
            # Convert ISO string back to datetime
            if isinstance(val.get("added_at"), str):
                val["added_at"] = datetime.fromisoformat(val["added_at"])
            _member_info[key] = val
        # Merge dynamic known members into the working set
        KNOWN_MEMBER_IDS.update(_dynamic_known)
        logger.info("Loaded %d member entries, %d cached usernames, %d dynamic known from disk",
                    len(_member_info), len(_username_cache), len(_dynamic_known))
    except Exception as e:
        logger.error("Failed to load member data: %s", e)


def _cache_user(user) -> None:
    """Cache a user's username -> user_id mapping for later lookup."""
    if user and getattr(user, "username", None):
        key = user.username.lower()
        new_entry = {
            "user_id": user.id,
            "first_name": getattr(user, "first_name", "") or "",
            "last_name": getattr(user, "last_name", "") or "",
            "username": user.username,
        }
        # Only write to disk if the entry is new or changed
        if _username_cache.get(key) != new_entry:
            _username_cache[key] = new_entry
            _save_data()


def _get_username_display(user) -> str:
    """Return @username if available, otherwise the user's full name."""
    if user.username:
        return f"@{user.username}"
    return user.full_name


def _build_security_message(
    new_member_display: str,
    added_by_display: str,
    hours: int,
) -> str:
    """Build the security protocol message."""
    hour_text = f"{hours} hour" if hours == 1 else f"{hours} hours"
    return (
        f"‼️ <b>SECURITY PROTOCOL</b> ‼️\n"
        f"\n"
        f"{new_member_display} (added by {added_by_display}) "
        f"is still in the group for more than {hour_text}, "
        f'if the deal is still running then click on '
        f'"<b>✅ I am still doing deal</b>" '
        f'if not then click on "<b>❌ Kick Member</b>" '
        f"please respond to the message within 30 minutes "
        f"or the member will be auto kicked."
    )


def _store_member_info(
    new_member_id: int,
    group_chat_id: int,
    hours: int,
    new_member_display: str,
    added_by_display: str,
    adder_id: int = 0,
    adder_known: bool = True,
) -> None:
    """Store member display info in memory for callback lookups."""
    key = f"{new_member_id}:{group_chat_id}"
    existing = _member_info.get(key, {})
    _member_info[key] = {
        "member_disp": new_member_display,
        "adder_disp": added_by_display,
        "hours": hours,
        "adder_id": adder_id,
        "adder_known": existing.get("adder_known", adder_known),
        "added_at": existing.get("added_at", datetime.now(timezone.utc)),
        "12h_used": existing.get("12h_used", False),
    }
    _save_data()


def _build_security_keyboard(
    new_member_id: int,
    group_chat_id: int,
    hours: int,
    new_member_display: str,
    added_by_display: str,
    adder_id: int = 0,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for the security protocol message."""
    # Store display info in memory so callback data stays under 64 bytes
    _store_member_info(
        new_member_id, group_chat_id, hours, new_member_display, added_by_display,
        adder_id=adder_id,
    )
    # Compact callback data: "d:member_id:group_id" for deal, "k:member_id:group_id" for kick
    callback_data_deal = f"d:{new_member_id}:{group_chat_id}"
    callback_data_kick = f"k:{new_member_id}:{group_chat_id}"
    callback_data_12h = f"h:{new_member_id}:{group_chat_id}"
    keyboard = [
        [InlineKeyboardButton("✅ I am still doing deal", callback_data=callback_data_deal)],
        [InlineKeyboardButton("❌ Kick Member", callback_data=callback_data_kick)],
        [InlineKeyboardButton("+12 Hours", callback_data=callback_data_12h)],
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_security_check(
    context: ContextTypes.DEFAULT_TYPE,
    new_member_id: int,
    group_chat_id: int,
    hours: int,
    new_member_display: str,
    added_by_display: str,
    target_chat_id: int | None = None,
    adder_id: int = 0,
) -> None:
    """Send security protocol message and schedule auto-kick."""
    send_to = target_chat_id or int(SECURITY_CHANNEL_ID)
    message_text = _build_security_message(new_member_display, added_by_display, hours)
    keyboard = _build_security_keyboard(
        new_member_id, group_chat_id, hours, new_member_display, added_by_display,
        adder_id=adder_id,
    )

    try:
        sent_msg = await context.bot.send_message(
            chat_id=send_to,
            text=message_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info(
            "Sent security check for member %s in group %s (hour %s)",
            new_member_id,
            group_chat_id,
            hours,
        )

        # Schedule auto-kick after 30 minutes if no response
        job_name = f"autokick_{new_member_id}_{group_chat_id}"
        # Remove any existing auto-kick job for this member
        existing_jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in existing_jobs:
            job.schedule_removal()

        context.job_queue.run_once(
            auto_kick_member,
            when=AUTO_KICK_TIMEOUT,
            name=job_name,
            data={
                "member_id": new_member_id,
                "group_id": group_chat_id,
                "message_id": sent_msg.message_id,
                "sent_chat_id": send_to,
            },
        )
    except Exception as e:
        logger.error("Failed to send security check: %s", e)


async def schedule_security_check(
    context: ContextTypes.DEFAULT_TYPE,
    new_member_id: int,
    group_chat_id: int,
    hours: int,
    new_member_display: str,
    added_by_display: str,
    adder_id: int = 0,
) -> None:
    """Schedule the first (or next) security check after SECURITY_CHECK_INTERVAL."""
    job_name = f"security_{new_member_id}_{group_chat_id}"
    # Remove any existing security check job for this member
    existing_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in existing_jobs:
        job.schedule_removal()

    context.job_queue.run_once(
        _security_check_job,
        when=SECURITY_CHECK_INTERVAL,
        name=job_name,
        data={
            "member_id": new_member_id,
            "group_id": group_chat_id,
            "hours": hours,
            "member_disp": new_member_display,
            "adder_disp": added_by_display,
            "adder_id": adder_id,
        },
    )
    logger.info(
        "Scheduled security check for member %s in %s seconds (hour %s)",
        new_member_id,
        SECURITY_CHECK_INTERVAL,
        hours,
    )


async def _is_member_in_group(bot, member_id: int, group_id: int) -> bool:
    """Check if a user is still a member of the group."""
    try:
        chat_member = await bot.get_chat_member(chat_id=group_id, user_id=member_id)
        return chat_member.status in (
            ChatMember.MEMBER,
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
            ChatMember.RESTRICTED,
        )
    except Exception as e:
        logger.warning("Could not check membership for %s in %s: %s", member_id, group_id, e)
        return False


def _cleanup_tracked_member(member_id: int, group_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a tracked member from storage and cancel their jobs."""
    info_key = f"{member_id}:{group_id}"
    if info_key in _member_info:
        _member_info.pop(info_key, None)
        _save_data()
    # Cancel security check job
    sec_jobs = context.job_queue.get_jobs_by_name(f"security_{member_id}_{group_id}")
    for job in sec_jobs:
        job.schedule_removal()
    # Cancel auto-kick job
    kick_jobs = context.job_queue.get_jobs_by_name(f"autokick_{member_id}_{group_id}")
    for job in kick_jobs:
        job.schedule_removal()
    logger.info("Cleaned up tracking for member %s in group %s (no longer in group)", member_id, group_id)


async def _security_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback that sends the security check message."""
    data = context.job.data
    member_id = data["member_id"]
    group_id = data["group_id"]

    # Skip if the member is now a known member (e.g. via !allow)
    if member_id in KNOWN_MEMBER_IDS:
        _cleanup_tracked_member(member_id, group_id, context)
        logger.info("Skipping security check for %s — now a known member", member_id)
        return

    # Verify member is still in the group before sending protocol message
    if not await _is_member_in_group(context.bot, member_id, group_id):
        _cleanup_tracked_member(member_id, group_id, context)
        return

    await send_security_check(
        context,
        new_member_id=member_id,
        group_chat_id=group_id,
        hours=data["hours"],
        new_member_display=data["member_disp"],
        added_by_display=data["adder_disp"],
        adder_id=data.get("adder_id", 0),
    )


async def auto_kick_member(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-kick member after 30 minutes of no response."""
    data = context.job.data
    member_id = data["member_id"]
    group_id = data["group_id"]
    message_id = data["message_id"]

    # Skip if the member is now a known member (e.g. via !allow)
    if member_id in KNOWN_MEMBER_IDS:
        _cleanup_tracked_member(member_id, group_id, context)
        logger.info("Skipping auto-kick for %s — now a known member", member_id)
        # Remove buttons from the message
        sent_chat_id = data.get("sent_chat_id", int(SECURITY_CHANNEL_ID))
        try:
            msg = await context.bot.edit_message_reply_markup(
                chat_id=sent_chat_id, message_id=message_id, reply_markup=None,
            )
            original_text = msg.text_html if hasattr(msg, "text_html") and msg.text_html else (msg.text or "")
            await context.bot.edit_message_text(
                chat_id=sent_chat_id, message_id=message_id,
                text=f"{original_text}\n\nStatus: Member is now a known member",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # Verify member is still in the group before auto-kicking
    if not await _is_member_in_group(context.bot, member_id, group_id):
        _cleanup_tracked_member(member_id, group_id, context)
        # Edit message to show they already left
        sent_chat_id = data.get("sent_chat_id", int(SECURITY_CHANNEL_ID))
        try:
            msg = await context.bot.edit_message_reply_markup(
                chat_id=sent_chat_id,
                message_id=message_id,
                reply_markup=None,
            )
            original_text = ""
            if hasattr(msg, "text_html") and msg.text_html:
                original_text = msg.text_html
            elif hasattr(msg, "text") and msg.text:
                original_text = msg.text
            await context.bot.edit_message_text(
                chat_id=sent_chat_id,
                message_id=message_id,
                text=f"{original_text}\n\nStatus: Member already left the group",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Failed to edit message for departed member: %s", e)
        return

    try:
        await context.bot.ban_chat_member(chat_id=group_id, user_id=member_id)
        await context.bot.unban_chat_member(chat_id=group_id, user_id=member_id)
        logger.info("Auto-kicked member %s from group %s", member_id, group_id)

        # Edit the security message: keep original text, remove buttons, append status
        sent_chat_id = data.get("sent_chat_id", int(SECURITY_CHANNEL_ID))
        try:
            msg = await context.bot.edit_message_reply_markup(
                chat_id=sent_chat_id,
                message_id=message_id,
                reply_markup=None,
            )
            original_text = ""
            if hasattr(msg, "text_html") and msg.text_html:
                original_text = msg.text_html
            elif hasattr(msg, "text") and msg.text:
                original_text = msg.text
            await context.bot.edit_message_text(
                chat_id=sent_chat_id,
                message_id=message_id,
                text=f"{original_text}\n\nStatus: Auto-Kicked (no response within 30 minutes)",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Failed to edit security message after auto-kick: %s", e)

    except Exception as e:
        logger.error("Failed to auto-kick member %s from group %s: %s", member_id, group_id, e)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses on security protocol and help messages."""
    query = update.callback_query

    raw = query.data or ""

    # Handle help category buttons
    if raw.startswith("help:"):
        category = raw.split(":", 1)[1]
        await query.answer()
        await _handle_help_callback(query, category)
        return

    parts = raw.split(":")
    if len(parts) < 3:
        await query.answer()
        return

    action = parts[0]  # "d" for deal, "k" for kick, "h" for +12h, "hy"/"hn" for confirm
    member_id = int(parts[1])
    group_id = int(parts[2])
    info_key = f"{member_id}:{group_id}"

    # Only the known member who added this person can use the buttons
    info = _member_info.get(info_key, {})
    adder_id = int(info.get("adder_id", 0))
    if adder_id and query.from_user.id != adder_id:
        await query.answer("Only the person who added this member can use this button.", show_alert=True)
        return

    # Check +12h one-time use before answering
    if action == "h" and info.get("12h_used"):
        await query.answer("The +12 Hours skip has already been used for this member.", show_alert=True)
        return

    await query.answer()

    if action == "k":
        # Cancel the auto-kick job
        job_name = f"autokick_{member_id}_{group_id}"
        existing_jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in existing_jobs:
            job.schedule_removal()

        # Cancel any pending security check job
        sec_job_name = f"security_{member_id}_{group_id}"
        sec_jobs = context.job_queue.get_jobs_by_name(sec_job_name)
        for job in sec_jobs:
            job.schedule_removal()

        # Clean up stored info
        _member_info.pop(info_key, None)
        _save_data()

        try:
            await context.bot.ban_chat_member(chat_id=group_id, user_id=member_id)
            await context.bot.unban_chat_member(chat_id=group_id, user_id=member_id)
            # Keep original message, remove buttons, append status
            original_text = query.message.text_html or query.message.text or ""
            await query.edit_message_text(
                text=f"{original_text}\n\nStatus: Kicked",
                parse_mode="HTML",
            )
            logger.info("Kicked member %s from group %s via button", member_id, group_id)
        except Exception as e:
            await query.edit_message_text(f"Failed to kick member: {e}")
            logger.error("Failed to kick member %s: %s", member_id, e)

    elif action == "d":
        info = _member_info.get(info_key, {})
        hours = int(info.get("hours", 1))
        new_member_display = str(info.get("member_disp", "Unknown"))
        added_by_display = str(info.get("adder_disp", "Unknown"))
        stored_adder_id = int(info.get("adder_id", 0))

        # Cancel the auto-kick job
        job_name = f"autokick_{member_id}_{group_id}"
        existing_jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in existing_jobs:
            job.schedule_removal()

        # If the member is now a known member, stop the cycle
        if member_id in KNOWN_MEMBER_IDS:
            _member_info.pop(info_key, None)
            _save_data()
            await query.edit_message_text(
                text=(
                    f"✅ {new_member_display} is now a known member. No further checks needed.\n\n"
                    f"- {new_member_display} (added by {added_by_display})"
                ),
                parse_mode="HTML",
            )
            return

        # Update the message to confirm, include member and adder info
        await query.edit_message_text(
            text=(
                f"✅ Deal confirmed. Will check again in 1 hour.\n\n"
                f"- {new_member_display} (added by {added_by_display})"
            ),
            parse_mode="HTML",
        )

        # Schedule another check in 1 hour with incremented hours
        await schedule_security_check(
            context,
            new_member_id=member_id,
            group_chat_id=group_id,
            hours=hours + 1,
            new_member_display=new_member_display,
            added_by_display=added_by_display,
            adder_id=stored_adder_id,
        )

    elif action == "h":
        # +12 Hours button — one-time use (already validated above)
        info = _member_info.get(info_key, {})

        # Send confirmation message with Yes/No buttons
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes", callback_data=f"hy:{member_id}:{group_id}")],
            [InlineKeyboardButton("No", callback_data=f"hn:{member_id}:{group_id}")],
        ])
        # Store the original security message id for later editing
        info["12h_orig_msg_id"] = query.message.message_id
        info["12h_orig_chat_id"] = query.message.chat_id
        _member_info[info_key] = info
        _save_data()

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "<b>This button can be used only One-Time for night deals "
                "or time taking deals, please confirm your decision.</b>"
            ),
            parse_mode="HTML",
            reply_markup=confirm_keyboard,
        )

    elif action == "hy":
        # Yes — confirm +12 Hours skip
        info = _member_info.get(info_key, {})
        new_member_display = str(info.get("member_disp", "Unknown"))
        added_by_display = str(info.get("adder_disp", "Unknown"))
        stored_adder_id = int(info.get("adder_id", 0))
        hours = int(info.get("hours", 1))

        # Mark as used
        info["12h_used"] = True
        _member_info[info_key] = info
        _save_data()

        # Cancel the auto-kick job
        job_name = f"autokick_{member_id}_{group_id}"
        existing_jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in existing_jobs:
            job.schedule_removal()

        # Cancel any pending security check job
        sec_job_name = f"security_{member_id}_{group_id}"
        sec_jobs = context.job_queue.get_jobs_by_name(sec_job_name)
        for job in sec_jobs:
            job.schedule_removal()

        # Edit the original security protocol message: keep text, remove buttons, add status
        orig_msg_id = info.get("12h_orig_msg_id")
        orig_chat_id = info.get("12h_orig_chat_id")
        if orig_msg_id and orig_chat_id:
            try:
                orig_msg = await context.bot.edit_message_reply_markup(
                    chat_id=orig_chat_id,
                    message_id=orig_msg_id,
                    reply_markup=None,
                )
                original_text = ""
                if hasattr(orig_msg, "text_html") and orig_msg.text_html:
                    original_text = orig_msg.text_html
                elif hasattr(orig_msg, "text") and orig_msg.text:
                    original_text = orig_msg.text
                await context.bot.edit_message_text(
                    chat_id=orig_chat_id,
                    message_id=orig_msg_id,
                    text=f"{original_text}\n\n<b>Status: Added 12 Hours Skip</b>",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("Failed to edit original security message for 12h skip: %s", e)

        # Edit the confirmation message: remove buttons
        await query.edit_message_text(
            text="<b>+12 Hours skip confirmed.</b>",
            parse_mode="HTML",
        )

        # Schedule next check in 12 hours
        await schedule_security_check(
            context,
            new_member_id=member_id,
            group_chat_id=group_id,
            hours=hours + 12,
            new_member_display=new_member_display,
            added_by_display=added_by_display,
            adder_id=stored_adder_id,
        )

    elif action == "hn":
        # No — cancel +12 Hours skip, allow reuse
        await query.edit_message_text(
            text="<b>+12 Hours skip cancelled.</b>",
            parse_mode="HTML",
        )


async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle chat member updates — detect when someone is added to the group."""
    if not LOG_CHANNEL_ID:
        logger.warning("LOG_CHANNEL_ID is not set. Skipping log.")
        return

    result = update.chat_member
    if result is None:
        return

    # Only monitor the specific group
    if result.chat.id != MONITORED_GROUP_ID:
        return

    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    # Detect a new member being added (was not in the group, now is a member)
    was_member = old_status in (
        ChatMember.MEMBER,
        ChatMember.ADMINISTRATOR,
        ChatMember.OWNER,
    )
    is_member = new_status in (
        ChatMember.MEMBER,
        ChatMember.ADMINISTRATOR,
        ChatMember.OWNER,
    )

    # Detect member leaving: was a member, now is NOT a member
    if was_member and not is_member:
        left_user = result.new_chat_member.user
        _cache_user(left_user)
        info_key = f"{left_user.id}:{result.chat.id}"
        if info_key in _member_info:
            _cleanup_tracked_member(left_user.id, result.chat.id, context)
        return

    if not (not was_member and is_member):
        # Not a new addition and not a leave — ignore
        return

    new_member = result.new_chat_member.user
    added_by = result.from_user
    group_chat_id = result.chat.id

    # Cache both users for !add @username lookup
    _cache_user(new_member)
    _cache_user(added_by)

    # Skip tracking if the new member is themselves a known member
    if new_member.id in KNOWN_MEMBER_IDS:
        logger.info("Known member %s joined/added — skipping tracking", new_member.id)
        return

    IST = timezone(timedelta(hours=5, minutes=30))
    timestamp = result.date or datetime.now(timezone.utc)
    ist_time = timestamp.astimezone(IST)
    formatted_time = ist_time.strftime("%Y-%m-%d %H:%M:%S IST")

    added_by_display = _get_username_display(added_by)
    new_member_display = _get_username_display(new_member)

    if added_by.id in KNOWN_MEMBER_IDS:
        # Known member added someone — friendly log
        log_message = (
            f"~ {new_member_display} (<code>{new_member.id}</code>) has been added by "
            f"{added_by_display} (<code>{added_by.id}</code>) in the Elite Market Group ‼️"
        )

        # Schedule security check after 1 hour
        await schedule_security_check(
            context,
            new_member_id=new_member.id,
            group_chat_id=group_chat_id,
            hours=1,
            new_member_display=new_member_display,
            added_by_display=added_by_display,
            adder_id=added_by.id,
        )
    else:
        # Unknown person added someone — alert
        log_message = (
            f"🚨 <b>MEMBER ADDED BY A UNKNOWN PERSON</b> ‼️\n"
            f"\n"
            f"Username - {new_member_display} (<code>{new_member.id}</code>)\n"
            f"Added by - {added_by_display} (<code>{added_by.id}</code>)\n"
            f"Date - {formatted_time}\n"
            f"\n"
            f"If it is not done by any of the group members "
            f"please kick both of them asap to avoid deal disruption."
        )

        # Also track unknown-added members for /unklist
        _store_member_info(
            new_member_id=new_member.id,
            group_chat_id=group_chat_id,
            hours=0,
            new_member_display=new_member_display,
            added_by_display=added_by_display,
            adder_id=added_by.id,
            adder_known=False,
        )

    try:
        await context.bot.send_message(
            chat_id=int(LOG_CHANNEL_ID),
            text=log_message,
            parse_mode="HTML",
        )
        logger.info(
            "Logged new member %s (added by %s, known=%s)",
            new_member.id,
            added_by.id,
            added_by.id in KNOWN_MEMBER_IDS,
        )
    except Exception as e:
        logger.error("Failed to send log message: %s", e)


async def _send_long_message(bot, chat_id: int, text: str) -> None:
    """Send a message, splitting into multiple messages if it exceeds Telegram's 4096 char limit."""
    max_len = 4000  # Leave some margin below 4096
    if len(text) <= max_len:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return

    # Split by lines, accumulate chunks
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        candidate = chunk + ("\n" if chunk else "") + line
        if len(candidate) > max_len:
            if chunk:
                await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")


async def unklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List tracked members in the monitored group, split by known/unknown adder."""
    try:
        tracked_keys = list(_member_info.keys())
        if not tracked_keys:
            await context.bot.send_message(
                chat_id=int(SECURITY_CHANNEL_ID),
                text="📋 <b>Tracked Members List</b>\n\nNo tracked members currently in the group.",
                parse_mode="HTML",
            )
            return

        known_lines = []
        unknown_lines = []
        now = datetime.now(timezone.utc)
        for key in tracked_keys:
            info = _member_info[key]
            member_disp = info.get("member_disp", "Unknown")
            adder_disp = info.get("adder_disp", "Unknown")
            added_at = info.get("added_at")
            adder_known = info.get("adder_known", True)
            parts = key.split(":")
            member_id = parts[0]
            # Calculate duration since added
            if added_at:
                delta = now - added_at
                total_seconds = int(delta.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                if hours > 0:
                    duration_text = f"{hours}h {minutes}m"
                else:
                    duration_text = f"{minutes}m"
            else:
                duration_text = "unknown"
            line = f"\u2022 {member_disp} (<code>{member_id}</code>) \u2014 added by {adder_disp} \u2014 in group since {duration_text}"
            if adder_known:
                known_lines.append(line)
            else:
                unknown_lines.append(line)

        sections = []
        if known_lines:
            sections.append(
                f"\U0001f464 <b>Added by Known Members ({len(known_lines)})</b>\n" + "\n".join(known_lines)
            )
        if unknown_lines:
            sections.append(
                f"\U0001f6a8 <b>Added by Unknown Members ({len(unknown_lines)})</b>\n" + "\n".join(unknown_lines)
            )

        total = len(known_lines) + len(unknown_lines)
        message_text = (
            f"\U0001f4cb <b>Tracked Members List</b>\n\n"
            + "\n\n".join(sections)
            + f"\n\nTotal: {total} member(s)."
        )

        await _send_long_message(context.bot, int(SECURITY_CHANNEL_ID), message_text)
    except Exception as e:
        logger.error("Failed to send tracked members list: %s", e)


async def _resolve_user(
    message, text: str, context: ContextTypes.DEFAULT_TYPE,
) -> tuple[int | None, str | None]:
    """Resolve a user from message entities, reply, or text argument.

    Returns (user_id, display_name) or (None, None) if unresolved.
    """
    resolved_user_id = None
    resolved_display = None

    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention":
                _cache_user(entity.user)
                resolved_user_id = entity.user.id
                resolved_display = _get_username_display(entity.user)
                break
            elif entity.type == "mention":
                username_text = message.text[entity.offset:entity.offset + entity.length].lstrip("@").lower()
                cached = _username_cache.get(username_text)
                if cached:
                    resolved_user_id = cached["user_id"]
                    resolved_display = f"@{cached['username']}"
                else:
                    try:
                        chat_obj = await context.bot.get_chat(chat_id=f"@{username_text}")
                        _cache_user(chat_obj)
                        resolved_user_id = chat_obj.id
                        resolved_display = _get_username_display(chat_obj)
                    except Exception:
                        pass
                break

    if resolved_user_id is None and message.reply_to_message and message.reply_to_message.from_user:
        reply_user = message.reply_to_message.from_user
        _cache_user(reply_user)
        resolved_user_id = reply_user.id
        resolved_display = _get_username_display(reply_user)

    if resolved_user_id is None:
        parts = text.split()
        if len(parts) >= 2:
            arg = parts[1].strip()
            if arg.isdigit():
                try:
                    member = await context.bot.get_chat_member(
                        chat_id=MONITORED_GROUP_ID,
                        user_id=int(arg),
                    )
                    _cache_user(member.user)
                    resolved_user_id = member.user.id
                    resolved_display = _get_username_display(member.user)
                except Exception as e:
                    logger.warning("Could not resolve user ID %s: %s", arg, e)
            else:
                username_raw = arg.lstrip("@").lower()
                cached = _username_cache.get(username_raw)
                if cached:
                    resolved_user_id = cached["user_id"]
                    resolved_display = f"@{cached['username']}"

    return resolved_user_id, resolved_display


async def handle_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle !add @username — lets known members manually add users to tracking."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not text.lower().startswith("!add"):
        return

    # Only known members can use this command
    sender = update.effective_user
    if not sender or sender.id not in KNOWN_MEMBER_IDS:
        return

    chat = update.effective_chat
    if not chat:
        return

    _cache_user(sender)

    resolved_user_id, resolved_display = await _resolve_user(message, text, context)

    if resolved_user_id is None:
        await message.reply_text(
            "Could not resolve the user. Make sure the username or user ID is correct.\n\n"
            "You can also reply to a message from that user with <b>!add</b>.",
            parse_mode="HTML",
        )
        return

    new_member_display = resolved_display or f"User {resolved_user_id}"
    adder_display = _get_username_display(sender)

    # Store in member info — always use monitored group as the group_chat_id
    _store_member_info(
        new_member_id=resolved_user_id,
        group_chat_id=MONITORED_GROUP_ID,
        hours=1,
        new_member_display=new_member_display,
        added_by_display=adder_display,
        adder_id=sender.id,
    )

    # Schedule security check after 1 hour
    await schedule_security_check(
        context,
        new_member_id=resolved_user_id,
        group_chat_id=MONITORED_GROUP_ID,
        hours=1,
        new_member_display=new_member_display,
        added_by_display=adder_display,
        adder_id=sender.id,
    )

    await message.reply_text(
        f"{new_member_display} (<code>{resolved_user_id}</code>) has been manually added to tracking by {adder_display}.",
        parse_mode="HTML",
    )
    logger.info(
        "Known member %s manually added %s (%s) to tracking",
        sender.id, resolved_user_id, new_member_display,
    )


async def handle_12hr_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle !12hr @username/user_id — lets known members manually apply 12h skip."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not text.lower().startswith("!12hr"):
        return

    # Only known members can use this command
    sender = update.effective_user
    if not sender or sender.id not in KNOWN_MEMBER_IDS:
        return

    chat = update.effective_chat
    if not chat:
        return

    _cache_user(sender)

    resolved_user_id, resolved_display = await _resolve_user(message, text, context)

    if resolved_user_id is None:
        await message.reply_text(
            "Could not resolve the user. Make sure the username or user ID is correct.\n\n"
            "You can also reply to a message from that user with <b>!12hr</b>.",
            parse_mode="HTML",
        )
        return

    new_member_display = resolved_display or f"User {resolved_user_id}"
    info_key = f"{resolved_user_id}:{MONITORED_GROUP_ID}"
    info = _member_info.get(info_key, {})

    if not info:
        await message.reply_text(
            f"{new_member_display} is not currently being tracked. Use <b>!add</b> first.",
            parse_mode="HTML",
        )
        return

    if info.get("12h_used"):
        await message.reply_text(
            f"The +12 Hours skip has already been used for {new_member_display}.",
            parse_mode="HTML",
        )
        return

    # Mark as used
    info["12h_used"] = True
    _member_info[info_key] = info
    _save_data()

    # Cancel the auto-kick job
    job_name = f"autokick_{resolved_user_id}_{MONITORED_GROUP_ID}"
    existing_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in existing_jobs:
        job.schedule_removal()

    # Cancel any pending security check job
    sec_job_name = f"security_{resolved_user_id}_{MONITORED_GROUP_ID}"
    sec_jobs = context.job_queue.get_jobs_by_name(sec_job_name)
    for job in sec_jobs:
        job.schedule_removal()

    adder_display = _get_username_display(sender)
    hours = int(info.get("hours", 1))
    added_by_display = str(info.get("adder_disp", adder_display))

    # Schedule next check in 12 hours
    await schedule_security_check(
        context,
        new_member_id=resolved_user_id,
        group_chat_id=MONITORED_GROUP_ID,
        hours=hours + 12,
        new_member_display=new_member_display,
        added_by_display=added_by_display,
        adder_id=int(info.get("adder_id", sender.id)),
    )

    await message.reply_text(
        f"<b>+12 Hours skip applied</b> for {new_member_display} (<code>{resolved_user_id}</code>) by {adder_display}.\n"
        f"Next security check in 12 hours.",
        parse_mode="HTML",
    )
    logger.info(
        "Known member %s applied 12h skip for %s (%s)",
        sender.id, resolved_user_id, new_member_display,
    )


async def handle_allow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle !allow @username/user_id — lets known members promote someone to known member."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not text.lower().startswith("!allow"):
        return

    sender = update.effective_user
    if not sender or sender.id not in KNOWN_MEMBER_IDS:
        return

    chat = update.effective_chat
    if not chat:
        return

    _cache_user(sender)

    resolved_user_id, resolved_display = await _resolve_user(message, text, context)

    if resolved_user_id is None:
        await message.reply_text(
            "Could not resolve the user. Make sure the username or user ID is correct.\n\n"
            "You can also reply to a message from that user with <b>!allow</b>.",
            parse_mode="HTML",
        )
        return

    display = resolved_display or f"User {resolved_user_id}"

    if resolved_user_id in KNOWN_MEMBER_IDS:
        await message.reply_text(
            f"{display} (<code>{resolved_user_id}</code>) is already a known member.",
            parse_mode="HTML",
        )
        return

    # Add to known members
    KNOWN_MEMBER_IDS.add(resolved_user_id)
    _dynamic_known.add(resolved_user_id)
    _save_data()

    # If they were being tracked, clean up their tracking
    info_key = f"{resolved_user_id}:{MONITORED_GROUP_ID}"
    if info_key in _member_info:
        _cleanup_tracked_member(resolved_user_id, MONITORED_GROUP_ID, context)

    adder_display = _get_username_display(sender)
    await message.reply_text(
        f"{display} (<code>{resolved_user_id}</code>) has been added to known members by {adder_display}.",
        parse_mode="HTML",
    )
    logger.info("Known member %s promoted %s (%s) to known member", sender.id, resolved_user_id, display)


async def handle_demote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle !demote @username/user_id — lets known members remove someone from known members."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not text.lower().startswith("!demote"):
        return

    sender = update.effective_user
    if not sender or sender.id not in KNOWN_MEMBER_IDS:
        return

    chat = update.effective_chat
    if not chat:
        return

    _cache_user(sender)

    resolved_user_id, resolved_display = await _resolve_user(message, text, context)

    if resolved_user_id is None:
        await message.reply_text(
            "Could not resolve the user. Make sure the username or user ID is correct.\n\n"
            "You can also reply to a message from that user with <b>!demote</b>.",
            parse_mode="HTML",
        )
        return

    display = resolved_display or f"User {resolved_user_id}"

    if resolved_user_id not in _dynamic_known:
        await message.reply_text(
            f"{display} (<code>{resolved_user_id}</code>) is not a dynamically added known member (only !allow'd members can be demoted).",
            parse_mode="HTML",
        )
        return

    # Remove from known members
    KNOWN_MEMBER_IDS.discard(resolved_user_id)
    _dynamic_known.discard(resolved_user_id)
    _save_data()

    adder_display = _get_username_display(sender)
    await message.reply_text(
        f"{display} (<code>{resolved_user_id}</code>) has been removed from known members by {adder_display}.",
        parse_mode="HTML",
    )
    logger.info("Known member %s demoted %s (%s) from known member", sender.id, resolved_user_id, display)


async def handle_refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle !refresh — check all tracked members and remove those no longer in the group."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not text.lower().startswith("!refresh"):
        return

    sender = update.effective_user
    if not sender or sender.id not in KNOWN_MEMBER_IDS:
        return

    # Iterate over all tracked members and check if they're still in the group
    keys_to_remove: list[tuple[int, int]] = []
    for key in list(_member_info.keys()):
        parts = key.split(":")
        if len(parts) != 2:
            continue
        member_id = int(parts[0])
        group_id = int(parts[1])
        if not await _is_member_in_group(context.bot, member_id, group_id):
            keys_to_remove.append((member_id, group_id))

    removed_count = 0
    for member_id, group_id in keys_to_remove:
        _cleanup_tracked_member(member_id, group_id, context)
        removed_count += 1

    total_remaining = len(_member_info)
    await message.reply_text(
        f"Refreshed tracking list. Removed <b>{removed_count}</b> member(s) no longer in the group.\n"
        f"Currently tracking <b>{total_remaining}</b> member(s).",
        parse_mode="HTML",
    )
    logger.info("Refresh by %s: removed %d, remaining %d", sender.id, removed_count, total_remaining)


async def handle_restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle !restart — clear all tracked members and cancel all timers."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not text.lower().startswith("!restart"):
        return

    sender = update.effective_user
    if not sender or sender.id not in KNOWN_MEMBER_IDS:
        return

    # Cancel all jobs and clear tracking
    cleared_count = len(_member_info)
    for key in list(_member_info.keys()):
        parts = key.split(":")
        if len(parts) == 2:
            member_id = int(parts[0])
            group_id = int(parts[1])
            _cleanup_tracked_member(member_id, group_id, context)

    await message.reply_text(
        f"Tracking list has been reset. Cleared <b>{cleared_count}</b> member(s).\n"
        f"All pending security checks and auto-kick timers have been cancelled.",
        parse_mode="HTML",
    )
    logger.info("Restart by %s: cleared %d tracked members", sender.id, cleared_count)


async def _handle_help_callback(query, category: str) -> None:
    """Handle help category button presses by editing the help message."""
    if category == "slash":
        text = (
            "\U0001f4d6 <b>Slash Commands</b>\n\n"
            "\u2022 <code>/test</code> \u2014 Send a sample security protocol message to the current chat for testing.\n\n"
            "\u2022 <code>/unklist</code> \u2014 List all tracked members split by known/unknown adder. Sent to the security channel.\n\n"
            "\u2022 <code>!knlist</code> \u2014 List all known members (static + dynamically added). Sent to the security channel.\n\n"
            "\u2022 <code>!help</code> \u2014 Show this help menu."
        )
    elif category == "member":
        text = (
            "\U0001f465 <b>Member Management</b>\n\n"
            "\u2022 <code>!add @username / user_id / reply</code>\n"
            "  Manually add a user to the tracking list. Bot treats them like a newly added member.\n\n"
            "\u2022 <code>!allow @username / user_id / reply</code>\n"
            "  Promote a user to the known members list. They will be trusted by the bot.\n\n"
            "\u2022 <code>!demote @username / user_id / reply</code>\n"
            "  Remove a user from the known members list (only dynamically added members).\n\n"
            "\u2022 <code>!12hr @username / user_id / reply</code>\n"
            "  Apply a 12-hour skip for a tracked member. One-time use per tracked member."
        )
    elif category == "tools":
        text = (
            "\U0001f6e0\ufe0f <b>Bot Tools</b>\n\n"
            "\u2022 <code>!refresh</code> \u2014 Check all tracked members' group membership and remove anyone who has left.\n\n"
            "\u2022 <code>!restart</code> \u2014 Clear all tracked members and cancel all pending timers. Starts from zero.\n\n"
            "\u2022 <code>!knlist</code> \u2014 List all known members.\n\n"
            "\u2022 <code>!help</code> \u2014 Show this help menu."
        )
    elif category == "auto":
        text = (
            "\u2699\ufe0f <b>Automatic Behaviors</b>\n\n"
            "\u2022 <b>Known member adds someone</b> \u2014 Friendly log sent to the log channel + security check scheduled after 1 hour.\n\n"
            "\u2022 <b>Unknown person adds someone</b> \u2014 Alert sent to the log channel with a warning. Member tracked in /unklist.\n\n"
            "\u2022 <b>Security Protocol</b> \u2014 After 1 hour, asks the adder to confirm deal status with 3 buttons:\n"
            "  \u2705 I am still doing deal \u2014 Resets timer, checks again in 1 hour\n"
            "  \u274c Kick Member \u2014 Kicks the added member immediately\n"
            "  +12 Hours \u2014 One-time skip for night/long deals\n\n"
            "\u2022 <b>Auto-Kick</b> \u2014 If no button is clicked within 30 minutes.\n\n"
            "\u2022 <b>Member Leaves</b> \u2014 Automatically removed from tracking."
        )
    elif category == "main":
        # Go back to main help menu
        text = _build_help_main_text()
    else:
        return

    keyboard = _build_help_keyboard(category)

    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error("Failed to edit help message: %s", e)


def _build_help_main_text() -> str:
    """Build the main help menu text."""
    return (
        "\U0001f4d6 <b>Elite Market Security Bot \u2014 Help</b>\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
        "Welcome! This bot monitors group member activity, enforces a security protocol "
        "for newly added members, and provides tools for known members to manage the group.\n\n"
        "\u2022 <b>All timestamps</b> are in IST (UTC+5:30)\n"
        "\u2022 <b>Commands with !</b> are for known members only\n"
        "\u2022 <b>Commands work</b> in any group where the bot is present\n\n"
        "Select a category below to learn more:"
    )


def _build_help_keyboard(current_category: str = "main") -> InlineKeyboardMarkup:
    """Build the help inline keyboard based on current category."""
    if current_category == "main":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4cb Slash Commands", callback_data="help:slash")],
            [InlineKeyboardButton("\U0001f465 Member Management", callback_data="help:member")],
            [InlineKeyboardButton("\U0001f6e0\ufe0f Bot Tools", callback_data="help:tools")],
            [InlineKeyboardButton("\u2699\ufe0f Automatic Behaviors", callback_data="help:auto")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\u25c0\ufe0f Back to Menu", callback_data="help:main")],
        ])


async def handle_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle !help — show the help menu with inline buttons."""
    message = update.effective_message
    if not message or not message.text:
        return

    sender = update.effective_user
    logger.info("!help triggered by user %s (id=%s)", sender.full_name if sender else "?", sender.id if sender else "?")

    if not sender or sender.id not in KNOWN_MEMBER_IDS:
        logger.warning("!help denied: user %s not in known members", sender.id if sender else "?")
        return

    help_text = _build_help_main_text()
    keyboard = _build_help_keyboard("main")

    try:
        await message.reply_text(
            text=help_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info("!help menu sent successfully")
    except Exception as e:
        logger.error("Failed to send help message: %s", e)


async def handle_knlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle !knlist — list all known members and send to the security channel."""
    message = update.effective_message
    if not message or not message.text:
        return

    sender = update.effective_user
    logger.info("!knlist triggered by user %s (id=%s)", sender.full_name if sender else "?", sender.id if sender else "?")

    if not sender or sender.id not in KNOWN_MEMBER_IDS:
        logger.warning("!knlist denied: user %s not in known members", sender.id if sender else "?")
        return

    try:
        # Build reverse lookup from username cache: user_id -> display name
        id_to_display: dict[int, str] = {}
        for _uname, info in _username_cache.items():
            uid = info.get("user_id")
            if uid:
                username = info.get("username", "")
                first_name = info.get("first_name", "")
                if username:
                    id_to_display[uid] = f"@{username}"
                elif first_name:
                    id_to_display[uid] = first_name

        # Hardcoded known members
        static_ids = {
            1166772148, 1870644348, 5651721135, 5662585948,
            6526824979, 6659288294, 6864194951, 7001100331,
            7090417167, 7279906688, 7338429782, 7422906767,
            7707071842, 7715451354,
        }

        static_lines = []
        for uid in sorted(static_ids):
            display = id_to_display.get(uid, "Unknown")
            static_lines.append(f"\u2022 {display} (<code>{uid}</code>)")

        dynamic_lines = []
        for uid in sorted(_dynamic_known):
            display = id_to_display.get(uid, "Unknown")
            dynamic_lines.append(f"\u2022 {display} (<code>{uid}</code>)")

        sections = []
        sections.append(
            f"\U0001f512 <b>Static Known Members ({len(static_lines)})</b>\n" + "\n".join(static_lines)
        )
        if dynamic_lines:
            sections.append(
                f"\u2795 <b>Dynamically Added Members ({len(dynamic_lines)})</b>\n" + "\n".join(dynamic_lines)
            )

        total = len(static_ids) + len(_dynamic_known)
        message_text = (
            f"\U0001f4cb <b>Known Members List</b>\n\n"
            + "\n\n".join(sections)
            + f"\n\nTotal: {total} known member(s)."
        )

        await _send_long_message(context.bot, int(SECURITY_CHANNEL_ID), message_text)
        logger.info("!knlist sent to security channel")
    except Exception as e:
        logger.error("Failed to send known members list: %s", e)


async def _cache_message_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Silently cache the sender's username from any message in the monitored group."""
    user = update.effective_user
    if user:
        _cache_user(user)


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a test security protocol message to the chat where /test is used."""
    test_member_display = "@test_member"
    test_adder_display = "@test_adder"
    test_member_id = 123456789
    test_chat_id = update.effective_chat.id

    # Build message and keyboard without storing in _member_info
    message_text = _build_security_message(test_member_display, test_adder_display, 1)
    callback_data_deal = f"d:{test_member_id}:{test_chat_id}"
    callback_data_kick = f"k:{test_member_id}:{test_chat_id}"
    callback_data_12h = f"h:{test_member_id}:{test_chat_id}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I am still doing deal", callback_data=callback_data_deal)],
        [InlineKeyboardButton("❌ Kick Member", callback_data=callback_data_kick)],
        [InlineKeyboardButton("+12 Hours", callback_data=callback_data_12h)],
    ])

    try:
        await context.bot.send_message(
            chat_id=test_chat_id,
            text=message_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error("Failed to send test security message: %s", e)


def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set!")
        return

    if not LOG_CHANNEL_ID:
        logger.warning(
            "LOG_CHANNEL_ID is not set. The bot will run but won't send logs anywhere."
        )

    # Load persisted member data from disk
    _load_data()

    application = Application.builder().token(BOT_TOKEN).build()

    # Monitor member changes in groups (other users being added/removed)
    application.add_handler(
        ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER)
    )

    # Handle inline button callbacks
    application.add_handler(CallbackQueryHandler(handle_callback))

    # /test command for testing security protocol
    application.add_handler(CommandHandler("test", test_command))

    # /unklist command to list unknown members in the monitored group
    application.add_handler(CommandHandler("unklist", unklist_command))

    # !knlist handler for known members to list known members
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!knlist"), handle_knlist_command)
    )

    # !add @username handler for known members to manually track users
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!add"), handle_add_command)
    )

    # !12hr @username handler for known members to manually apply 12h skip
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!12hr"), handle_12hr_command)
    )

    # !allow @username handler for known members to promote users to known members
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!allow"), handle_allow_command)
    )

    # !demote @username handler for known members to remove users from known members
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!demote"), handle_demote_command)
    )

    # !refresh handler for known members to refresh the tracking list
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!refresh"), handle_refresh_command)
    )

    # !restart handler for known members to reset all tracking
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!restart"), handle_restart_command)
    )

    # !help handler for known members to show help menu
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"(?i)^!help"), handle_help_command)
    )

    # Catch-all handler to cache usernames from all messages in the monitored group
    # Registered in group 1 so it doesn't conflict with command handlers in group 0
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Chat(chat_id=MONITORED_GROUP_ID), _cache_message_user),
        group=1,
    )

    # Error handler for uncaught exceptions
    async def error_handler(update, context):
        logger.error("Unhandled exception: %s", context.error, exc_info=context.error)

    application.add_error_handler(error_handler)

    logger.info("-" * 40)
    logger.info("Elite Market Security Bot started")
    logger.info("Monitored group: %s", MONITORED_GROUP_ID)
    logger.info("Log channel: %s", LOG_CHANNEL_ID)
    logger.info("Security channel: %s", SECURITY_CHANNEL_ID)
    logger.info("Known members: %d static + %d dynamic", len(KNOWN_MEMBER_IDS) - len(_dynamic_known), len(_dynamic_known))
    logger.info("Tracked members: %d", len(_member_info))
    logger.info("-" * 40)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
