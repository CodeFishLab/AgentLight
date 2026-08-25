from __future__ import annotations

from agentlight.device import OrvynController, parse_status, styled_sound


def test_parse_full_status_packet() -> None:
    data = bytearray(38)
    data[0] = 2
    data[1] = 20
    data[2:11] = bytes.fromhex("000000ff8000000000")
    data[13:15] = (1200).to_bytes(2, "little")
    data[24] = 0
    data[25] = 30
    data[26] = 1
    data[27:29] = (300).to_bytes(2, "little")
    data[30] = 0
    data[32] = 1
    data[33:35] = (180).to_bytes(2, "little")
    data[35] = 67
    data[36] = 88
    status = parse_status(bytes(data))
    assert status["mode"] == "breath"
    assert status["colors"] == ["000000", "ff8000", "000000"]
    assert status["period_ms"] == 1200
    assert status["auto_sleep"] is True
    assert status["sleep_timeout"] == 300
    assert status["display_ready"] is True
    assert status["screen_rotation"] == 180


def test_parse_rejects_short_packet() -> None:
    try:
        parse_status(b"short")
    except ValueError as exc:
        assert "short status" in str(exc)
    else:
        raise AssertionError("short packet should fail")


def test_quota_protocol_accepts_unknown_sentinel() -> None:
    controller = OrvynController()
    recorded: list[tuple[int, bytes]] = []
    controller._write_status_command = lambda command, payload: recorded.append((command, payload)) or {}  # type: ignore[method-assign]

    controller.set_quota(37, 255)

    assert recorded == [(0x31, bytes([37, 255]))]


def test_sound_styles_change_rhythm_not_volume() -> None:
    assert styled_sound("success", "standard") == "success"
    assert styled_sound("beep", "gentle") == "custom:1200:72"
    assert styled_sound("success", "prominent") == "custom:900:117,1300:169,0:80,900:117,1300:169"
    prominent = styled_sound("custom:1000:100,0:50,1200:100", "prominent")
    assert prominent.count(",") + 1 == 6
    assert styled_sound("stop", "prominent") == "stop"
