import asyncio
import io
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch

from logger import setup_logger
from telegram_control import MirrorControlState, TelegramMirrorController
from mirror_trade import (
    _deal_to_trade_event,
    confirm_live_mode,
    create_trade_plan,
    dispatch_mirror_trade,
    execute_trade_plan,
    get_child_ssids,
    normalize_action,
)


class FakeExecutor:
    def __init__(self, label):
        self.label = label
        self.calls = []

    def place_trade(self, asset, action, amount, duration):
        payload = {
            "label": self.label,
            "asset": asset,
            "action": action,
            "amount": amount,
            "duration": duration,
        }
        self.calls.append(payload)
        return payload


class FakeAccount:
    def __init__(self, label, balance="100.00"):
        self.label = label
        self.connection_status = "connected"
        self._balance = balance
        self._balance_updated_at = None
        self.discarded_events = False
        self.refresh_count = 0
        self.calls = []

    def discard_opened_trade_events(self):
        self.discarded_events = True

    async def refresh_balance(self):
        self.refresh_count += 1

    async def place_trade(self, asset, action, amount, duration):
        result = {"label": self.label, "asset": asset, "action": action, "amount": amount, "duration": duration}
        self.calls.append(result)
        return result


def test_create_trade_plan_marks_child_as_mirrored_only():
    plan = create_trade_plan("EURUSD", "call", 1.25, 60)

    assert plan["master"]["origin"] == "manual"
    assert plan["child"]["origin"] == "mirrored_from_master"
    assert plan["child"]["triggered_by"] == "manual_master"
    assert plan["child"]["source_label"] == "master"


def test_execute_trade_plan_runs_both_accounts_once():
    master = FakeExecutor("master")
    child = FakeExecutor("child")
    plan = create_trade_plan("EURUSD", "put", 2.0, 120)

    result = execute_trade_plan(master, child, plan)

    assert len(master.calls) == 1
    assert len(child.calls) == 1
    assert result["master"]["action"] == "put"
    assert result["child"]["action"] == "put"
    assert result["child"]["label"] == "child"


def test_get_child_ssids_includes_legacy_and_numbered_children():
    config = {
        "CHILD_SSID_2": "second",
        "CHILD_SSID": "first",
        "CHILD_SSID_10": "tenth",
    }

    assert get_child_ssids(config) == ["first", "second", "tenth"]


def test_open_deal_callback_queues_trade_event_without_polling():
    from mirror_trade import PocketOptionTradeExecutor

    class Deal:
        id = "deal-1"
        command = 0
        asset = "EURUSD"
        amount = 1.0
        time = 60

    executor = PocketOptionTradeExecutor("ssid", "master")

    async def receive_event():
        await executor._handle_success_open_deal(Deal())
        return await executor.next_opened_trade(timeout=0.01)

    event = asyncio.run(receive_event())

    assert event["trade_id"] == "deal-1"
    assert event["action"] == "call"


def test_open_deal_callback_deduplicates_repeated_trade_events():
    from mirror_trade import PocketOptionTradeExecutor

    class Deal:
        id = "deal-1"
        command = 0
        asset = "EURUSD"
        amount = 1.0
        time = 60

    executor = PocketOptionTradeExecutor("ssid", "master")

    async def receive_events():
        await executor._handle_success_open_deal(Deal())
        await executor._handle_success_open_deal(Deal())
        first = await executor.next_opened_trade(timeout=0.01)
        second = await executor.next_opened_trade(timeout=0.01)
        return first, second

    first, second = asyncio.run(receive_events())

    assert first["trade_id"] == "deal-1"
    assert second is None


def test_balance_callback_records_and_logs_balance():
    from mirror_trade import PocketOptionTradeExecutor

    class BalanceUpdate:
        balance = "123.45"

    executor = PocketOptionTradeExecutor("ssid", "child-1")
    asyncio.run(executor._handle_balance_update(BalanceUpdate()))

    assert executor._balance == "123.45"
    assert executor._balance_event.is_set()


def test_extracts_ssid_and_payload_from_wrapper():
    from mirror_trade import PocketOptionTradeExecutor

    wrapped = '42["auth",{"session":"abc123","isDemo":1,"uid":42,"platform":2,"isFastHistory":true,"isOptimized":true}]'
    executor = PocketOptionTradeExecutor(wrapped, "test")

    assert executor.ssid == "abc123"
    assert executor.auth_payload["uid"] == 42
    assert executor.auth_payload["isDemo"] == 1
    assert executor.auth_payload["platform"] == 2


def test_normalize_action_maps_numeric_command_values_correctly():
    assert normalize_action(0) == "call"
    assert normalize_action("0") == "call"
    assert normalize_action(1) == "put"
    assert normalize_action("1") == "put"


def test_live_mode_accepts_env_confirmation_without_prompt(monkeypatch):
    monkeypatch.setenv("LIVE_CONFIRMATION", "LIVE")
    assert confirm_live_mode() is True


