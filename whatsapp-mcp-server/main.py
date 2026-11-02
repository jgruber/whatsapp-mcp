import hmac
import os
import signal
import sys
import tempfile
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

import media_preview
import transcription
import uploads


def _build_transport_security() -> TransportSecuritySettings:
    """Extend FastMCP's localhost-only default allow-list with the hosts the
    public reverse proxy forwards to us.

    FastMCP binds the DNS-rebinding protection to the Host header the backend
    sees. When Traefik/nginx preserves the public hostname (the default), it
    reaches us as something like "whats.grubernet.org", which is not in the
    stock allow-list. Add the public FQDNs via WHATSAPP_MCP_ALLOWED_HOSTS
    (comma-separated) rather than disabling the check.
    """
    extra = [h.strip() for h in os.getenv("WHATSAPP_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *extra],
        allowed_origins=[
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
            *(f"https://{h.split(':')[0]}" for h in extra),
            *(f"http://{h.split(':')[0]}" for h in extra),
        ],
    )
from mcp_config import resolve_host, resolve_port, resolve_transport
from parent_watchdog import install_stdio_parent_watchdog
from whatsapp import (
    MESSAGES_DB_PATH,
    msg_to_dict,
)
from whatsapp import (
    download_media as whatsapp_download_media,
)
from whatsapp import (
    get_chat as whatsapp_get_chat,
)
from whatsapp import (
    get_contact_chats as whatsapp_get_contact_chats,
)
from whatsapp import (
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
)
from whatsapp import (
    get_last_interaction as whatsapp_get_last_interaction,
)
from whatsapp import (
    get_message_context as whatsapp_get_message_context,
)
from whatsapp import (
    get_sender_name as whatsapp_get_sender_name,
)
from whatsapp import (
    list_chats as whatsapp_list_chats,
)
from whatsapp import (
    list_messages as whatsapp_list_messages,
)
from whatsapp import (
    mark_messages_read as whatsapp_mark_messages_read,
)
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
)
from whatsapp import (
    send_audio_message as whatsapp_audio_voice_message,
)
from whatsapp import (
    send_file as whatsapp_send_file,
)
from whatsapp import (
    send_message as whatsapp_send_message,
)
from whatsapp import (
    send_reaction as whatsapp_send_reaction,
)
from whatsapp import (
    list_groups as whatsapp_list_groups,
    get_group_info as whatsapp_get_group_info,
    create_group as whatsapp_create_group,
    update_group_participants as whatsapp_update_group_participants,
    get_group_invite_link as whatsapp_get_group_invite_link,
)

# Initialize FastMCP server. Env-var handling is deferred to the __main__ block
# so importing this module never parses env vars or exits the process. The one
# exception is WHATSAPP_MCP_ALLOWED_HOSTS, which must be bound at construction
# time because FastMCP wires transport_security into streamable_http_app().
mcp = FastMCP("whatsapp", transport_security=_build_transport_security())

PreviewDimension = Annotated[
    int,
    Field(
        strict=True,
        ge=media_preview.MIN_MAX_DIMENSION,
        le=media_preview.MAX_MAX_DIMENSION,
        description="Longest preview edge in pixels; must be an integer from 1 to 2048.",
    ),
]


