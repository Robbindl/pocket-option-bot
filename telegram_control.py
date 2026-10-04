import asyncio
import datetime as dt
import json
import os
import ssl
import sys
from collections import deque
from html import escape
from pathlib import Path
from typing import Any
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request

from logger import setup_logger

logger = setup_logger("telegram_control", log_file=str(Path(__file__).resolve().parent / "mirror_trade.log"))


class MirrorControlState:
    """Tracks whether the mirror loop should ignore new master trade events."""

    def __init__(self) -> None:
        self._paused = asyncio.Event()
        self.master_changed = asyncio.Event()
        self.accounts: list[Any] = []
        self.master_executor: Any | None = None
        self.enabled_children: set[str] = set()
        self.trade_history: deque[dict[str, Any]] = deque(maxlen=10)
        self.inflight_trade_count = 0

    def configure_accounts(self, master_executor: Any, child_executors: list[Any]) -> None:
        self.accounts = [master_executor, *child_executors]
        self.master_executor = master_executor
        self.enabled_children = {executor.label for executor in child_executors}

    def get_account(self, label: str) -> Any | None:
        return next((account for account in self.accounts if account.label == label), None)

    def child_executors(self) -> list[Any]:
        return [
            account
            for account in self.accounts
            if account is not self.master_executor and account.label in self.enabled_children
        ]

    def select_master(self, label: str) -> bool:
        if not self.is_paused() or self.inflight_trade_count:
            return False
        selected = self.get_account(label)
        if (
            selected is None
            or selected is self.master_executor
            or getattr(selected, "connection_status", "connected") != "connected"
        ):
            return False

        previous_master = self.master_executor
        self.master_executor = selected
        if previous_master is not None:
            self.enabled_children.add(previous_master.label)
        self.enabled_children.discard(selected.label)
        selected.discard_opened_trade_events()
        self.master_changed.set()
        return True

    def begin_trade(self) -> None:
        self.inflight_trade_count += 1

    def end_trade(self) -> None:
        self.inflight_trade_count = max(0, self.inflight_trade_count - 1)

    def toggle_child(self, label: str) -> bool | None:
        account = self.get_account(label)
        if account is None or account is self.master_executor:
            return None
        if label in self.enabled_children:
            self.enabled_children.remove(label)
            return False
        self.enabled_children.add(label)
        return True

    def record_trade(self, trade_event: dict[str, Any], status: str) -> None:
        trade_id = str(trade_event.get("trade_id") or "")
        record = next(
            (item for item in self.trade_history if trade_id and item.get("trade_id") == trade_id),
            None,
        )
        if record is None:
            record = {
                "trade_id": trade_id,
                "asset": trade_event.get("asset", "?"),
                "action": trade_event.get("action", "?"),
                "amount": trade_event.get("amount"),
                "duration": trade_event.get("duration"),
                "time": dt.datetime.now().strftime("%H:%M:%S"),
            }
            self.trade_history.appendleft(record)
        record["status"] = status

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def should_process_trade(self) -> bool:
        return not self.is_paused()

    def status(self) -> dict[str, Any]:
        return {"paused": self.is_paused(), "state": "paused" if self.is_paused() else "running"}