def test_live_mode_accepts_confirmation_from_loaded_profile():
    assert confirm_live_mode({"LIVE_CONFIRMATION": "'LIVE'"}) is True


def test_live_mode_returns_false_when_no_input_is_available(monkeypatch):
    monkeypatch.delenv("LIVE_CONFIRMATION", raising=False)
    with patch("builtins.input", side_effect=EOFError):
        assert confirm_live_mode() is False


def test_child_ssids_can_be_loaded_from_environment_without_profile_file(monkeypatch):
    monkeypatch.setenv("CHILD_SSID", "child-session")
    monkeypatch.setenv("CHILD_SSID_2", "second-child-session")
    assert get_child_ssids() == ["child-session", "second-child-session"]


def test_deal_to_trade_event_preserves_put_and_derives_duration():
    class Deal:
        id = "deal-1"
        command = 1
        asset = "EURUSD_otc"
        amount = 3.0
        open_timestamp = 1000
        close_timestamp = 1060

    event = _deal_to_trade_event(Deal())

    assert event["trade_id"] == "deal-1"
    assert event["asset"] == "EURUSD_otc"
    assert event["action"] == "put"
    assert event["amount"] == 3.0
    assert event["duration"] == 60
    assert event["open_timestamp"] == 1000
    assert event["close_timestamp"] == 1060


def test_mirror_control_state_can_pause_and_resume():
    state = MirrorControlState()

    assert state.is_paused() is False
    assert state.should_process_trade() is True

    state.pause()
    assert state.is_paused() is True
    assert state.should_process_trade() is False

    state.resume()
    assert state.is_paused() is False
    assert state.should_process_trade() is True


def test_master_switch_requires_pause_and_no_inflight_trades():
    state = MirrorControlState()
    master = FakeAccount("master")
    child_one = FakeAccount("child-1")
    child_two = FakeAccount("child-2")
    state.configure_accounts(master, [child_one, child_two])

    assert state.select_master("child-1") is False
    state.pause()
    state.begin_trade()
    assert state.select_master("child-1") is False
    state.end_trade()

    assert state.select_master("child-1") is True
    assert state.master_executor is child_one
    assert state.child_executors() == [master, child_two]
    assert master.label in state.enabled_children
    assert child_one.discarded_events is True
    assert state.master_changed.is_set() is True


def test_child_toggle_and_recent_trade_history():
    state = MirrorControlState()
    master = FakeAccount("master")
    child = FakeAccount("child-1")
    state.configure_accounts(master, [child])

    assert state.toggle_child("child-1") is False
    assert state.child_executors() == []
    assert state.toggle_child("child-1") is True
    assert state.child_executors() == [child]
    assert state.toggle_child("master") is None

    trade = {"trade_id": "trade-1", "asset": "EURUSD", "action": "call", "amount": 2}
    state.record_trade(trade, "detected")
    state.record_trade(trade, "mirrored 1")
    assert len(state.trade_history) == 1
    assert state.trade_history[0]["status"] == "mirrored 1"


def test_telegram_account_views_and_master_selection_controls():
    state = MirrorControlState()
    master = FakeAccount("master")
    master.raw_ssid = "never-display-this"
    child = FakeAccount("child-1", "250.50")
    state.configure_accounts(master, [child])
    controller = TelegramMirrorController("test-token", "12345", state)

    assert controller.parse_command("/balance") == "balance"
    assert controller.parse_command("/children") == "children"
    assert controller.parse_command("/master") == "master"
    assert controller.parse_command("/recent") == "recent"
    help_update = {"message": {"chat": {"id": 12345}, "text": "/help"}}
    assert "/balance" in controller._handle_command(help_update)
    assert "/start" in controller._handle_command(help_update)
    assert "250.50" in controller._balance_message()
    assert "never-display-this" not in controller._status_message()

    master_update = {"message": {"chat": {"id": 12345}, "text": "/master"}}
    assert "Pause mirroring" in controller._handle_command(master_update)
    state.pause()
    assert "previous master will be enabled" in controller._handle_command(master_update)
    assert controller._keyboard_markup()["inline_keyboard"][0][0]["callback_data"] == "master_select:child-1"

    select_update = {
        "callback_query": {
            "data": "master_select:child-1",
            "message": {"chat": {"id": 12345}},
        }
    }
    assert "Former master enabled as a child" in controller._handle_command(select_update)
    assert state.master_executor is child
    assert state.child_executors() == [master]


def test_balance_command_refreshes_each_configured_account():
    state = MirrorControlState()
    master = FakeAccount("master")
    child = FakeAccount("child-1")
    state.configure_accounts(master, [child])
    controller = TelegramMirrorController("test-token", "12345", state)
    update = {"message": {"chat": {"id": 12345}, "text": "/balance"}}

    with patch.object(controller, "_send_message"):
        asyncio.run(controller.process_update(update))

    assert master.refresh_count == 1
    assert child.refresh_count == 1


