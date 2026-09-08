import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch

from logger import setup_logger
from mirror_trade import (
    _deal_to_trade_event,
    confirm_live_mode,
    create_trade_plan,
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


def test_setup_logger_writes_to_file(tmp_path):
    log_file = tmp_path / "mirror.log"
    logger = setup_logger("mirror-test", log_file=str(log_file))
    logger.info("hello logger")

    assert log_file.exists()
    assert "hello logger" in log_file.read_text(encoding="utf-8")