class TelegramMirrorController:
    """Small Telegram control interface for pausing or resuming the mirror loop."""

    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: str | int,
        control_state: MirrorControlState | None = None,
        *,
        poll_interval: float = 2.0,
        allow_insecure_ssl: bool | None = None,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.allowed_chat_id = str(allowed_chat_id).strip() if allowed_chat_id is not None else ""
        self.control_state = control_state or MirrorControlState()
        self.poll_interval = float(poll_interval)
        self.allow_insecure_ssl = bool(allow_insecure_ssl) if allow_insecure_ssl is not None else False
        self._offset = 0
        self._stop_event = asyncio.Event()
        self._keyboard_view = "home"

    @staticmethod
    def parse_command(raw_text: str | None) -> str:
        text = (raw_text or "").strip()
        if not text:
            return "unknown"
        command = text.split()[0].lower()
        if command.startswith("master_select:"):
            return "master_select"
        if command.startswith("child_toggle:"):
            return "child_toggle"
        if command in {"/status", "status"}:
            return "status"
        if command in {"/pause", "pause"}:
            return "pause"
        if command in {"/resume", "resume"}:
            return "resume"
        if command in {"/balance", "balance"}:
            return "balance"
        if command in {"/children", "children"}:
            return "children"
        if command in {"/master", "master"}:
            return "master"
        if command in {"/recent", "recent"}:
            return "recent"
        if command in {"/help", "help", "/start", "start"}:
            return "help"
        return "unknown"

    def _message_is_allowed(self, update: dict[str, Any]) -> bool:
        callback_query = update.get("callback_query") or {}
        message = update.get("message") or callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        return str(chat_id) == self.allowed_chat_id

    def _account_title(self, account: Any) -> str:
        if account is self.control_state.master_executor:
            return "Master"
        if account.label == "master":
            return "Former master"
        suffix = account.label.removeprefix("child-")
        return f"Child {suffix}"

    def _status_message(self) -> str:
        state = "⏸ Paused" if self.control_state.is_paused() else "🟢 Running"
        master = self.control_state.master_executor
        master_name = self._account_title(master) if master is not None else "not selected"
        lines = [f"<b>Mirror: {state}</b>", f"Source: {escape(master_name)}", "<b>Accounts</b>"]
        for account in self.control_state.accounts:
            role = "SOURCE" if account is master else (
                "CHILD ON" if account.label in self.control_state.enabled_children else "CHILD OFF"
            )
            balance = getattr(account, "_balance", None)
            balance_text = escape(str(balance)) if balance is not None else "not available"
            lines.append(
                f"• {escape(self._account_title(account))}: {role} · "
                f"{escape(account.connection_status)} · balance {balance_text}"
            )
        return "\n".join(lines)

    def _balance_message(self) -> str:
        lines = ["<b>Account balances</b>"]
        for account in self.control_state.accounts:
            balance = getattr(account, "_balance", None)
            value = escape(str(balance)) if balance is not None else "not available"
            updated = getattr(account, "_balance_updated_at", None)
            updated_text = f" · {updated:%H:%M:%S}" if updated is not None else ""
            lines.append(f"• {escape(self._account_title(account))}: {value}{updated_text}")
        return "\n".join(lines)

    def _children_message(self) -> str:
        lines = ["<b>Child accounts</b>"]
        for account in self.control_state.accounts:
            if account is self.control_state.master_executor:
                continue
            enabled = account.label in self.control_state.enabled_children
            status = "ON" if enabled else "OFF"
            lines.append(f"• {escape(self._account_title(account))}: {status} · {escape(account.connection_status)}")
        if len(lines) == 1:
            lines.append("No child accounts are configured.")
        return "\n".join(lines)

    def _master_message(self) -> str:
        if not self.control_state.is_paused():
            return "<b>Choose master</b>\nPause mirroring before changing the source account."
        if self.control_state.inflight_trade_count:
            return "<b>Choose master</b>\nWait for current child copies to finish before switching."
        lines = ["<b>Choose master</b>", "The previous master will be enabled as a child."]
        lines.extend(
            f"• {escape(self._account_title(account))} · {escape(account.connection_status)}"
            for account in self.control_state.accounts
            if account is not self.control_state.master_executor
        )
        return "\n".join(lines)

    def _recent_message(self) -> str:
        if not self.control_state.trade_history:
            return "<b>Recent trades</b>\nNo trades detected yet."
        lines = ["<b>Recent trades</b>"]
        for trade in self.control_state.trade_history:
            amount = trade.get("amount")
            amount_text = f" · {escape(str(amount))}" if amount is not None else ""
            lines.append(
                f"• {escape(str(trade.get('time', '')))} {escape(str(trade.get('asset', '?')))} "
                f"{escape(str(trade.get('action', '?')))}{amount_text} · {escape(str(trade.get('status', 'detected')))}"
            )
        return "\n".join(lines)

    def _keyboard_markup(self) -> dict[str, Any]:
        toggle_button = (
            {"text": "▶️ Resume", "callback_data": "resume"}
            if self.control_state.is_paused()
            else {"text": "⏸ Pause", "callback_data": "pause"}
        )
        if self._keyboard_view == "master":
            if self.control_state.is_paused() and not self.control_state.inflight_trade_count:
                rows = [
                    [{"text": self._account_title(account), "callback_data": f"master_select:{account.label}"}]
                    for account in self.control_state.accounts
                    if account is not self.control_state.master_executor
                ]
            else:
                label = "Wait for copies" if self.control_state.inflight_trade_count else "⏸ Pause first"
                rows = [[{"text": "⏸ Pause first", "callback_data": "pause"}]]
                if self.control_state.inflight_trade_count:
                    rows = [[{"text": label, "callback_data": "status"}]]
            rows.append([{"text": "↩️ Back", "callback_data": "status"}])
            return {"inline_keyboard": rows}
        if self._keyboard_view == "children":
            rows = []
            for account in self.control_state.accounts:
                if account is self.control_state.master_executor:
                    continue
                enabled = account.label in self.control_state.enabled_children
                marker = "🟢" if enabled else "⚪"
                rows.append([
                    {
                        "text": f"{marker} {self._account_title(account)}: {'ON' if enabled else 'OFF'}",
                        "callback_data": f"child_toggle:{account.label}",
                    }
                ])
            rows.append([{"text": "↩️ Back", "callback_data": "status"}])
            return {"inline_keyboard": rows}
        return {
            "inline_keyboard": [
                [
                    {"text": "📊 Status", "callback_data": "status"},
                    toggle_button,
                ],
                [
                    {"text": "💰 Balance", "callback_data": "balance"},
                    {"text": "👥 Children", "callback_data": "children"},
                ],
                [
                    {"text": "🎯 Master", "callback_data": "master"},
                    {"text": "🕘 Recent", "callback_data": "recent"},
                ],
                [{"text": "❔ Help", "callback_data": "help"}],
            ]
        }

    def _handle_command(self, update: dict[str, Any]) -> str | None:
        if not self._message_is_allowed(update):
            return None

        message = update.get("message") or {}
        callback_query = update.get("callback_query") or {}
        command_text = callback_query.get("data") or message.get("text") or ""
        command = self.parse_command(command_text)
        callback_argument = command_text.split(":", 1)[1] if ":" in command_text else ""
        if command == "status":
            self._keyboard_view = "home"
            return self._status_message()
        if command == "pause":
            self.control_state.pause()
            self._keyboard_view = "home"
            return self._status_message()
        if command == "resume":
            self.control_state.resume()
            self._keyboard_view = "home"
            return self._status_message()
        if command == "balance":
            self._keyboard_view = "home"
            return self._balance_message()
        if command == "children":
            self._keyboard_view = "children"
            return self._children_message()
        if command == "child_toggle":
            enabled = self.control_state.toggle_child(callback_argument)
            self._keyboard_view = "children"
            if enabled is None:
                return "That account cannot be changed as a child."
            return self._children_message()
        if command == "master":
            self._keyboard_view = "master"
            return self._master_message()
        if command == "master_select":
            selected_account = self.control_state.get_account(callback_argument)
            if selected_account is None:
                self._keyboard_view = "master"
                return "That account is not available."
            if selected_account.connection_status != "connected":
                self._keyboard_view = "master"
                return f"{escape(self._account_title(selected_account))} is not connected and cannot be selected."
            if not self.control_state.select_master(callback_argument):
                self._keyboard_view = "master"
                return self._master_message()
            self._keyboard_view = "home"
            master = self.control_state.master_executor
            return f"<b>New master: {escape(self._account_title(master))}</b>\nFormer master enabled as a child.\n{self._status_message()}"
        if command == "recent":
            self._keyboard_view = "home"
            return self._recent_message()
        if command == "help":
            self._keyboard_view = "home"
            return (
                "<b>Mirror controls</b>\n"
                "/status · /pause · /resume\n"
                "/master · /children · /recent · /balance\n"
                "/help · /start"
            )
        return None

    def _build_ssl_context(self) -> ssl.SSLContext:
        if self.allow_insecure_ssl:
            return ssl._create_unverified_context()

        context = ssl.create_default_context()
        if sys.platform == "win32":
            for certificate, encoding, trust in ssl.enum_certificates("ROOT"):
                server_trusted = trust is True or (
                    trust is not False and "1.3.6.1.5.5.7.3.1" in trust
                )
                if encoding != "x509_asn" or not server_trusted:
                    continue
                try:
                    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
                except ssl.SSLError:
                    continue
        return context

    def _fetch_updates(self) -> list[dict[str, Any]]:
        if not self.bot_token:
            return []
        query = urllib_parse.urlencode(
            {
                "timeout": 30,
                "limit": 20,
                "offset": self._offset,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
        )
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates?{query}"
        request = urllib_request.Request(url, headers={"Accept": "application/json"})
        context = self._build_ssl_context()
        with urllib_request.urlopen(request, timeout=35, context=context) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if not payload.get("ok"):
            return []
        updates = payload.get("result") or []
        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                self._offset = max(self._offset, int(update_id) + 1)
        return updates

    def _post_telegram(self, method: str, payload: dict[str, Any]) -> None:
        if not self.bot_token:
            return
        form_data = {
            key: json.dumps(value) if isinstance(value, (dict, list)) else value
            for key, value in payload.items()
        }
        data = urllib_parse.urlencode(form_data)
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        request = urllib_request.Request(
            url,
            data=data.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=10, context=self._build_ssl_context()) as response:
                result = json.loads(response.read().decode("utf-8") or "{}")
            if not result.get("ok"):
                logger.warning("telegram %s failed: %s", method, result.get("description", "unknown error"))
        except urllib_error.HTTPError as exc:  # pragma: no cover - network-only path
            try:
                result = json.loads(exc.read().decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                result = {}
            description = result.get("description", str(exc))
            if method == "editMessageText" and "message is not modified" in description.lower():
                return
            logger.warning("telegram %s failed: %s", method, description)
        except Exception as exc:  # pragma: no cover - network-only path
            logger.warning("telegram %s failed: %s", method, exc)

    def _send_message(self, chat_id: str | int, text: str) -> None:
        if text:
            self._post_telegram(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": self._keyboard_markup(),
                },
            )

    def _edit_message(self, chat_id: str | int, message_id: int, text: str) -> None:
        self._post_telegram(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": self._keyboard_markup(),
            },
        )

    def _answer_callback_query(self, callback_query_id: str) -> None:
        self._post_telegram("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def process_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query") or {}
        callback_query_id = callback_query.get("id")
        if not self._message_is_allowed(update):
            if callback_query_id:
                await asyncio.to_thread(self._answer_callback_query, str(callback_query_id))
            return
        message = update.get("message") or callback_query.get("message") or {}
        command_text = callback_query.get("data") or message.get("text") or ""
        if self.parse_command(command_text) == "balance":
            await asyncio.gather(*(account.refresh_balance() for account in self.control_state.accounts))
        response = self._handle_command(update)
        if callback_query_id:
            await asyncio.to_thread(self._answer_callback_query, str(callback_query_id))
        if response is None:
            return
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is not None:
            if callback_query_id and message.get("message_id") is not None:
                await asyncio.to_thread(self._edit_message, chat_id, message["message_id"], response)
            else:
                await asyncio.to_thread(self._send_message, chat_id, response)

    async def start(self) -> None:
        if not self.bot_token or not self.allowed_chat_id:
            return

        while not self._stop_event.is_set():
            try:
                updates = await asyncio.to_thread(self._fetch_updates)
                for update in updates:
                    await self.process_update(update)
            except Exception as exc:
                logger.warning("telegram poll failed: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        self._stop_event.set()


def load_telegram_config(config: dict[str, str] | None = None) -> dict[str, str]:
    config = config or {}
    values = {
        "TELEGRAM_BOT_TOKEN": (config.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip(),
        "TELEGRAM_ALLOWED_CHAT_ID": (config.get("TELEGRAM_ALLOWED_CHAT_ID") or os.getenv("TELEGRAM_ALLOWED_CHAT_ID") or "").strip(),
        "TELEGRAM_ALLOW_INSECURE_SSL": (config.get("TELEGRAM_ALLOW_INSECURE_SSL") or os.getenv("TELEGRAM_ALLOW_INSECURE_SSL") or "").strip(),
    }
    return {key: value for key, value in values.items() if value}