def test_trade_dispatch_uses_only_enabled_children():
    state = MirrorControlState()
    master = FakeAccount("master")
    first_child = FakeAccount("child-1")
    second_child = FakeAccount("child-2")
    state.configure_accounts(master, [first_child, second_child])
    assert state.toggle_child("child-1") is False
    trade_event = {
        "trade_id": "source-1",
        "asset": "EURUSD",
        "action": "call",
        "amount": 2,
        "duration": 60,
    }
    state.begin_trade()

    asyncio.run(
        dispatch_mirror_trade(
            master,
            [first_child, second_child],
            trade_event,
            asyncio.Semaphore(1),
            state,
        )
    )

    assert first_child.calls == []
    assert len(second_child.calls) == 1
    assert state.trade_history[0]["status"] == "mirrored 1"
    assert state.inflight_trade_count == 0


def test_telegram_controller_parses_allowed_commands_and_chat_ids():
    controller = TelegramMirrorController("test-token", "12345", MirrorControlState())

    assert controller.parse_command("/status") == "status"
    assert controller.parse_command("/pause") == "pause"
    assert controller.parse_command("/resume") == "resume"
    assert controller.parse_command("/start") == "help"
    assert controller._message_is_allowed({"message": {"chat": {"id": 12345}, "text": "/status"}}) is True
    assert controller._message_is_allowed({"message": {"chat": {"id": 99999}, "text": "/pause"}}) is False


def test_telegram_inline_controls_update_state_and_keyboard():
    controller = TelegramMirrorController("test-token", "12345", MirrorControlState())
    callback_update = {
        "callback_query": {
            "id": "callback-1",
            "data": "pause",
            "message": {"message_id": 7, "chat": {"id": 12345}},
        }
    }

    assert controller._message_is_allowed(callback_update) is True
    assert controller._handle_command(callback_update) == "<b>Mirror: ⏸ Paused</b>\nSource: not selected\n<b>Accounts</b>"
    assert controller.control_state.is_paused() is True
    assert controller._keyboard_markup()["inline_keyboard"][0][1]["callback_data"] == "resume"

    callback_update["callback_query"]["data"] = "resume"
    assert controller._handle_command(callback_update) == "<b>Mirror: 🟢 Running</b>\nSource: not selected\n<b>Accounts</b>"
    assert controller.control_state.is_paused() is False


def test_telegram_callback_edits_panel_and_acknowledges_button_tap():
    controller = TelegramMirrorController("test-token", "12345", MirrorControlState())
    update = {
        "callback_query": {
            "id": "callback-2",
            "data": "pause",
            "message": {"message_id": 7, "chat": {"id": 12345}},
        }
    }
    with patch.object(controller, "_answer_callback_query") as answer_callback, patch.object(
        controller, "_edit_message"
    ) as edit_message, patch.object(controller, "_send_message") as send_message:
        asyncio.run(controller.process_update(update))

    answer_callback.assert_called_once_with("callback-2")
    edit_message.assert_called_once_with(
        12345, 7, "<b>Mirror: ⏸ Paused</b>\nSource: not selected\n<b>Accounts</b>"
    )
    send_message.assert_not_called()


def test_telegram_poll_explicitly_requests_button_callbacks():
    controller = TelegramMirrorController("test-token", "12345", MirrorControlState())
    response = type(
        "Response",
        (),
        {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *args: None,
            "read": lambda self: b'{"ok":true,"result":[]}',
        },
    )()
    with patch("telegram_control.urllib_request.urlopen", return_value=response) as urlopen:
        assert controller._fetch_updates() == []

    request = urlopen.call_args.args[0]
    allowed_updates = parse_qs(urlsplit(request.full_url).query)["allowed_updates"]
    assert allowed_updates == ['["message", "callback_query"]']


def test_telegram_ignores_unchanged_panel_edit_error():
    from urllib.error import HTTPError

    controller = TelegramMirrorController("test-token", "12345", MirrorControlState())
    error = HTTPError(
        "https://api.telegram.org/bot.../editMessageText",
        400,
        "Bad Request",
        hdrs=None,
        fp=io.BytesIO(b'{"ok":false,"description":"Bad Request: message is not modified"}'),
    )
    with patch("telegram_control.urllib_request.urlopen", side_effect=error), patch(
        "mirror_trade.logger.warning"
    ) as warning:
        controller._post_telegram("editMessageText", {"chat_id": 12345, "message_id": 7})

    warning.assert_not_called()


def test_telegram_controller_uses_profile_ssl_flag_when_enabled():
    controller = TelegramMirrorController("test-token", "12345", MirrorControlState(), allow_insecure_ssl=True)
    assert controller.allow_insecure_ssl is True


def test_setup_logger_writes_to_file(tmp_path):
    log_file = tmp_path / "mirror.log"
    logger = setup_logger("mirror-test", log_file=str(log_file))
    logger.info("hello logger")

    assert log_file.exists()
    assert "hello logger" in log_file.read_text(encoding="utf-8")