@mcp.tool()
def search_contacts(query: str) -> list[dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number.

    Args:
        query: Search term to match against contact names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return contacts


@mcp.tool()
def get_contact(
    identifier: str | None = None,
    phone_number: str | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    """Look up a WhatsApp contact by phone number, LID, or full JID.

    Automatically detects the identifier type and queries appropriately.

    Args:
        identifier: Phone number, LID, or full JID. Examples:
                    - "12025551234" (phone number)
                    - "35047067385985" (LID - numeric)
                    - "12025551234@s.whatsapp.net" (phone JID)
                    - "184125298348272@lid" (LID JID)
        phone_number: Backward-compatible alias for `identifier`.
        phone: Backward-compatible alias for `identifier` (matches README parameter name).

    Returns:
        Dictionary with jid, name, display_name, is_lid, and resolved status
    """
    if identifier is None:
        identifier = phone_number
    if identifier is None:
        identifier = phone
    if identifier is None:
        raise ValueError("Missing required argument: identifier (or phone_number / phone)")

    identifier = identifier.strip()
    if not identifier:
        raise ValueError("identifier must be non-empty")

    # Detect identifier type and normalize to JID.
    bare_numeric_digits: str | None = None
    if "@" in identifier:
        # Already a JID - use as-is
        jid = identifier
        is_lid = jid.endswith("@lid") or jid.split("@", 1)[-1] == "lid"
    else:
        digits = "".join(c for c in identifier if c.isdigit())
        if digits:
            # LIDs can overlap phone-number lengths, so bare numeric inputs try phone first.
            jid = f"{digits}@s.whatsapp.net"
            is_lid = False
            if identifier.isdigit():
                bare_numeric_digits = digits
        else:
            # Non-numeric and not a JID; try as-is.
            jid = identifier
            is_lid = False

    jid_user = jid.split("@", 1)[0]

    display_name: str | None = None
    resolved = False

    # Prefer chats table lookup via get_chat (works for both phone and LID contacts).
    candidates: list[tuple[str, bool]] = [(jid, is_lid)]
    if bare_numeric_digits:
        candidates.append((f"{bare_numeric_digits}@lid", True))

    chat = None
    for candidate_jid, candidate_is_lid in candidates:
        chat = whatsapp_get_chat(candidate_jid, include_last_message=False)
        if chat:
            jid = candidate_jid
            is_lid = candidate_is_lid
            jid_user = jid.split("@", 1)[0]
            break

    if chat and chat.get("name"):
        display_name = chat["name"]
        resolved = display_name not in (jid, jid_user)
    else:
        # Fallback: best-effort sender-name resolution (may use fuzzy LIKE lookup).
        display_name = whatsapp_get_sender_name(jid)
        resolved = display_name not in (jid, jid_user, identifier)

    return {
        "identifier": identifier,
        "jid": jid,
        "phone_number": jid_user if not is_lid else None,
        "lid": jid_user if is_lid else None,
        "name": display_name if resolved else jid_user,
        "display_name": display_name,
        "is_lid": is_lid,
        "resolved": resolved,
    }


@mcp.tool()
def list_messages(
    after: str | None = None,
    before: str | None = None,
    sender_phone_number: str | None = None,
    chat_jid: str | None = None,
    query: str | None = None,
    limit: int = 50,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    sort_by: str = "newest",
) -> list[dict[str, Any]]:
    """Get WhatsApp messages matching specified criteria with optional context.

    Each message includes sender_display showing "Name (phone)" for easy identification.

    Args:
        after: ISO-8601 date string (e.g., "2026-01-01" or "2026-01-01T09:00:00")
        before: ISO-8601 date string (e.g., "2026-01-09" or "2026-01-09T18:00:00")
        sender_phone_number: Phone number to filter by sender (e.g., "12025551234")
        chat_jid: Chat JID to filter by (e.g., "12025551234@s.whatsapp.net" or group JID)
        query: Search term to filter messages by content
        limit: Max messages to return (default 50, max 500)
        page: Page number for pagination (default 0)
        include_context: Include surrounding messages for context (default True)
        context_before: Messages to include before each match (default 1)
        context_after: Messages to include after each match (default 1)
        sort_by: "newest" (default, most recent first) or "oldest" (chronological)
    """
    # Cap limit at 500 to prevent excessive queries
    limit = max(1, min(limit, 500))
    page = max(0, page)
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after,
        sort_by=sort_by,
    )
    return messages


@mcp.tool()
def list_chats(
    query: str | None = None,
    limit: int = 50,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
) -> list[dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.

    Args:
        query: Search term to filter chats by name or JID
        limit: Max chats to return (default 50, max 200)
        page: Page number for pagination (default 0)
        include_last_message: Include the last message in each chat (default True)
        sort_by: "last_active" (default, most recent first) or "name" (alphabetical)

    Returns:
        Chat dictionaries with jid, name, is_group, last_message_time, last_message,
        last_sender, last_is_from_me, last_read_time and unread. `last_read_time` is
        how far the chat has been read on any device (null if never reported); `unread`
        is true when the last message is inbound and newer than that marker, so chats
        already read on the phone are not reported as unread. Numeric fallback names
        are resolved from local contacts when possible, even while the bridge is offline.
    """
    # Cap limit at 200 to prevent excessive queries
    limit = max(1, min(limit, 200))
    page = max(0, page)
    chats = whatsapp_list_chats(
        query=query, limit=limit, page=page, include_last_message=include_last_message, sort_by=sort_by
    )
    return chats


@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> dict[str, Any]:
    """Get WhatsApp chat metadata by JID.

    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)

    Returns:
        Chat dictionary — same shape as list_chats, including last_read_time and unread.
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return chat


@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.

    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return chat


@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> list[dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    limit = max(1, min(limit, 200))
    page = max(0, page)
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return chats


@mcp.tool()
def get_last_interaction(jid: str) -> dict[str, Any]:
    """Get most recent WhatsApp message involving the contact.

    Args:
        jid: The JID of the contact to search for

    Returns:
        Message dictionary with id, timestamp, sender, content, etc. or empty dict if not found.
    """
    message = whatsapp_get_last_interaction(jid)
    return message if message else {}


@mcp.tool()
def get_message_context(message_id: str, before: int = 5, after: int = 5) -> dict[str, Any]:
    """Get context around a specific WhatsApp message.

    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    context = whatsapp_get_message_context(message_id, before, after)
    return {
        "message": msg_to_dict(context.message),
        "before": [msg_to_dict(message) for message in context.before],
        "after": [msg_to_dict(message) for message in context.after],
    }


@mcp.tool()
def send_message(
    recipient: str,
    message: str,
    quoted_message_id: str = "",
    quoted_sender_jid: str = "",
    quoted_content: str = "",
    mentions: list[str] | None = None,
) -> dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send
        quoted_message_id: ID of the message to reply to (optional). When set, the sent
                           message will appear as a quoted reply in WhatsApp.
        quoted_sender_jid: Full JID of the author of the quoted message. Required for
                           group replies so WhatsApp renders the correct attribution.
        quoted_content: Text content of the quoted message, used for the reply preview.
                        Only plain text is supported; media previews are not included.
        mentions: Users to @-mention, as phone numbers with country code but no + (e.g.
                  ["420601234567"]) or JIDs. For each entry the message text must contain
                  a matching "@<number>" token (e.g. "hi @420601234567"), otherwise the
                  mention won't render on recipients' devices. Only meaningful in groups.

    Returns:
        A dictionary containing success status and a status message
    """
    # Validate input
    if not recipient:
        return {"success": False, "message": "Recipient must be provided"}

    # Call the whatsapp_send_message function with the unified recipient parameter
    success, status_message = whatsapp_send_message(
        recipient, message, quoted_message_id, quoted_sender_jid, quoted_content, mentions
    )
    return {"success": success, "message": status_message}


@mcp.tool()
def send_reaction(
    recipient: str,
    message_id: str,
    emoji: str,
    from_me: bool = False,
    sender_jid: str = "",
) -> dict[str, Any]:
    """Send (or remove) a reaction to a WhatsApp message.

    Args:
        recipient: The chat JID the message belongs to (e.g., "12025551234@s.whatsapp.net"
                   or a group JID like "123456789@g.us")
        message_id: The ID of the message to react to
        emoji: The reaction emoji (e.g., "👍"). Pass an empty string to remove the reaction.
        from_me: Whether the original message was sent by the current user (default False)
        sender_jid: JID of the original message sender — required for group messages when
                    from_me is False so the bridge can build the correct WhatsApp key

    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_send_reaction(recipient, message_id, emoji, from_me, sender_jid)
    return {"success": success, "message": status_message}


@mcp.tool()
def mark_messages_read(
    message_ids: list[str],
    chat_jid: str,
    sender_jid: str = "",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Mark selected WhatsApp messages as read and send read receipts.

    This is an explicit external side effect. All message IDs must belong to the
    same chat and sender.

    Args:
        message_ids: IDs of the messages to mark as read
        chat_jid: JID of the chat containing the messages
        sender_jid: JID or bare phone number of the original sender; required for groups
        timestamp: Optional RFC 3339 read timestamp; defaults to the current time

    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_mark_messages_read(message_ids, chat_jid, sender_jid, timestamp)
    return {"success": success, "message": status_message}


def _attachment_path(media_path: str | None, upload_id: str | None) -> tuple[str | None, str | None]:
    """Return (path, error) for the attachment to send. Exactly one of
    media_path or upload_id must be supplied; the upload is resolved to a
    path on the shared store volume."""
    provided = [v for v in (media_path, upload_id) if v]
    if len(provided) != 1:
        return None, "pass exactly one of media_path or upload_id"
    if upload_id:
        path = uploads.resolve(upload_id)
        if not path:
            return None, f"unknown or expired upload_id {upload_id!r}"
        return path, None
    return media_path, None


@mcp.tool()
def send_file(
    recipient: str,
    media_path: str | None = None,
    upload_id: str | None = None,
    caption: str = "",
) -> dict[str, Any]:
    """Send a file (image, video, document) via WhatsApp, optionally with a caption.

    When `caption` is provided, the file and text arrive as a single
    attachment-with-caption message (one bubble in the WA UI), instead of
    needing a separate follow-up send_message call. For group chats use the JID.

    Args:
        recipient: Either a phone number with country code (no + or symbols),
                 or a JID (e.g., "123456789@s.whatsapp.net" or "123456789@g.us")
        media_path: Absolute path to the media file (image, video, document).
                 Pass this OR upload_id, not both.
        upload_id: Id of a file previously posted to POST /upload. Resolved to
                 a path on the shared store volume. Pass this OR media_path,
                 not both.
        caption: Optional text rendered with the file as a caption. Omit for
                 a bare attachment.

    Returns:
        A dictionary containing success status and a status message
    """
    path, err = _attachment_path(media_path, upload_id)
    if err:
        return {"success": False, "message": err}
    success, status_message = whatsapp_send_file(recipient, path, caption)
    if upload_id and success:
        uploads.delete(upload_id)
    return {"success": success, "message": status_message}


@mcp.tool()
def send_audio_message(
    recipient: str,
    media_path: str | None = None,
    upload_id: str | None = None,
) -> dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file).
                 Pass this OR upload_id, not both.
        upload_id: Id of an audio file previously posted to POST /upload. Pass this OR media_path, not both.

    Returns:
        A dictionary containing success status and a status message
    """
    path, err = _attachment_path(media_path, upload_id)
    if err:
        return {"success": False, "message": err}
    success, status_message = whatsapp_audio_voice_message(recipient, path)
    if upload_id and success:
        uploads.delete(upload_id)
    return {"success": success, "message": status_message}


@mcp.tool()
def list_groups() -> dict[str, Any]:
    """List every WhatsApp group this account is a member of.

    Returns:
        {"groups": [{"jid", "name", "topic", "owner_jid", "participant_count",
                     "created_at", "is_announce", "is_locked", "is_ephemeral"}, ...]}
        or {"error": "..."} if the bridge reports a failure.
    """
    return whatsapp_list_groups()


@mcp.tool()
def get_group_info(group_jid: str) -> dict[str, Any]:
    """Get a group's full metadata, including every participant and their admin role.

    Args:
        group_jid: The group's JID. Either "<id>@g.us" or just "<id>".

    Returns:
        A dict with the group's name/topic/owner/created_at plus a
        "participants" list where each entry has {"jid", "phone_number",
        "display_name", "is_admin", "is_super_admin"}. On failure the dict
        contains an "error" key instead.
    """
    return whatsapp_get_group_info(group_jid)


@mcp.tool()
def create_group(name: str, participants: list[str]) -> dict[str, Any]:
    """Create a new WhatsApp group with the given name and initial participants.

    The authenticated account is implicitly added as the group owner.

    Args:
        name: Visible group name (1–100 chars; WhatsApp trims very long names).
        participants: List of phone numbers (digits only, with country code,
            no + or symbols) OR full JIDs ending in "@s.whatsapp.net".
            At least one participant other than yourself is required.

    Returns:
        The new group's info (same shape as get_group_info), or {"error": ...}.
    """
    return whatsapp_create_group(name, participants)


@mcp.tool()
def update_group_participants(
    group_jid: str, action: str, participants: list[str]
) -> dict[str, Any]:
    """Add, remove, promote, or demote participants in a group you administer.

    Args:
        group_jid: The group's JID. Either "<id>@g.us" or just "<id>".
        action: One of "add", "remove", "promote", "demote".
        participants: List of phone numbers or JIDs to act on. Phone numbers
            must be digits-only with country code (no + or symbols).

    Returns:
        {"action": "...", "results": [{"jid": "...", "status": <int>}, ...]}
        where status is 0 on success and a non-zero WhatsApp code on failure
        (e.g. 403 for "already a participant"). On transport failure the dict
        contains an "error" key.
    """
    return whatsapp_update_group_participants(group_jid, action, participants)


@mcp.tool()
def get_group_invite_link(group_jid: str, reset: bool = False) -> dict[str, Any]:
    """Return the shareable https://chat.whatsapp.com/<code> link for a group.

    The account must be an admin of the group. Setting reset=True revokes
    the previous link and generates a fresh one — anyone holding the old
    link can no longer use it.

    Args:
        group_jid: The group's JID. Either "<id>@g.us" or just "<id>".
        reset: Revoke the current link and issue a new one.

    Returns:
        {"invite_link": "https://chat.whatsapp.com/..."} or {"error": "..."}.
    """
    return whatsapp_get_group_invite_link(group_jid, reset)


@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(message_id, chat_jid)

    if file_path:
        return {"success": True, "message": "Media downloaded successfully", "file_path": file_path}
    else:
        return {"success": False, "message": "Failed to download media"}


@mcp.tool()
def view_media(
    message_id: str,
    chat_jid: str,
    max_dimension: PreviewDimension = media_preview.DEFAULT_MAX_DIMENSION,
) -> Any:
    """View the media of a WhatsApp message as an image.

    download_media only returns a local file path, which a client without
    filesystem access cannot open. This returns the picture itself instead.
    Videos return their first frame, which is enough to tell what was sent.
    Both are downscaled so a single photo cannot flood the context. Voice notes
    are not images — use transcribe_audio or read the transcript from the
    message content with list_messages.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
        max_dimension: Longest edge of the returned image in pixels (default 1024)

    Returns:
        Image content on success, otherwise a dictionary explaining why not
    """
    try:
        media_preview.validate_max_dimension(max_dimension)
    except media_preview.PreviewError as exc:
        return {"success": False, "message": str(exc)}

    file_path = whatsapp_download_media(message_id, chat_jid)
    if not file_path:
        return {"success": False, "message": "Failed to download media"}

    with tempfile.TemporaryDirectory() as work_dir:
        try:
            data, image_format = media_preview.render_preview(file_path, max_dimension=max_dimension, work_dir=work_dir)
        except media_preview.PreviewError as exc:
            return {"success": False, "message": str(exc), "file_path": file_path}

    return Image(data=data, format=image_format)


@mcp.tool()
def transcribe_audio(message_id: str, chat_jid: str, force: bool = False) -> dict[str, Any]:
    """Transcribe a WhatsApp voice note and return its text.

    Runs whisper.cpp locally by default, or sends audio to the operator's
    configured OpenAI-compatible endpoint. The transcript is also written into
    the message's empty content field, so afterwards it is readable through list_messages by any
    client — including one with no filesystem access — without transcribing
    again.

    Call this for a voice note whose content field is still empty. Requires
    whisper.cpp, FFmpeg, and WHISPER_MODEL for the default provider; alternatively
    configure WHATSAPP_TRANSCRIPTION_PROVIDER=openai_compatible, URL and MODEL.

    Args:
        message_id: The ID of the message containing the voice note
        chat_jid: The JID of the chat containing the message
        force: Transcribe again even when a transcript is already stored

    Returns:
        A dictionary with success status and the transcript
    """
    if not force:
        existing = transcription.stored_transcript(MESSAGES_DB_PATH, message_id, chat_jid)
        if existing:
            return {"success": True, "message": "Transcript already stored", "transcript": existing}

    # WhatsApp expires media server-side after a few weeks, so the file has to
    # be on disk. This is a no-op when the bridge already downloaded it.
    file_path = whatsapp_download_media(message_id, chat_jid)
    if not file_path:
        return {
            "success": False,
            "message": (
                "Could not obtain the audio file. WhatsApp expires media after a while, "
                "so an old voice note may no longer be downloadable."
            ),
        }
    if not transcription.is_audio(file_path):
        return {
            "success": False,
            "message": "This message is not audio. Use download_media instead.",
            "file_path": file_path,
        }

    try:
        provider = transcription.provider_name()
        model = transcription.configured_model(provider)
        with tempfile.TemporaryDirectory() as work_dir:
            text = transcription.transcribe_file(file_path, work_dir, model=model, provider=provider)
    except transcription.TranscriptionError as exc:
        return {"success": False, "message": str(exc)}

    if not transcription.store_transcript(MESSAGES_DB_PATH, message_id, chat_jid, text, model, provider=provider):
        # The words are worth returning even when the row could not be updated,
        # but say so: without the row, list_messages will not show them.
        return {
            "success": True,
            "message": "Transcribed, but the transcript could not be stored on the message row",
            "transcript": text,
        }

    return {
        "success": True,
        "message": "Transcribed",
        "transcript": f"{transcription.label(model, provider)}{text}",
    }


class _UploadTooLarge(Exception):
    pass


@mcp.custom_route("/upload", methods=["POST"])
async def upload_route(request: Request) -> JSONResponse:
    """Accept a raw file body and stash it on the shared store volume.

    Body: the raw file bytes. Query: filename=<name with extension>.
    Auth: the same bearer token as /mcp (enforced by BearerTokenAuth).
    """
    uploads.sweep()
    filename = request.query_params.get("filename") or request.headers.get("x-filename")
    if not filename:
        return JSONResponse({"error": "filename query parameter is required"}, status_code=400)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > uploads.MAX_UPLOAD_BYTES:
        return JSONResponse({"error": f"file exceeds {uploads.MAX_UPLOAD_BYTES} bytes"}, status_code=413)

    upload_id, path = uploads.new_upload(filename)
    size = 0
    try:
        with open(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > uploads.MAX_UPLOAD_BYTES:
                    raise _UploadTooLarge
                f.write(chunk)
    except _UploadTooLarge:
        uploads.delete(upload_id)
        return JSONResponse({"error": f"file exceeds {uploads.MAX_UPLOAD_BYTES} bytes"}, status_code=413)
    except BaseException:
        uploads.delete(upload_id)
        raise
    if size == 0:
        uploads.delete(upload_id)
        return JSONResponse({"error": "empty body; send the file bytes as the request body"}, status_code=400)

    return JSONResponse(
        {
            "upload_id": upload_id,
            "filename": os.path.basename(path),
            "size": size,
            "expires_in": uploads.UPLOAD_TTL_SECONDS,
        },
        status_code=201,
    )


class BearerTokenAuth:
    """ASGI middleware that rejects HTTP requests without the expected bearer token.

    /healthz is allowed through unauthenticated so container probes don't
    need the secret.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/healthz":
                await self._respond(send, 200, b"ok")
                return
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                await self._respond(send, 401, b"Unauthorized")
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _respond(send, status: int, body: bytes):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": body})


def _run_http_with_auth() -> None:
    """Run the streamable-http MCP app behind bearer-token auth with uvicorn."""
    import uvicorn

    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_AUTH_TOKEN must be set to a random string of at least 32 characters")
    app = BearerTokenAuth(mcp.streamable_http_app(), token)
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)


def shutdown_handler(signum, frame):
    """Handle shutdown signals gracefully to prevent zombie processes."""
    sys.exit(0)


if __name__ == "__main__":
    # Capture before any await — os.getppid() is dynamic.
    parent_pid = os.getppid()
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Resolve the transport first: host/port are only used (and validated) for the
    # network transports, so a bad WHATSAPP_MCP_PORT can't break a stdio launch.
    # The localhost default keeps a remote server unreachable until explicitly opened up.
    try:
        transport = resolve_transport(os.getenv("WHATSAPP_MCP_TRANSPORT"))
        if transport != "stdio":
            mcp.settings.host = resolve_host(os.getenv("WHATSAPP_MCP_HOST"))
            mcp.settings.port = resolve_port(os.getenv("WHATSAPP_MCP_PORT"))
            # stdout is reserved for the protocol on stdio; log startup to stderr.
            print(
                f"WhatsApp MCP server listening on {mcp.settings.host}:{mcp.settings.port} via {transport}",
                file=sys.stderr,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    if transport == "stdio":
        install_stdio_parent_watchdog("WHATSAPP_PARENT_WATCHDOG_S", parent_pid=parent_pid)
    if transport == "streamable-http":
        # Wrap FastMCP's streamable-http app in BearerTokenAuth and serve
        # /upload + /healthz alongside /mcp from the same uvicorn process.
        _run_http_with_auth()
    else:
        mcp.run(transport=transport)
