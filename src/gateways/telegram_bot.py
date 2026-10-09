import logging
import re
from typing import Any

import requests

from src.domain import Button, CallbackEvent, ChatEvent, CommandEvent, ReplyEvent, TextEvent
from src.interactions.callbacks import is_valid_callback_data
from src.observability.metrics import Metrics
from src.ports import ChannelDeliveryError

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 10
POLL_HTTP_TIMEOUT_MARGIN_SECONDS = 10

FOREIGN_CHAT = "foreign_chat"
UNAUTHORIZED_USER = "unauthorized_user"
MALFORMED = "malformed"

# Telegram fetches the first link of a message to build its preview: a link the bot relays
# from a mail would be requested without anyone clicking it.
_NO_LINK_PREVIEW = {"is_disabled": True}
# "/name", optionally "/name@bot" as Telegram writes it in groups, then free-form arguments.
_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)


class TelegramApiError(requests.RequestException):
    pass


class TelegramBot:
    def __init__(
        self,
        token: str,
        chat_id: str,
        http: requests.Session | None = None,
        poll_timeout: int = 30,
        allowed_user_ids: frozenset[int] = frozenset(),
    ) -> None:
        self._token = token
        self._chat_id = str(chat_id).strip()
        self._http = http or requests.Session()
        self._poll_timeout = poll_timeout
        self._allowed_user_ids = frozenset(allowed_user_ids) or _private_chat_user(self._chat_id)

    @property
    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    @property
    def inbound_authorized(self) -> bool:
        return bool(self._allowed_user_ids)

    def send_message(
        self,
        text: str,
        buttons: list[list[Button]] | None = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "link_preview_options": _NO_LINK_PREVIEW,
        }
        if silent:
            payload["disable_notification"] = True
        if buttons:
            payload["reply_markup"] = _inline_keyboard(buttons)
        if reply_to is not None:
            payload["reply_parameters"] = {"message_id": reply_to}
        message_id = self._call("sendMessage", payload, expect=dict).get("message_id")
        if not _is_int(message_id):
            raise TelegramApiError("Telegram sendMessage returned no message id")
        return message_id

    def edit_message(
        self, message_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": _NO_LINK_PREVIEW,
        }
        # Telegram drops the keyboard of an edited message unless it is sent again.
        if buttons:
            payload["reply_markup"] = _inline_keyboard(buttons)
        self._call("editMessageText", payload)

    def get_me(self) -> dict:
        return self._call("getMe", {}, expect=dict)

    def check_connection(self) -> str:
        return f"bot @{self.get_me()['username']} reachable"

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self._call("answerCallbackQuery", payload)

    def clear_buttons(self, message_id: int) -> None:
        self._call(
            "editMessageReplyMarkup",
            {
                "chat_id": self._chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )

    def get_updates(self, offset: int | None) -> tuple[list[ChatEvent], int | None]:
        payload: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        try:
            updates = self._call(
                "getUpdates",
                payload,
                timeout=self._poll_timeout + POLL_HTTP_TIMEOUT_MARGIN_SECONDS,
                expect=list,
            )
        except Exception as exc:
            Metrics.mark_telegram_poll_error()
            # The listener already warns with the retry delay; keep the sanitized detail at debug.
            logger.debug("Telegram polling failed: %s", _describe(exc))
            raise
        Metrics.mark_telegram_poll_success()
        events: list[ChatEvent] = []
        next_offset = offset
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not _is_int(update_id):
                _reject(MALFORMED)
                continue
            next_offset = max(next_offset or 0, update_id + 1)
            event = self._parse_update(update)
            if event is not None:
                events.append(event)
        return events, next_offset

    def _parse_update(self, update: dict) -> ChatEvent | None:
        if "callback_query" in update:
            return self._parse_callback(update["callback_query"])
        if "message" in update:
            return self._parse_message(update["message"])
        return _reject(MALFORMED)

    def _parse_callback(self, query: Any) -> CallbackEvent | None:
        if not isinstance(query, dict):
            return _reject(MALFORMED)
        message = query.get("message")
        if not self._is_authorized(message, query.get("from")):
            return None
        callback_id, data = query.get("id"), query.get("data")
        if not isinstance(callback_id, str) or not is_valid_callback_data(data):
            return _reject(MALFORMED)
        if not _is_int(message.get("message_id")):
            return _reject(MALFORMED)
        return CallbackEvent(callback_id=callback_id, message_id=message["message_id"], data=data)

    def _parse_message(self, message: Any) -> ReplyEvent | CommandEvent | TextEvent | None:
        if not isinstance(message, dict):
            return _reject(MALFORMED)
        # sender_chat marks a post made as the group or a channel (anonymous admin): "from" is
        # then a placeholder bot account, so no real user can be authorized.
        sender = None if "sender_chat" in message else message.get("from")
        if not self._is_authorized(message, sender):
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text or not _is_int(message.get("message_id")):
            return None
        replied = message.get("reply_to_message")
        # A reply is always mail text, even when it starts with a slash: it must never be
        # swallowed as a command.
        if isinstance(replied, dict):
            if not _is_int(replied.get("message_id")):
                return None
            return ReplyEvent(
                message_id=message["message_id"],
                reply_to_message_id=replied["message_id"],
                text=text,
            )
        command = _COMMAND_RE.match(text)
        if command is None:
            return TextEvent(message_id=message["message_id"], text=text)
        return CommandEvent(
            message_id=message["message_id"],
            name=command.group(1).lower(),
            args=(command.group(2) or "").strip(),
        )

    def _is_authorized(self, message: Any, sender: Any) -> bool:
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if chat_id is None or str(chat_id) != self._chat_id:
            _reject(FOREIGN_CHAT, chat_id)
            return False
        # Any member of a group can press buttons or reply: the chat alone proves nothing.
        user_id = sender.get("id") if isinstance(sender, dict) else None
        if not _is_int(user_id) or user_id not in self._allowed_user_ids:
            _reject(UNAUTHORIZED_USER, user_id)
            return False
        return True

    def _call(
        self,
        method: str,
        payload: dict,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        expect: type | tuple[type, ...] = object,
    ) -> Any:
        # The token is part of the URL and requests/urllib3 embed that URL in exception messages,
        # so every failure is re-raised as a sanitized error without the original chained.
        try:
            response = self._http.post(
                f"{API_BASE_URL}/bot{self._token}/{method}", json=payload, timeout=timeout
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise _sanitized_error(method, exc) from None
        if not isinstance(body, dict) or not body.get("ok"):
            raise TelegramApiError(f"Telegram {method} rejected (status {response.status_code})")
        result = body.get("result")
        if not isinstance(result, expect):
            raise TelegramApiError(f"Telegram {method} returned an unexpected result")
        return result


def _inline_keyboard(buttons: list[list[Button]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in buttons
        ]
    }


def _sanitized_error(method: str, exc: requests.RequestException) -> TelegramApiError:
    status = exc.response.status_code if exc.response is not None else "n/a"
    return TelegramApiError(
        f"Telegram {method} failed: {type(exc).__name__} (status {status})",
        response=exc.response,
    )


def _private_chat_user(chat_id: str) -> frozenset[int]:
    # A private chat's id is the user's id; group and channel ids are negative and need an
    # explicit allowlist.
    try:
        user_id = int(chat_id)
    except ValueError:
        return frozenset()
    return frozenset({user_id}) if user_id > 0 else frozenset()


def _reject(reason: str, detail: object = None) -> None:
    # Anyone can message the bot, so rejections stay at DEBUG to avoid log flooding; the counter
    # is the signal. Only ids are logged: foreign content is untrusted and may be personal data.
    logger.debug("Ignoring Telegram update (%s): %s", reason, detail)
    Metrics.mark_telegram_rejected(reason)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _describe(exc: Exception) -> str:
    if isinstance(exc, TelegramApiError):
        return str(exc)
    return type(exc).__name__


class TelegramChannel:
    name = "telegram"
    interactive = True

    def __init__(self, bot: TelegramBot) -> None:
        self._bot = bot

    @property
    def is_configured(self) -> bool:
        return self._bot.is_configured

    def check_connection(self) -> str:
        return self._bot.check_connection()

    def send(self, text: str, buttons: list[list[Button]] | None = None) -> int:
        try:
            return self._bot.send_message(text, buttons=buttons)
        except TelegramApiError as exc:
            raise ChannelDeliveryError(str(exc)) from None

    def update(
        self, message_id: int, text: str, buttons: list[list[Button]] | None = None
    ) -> None:
        try:
            self._bot.edit_message(message_id, text, buttons=buttons)
        except TelegramApiError as exc:
            raise ChannelDeliveryError(str(exc)) from None
