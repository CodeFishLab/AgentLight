from __future__ import annotations

from agentlight import effects
from agentlight.models import StateProfile


RED = "ff0000"
GREEN = "00ff00"
BLUE = "0000ff"
TRIO = [RED, GREEN, BLUE]
BLACK = (0, 0, 0)


def test_registry_covers_every_declared_effect() -> None:
    declared = set(effects.HARDWARE_MODES) | set(effects.SOFTWARE_EFFECTS)
    assert set(effects.REGISTRY) == declared
    assert set(effects._RENDERERS) == declared
    for identifier, spec in effects.REGISTRY.items():
        assert spec.id == identifier
        expected = "hardware" if identifier in effects.HARDWARE_MODES else "software"
        assert spec.kind == expected
        assert spec.label and spec.description


def test_every_effect_returns_three_valid_leds_across_the_whole_cycle() -> None:
    for identifier in effects.REGISTRY:
        for step in range(0, 21):
            frame = effects.render_frame(identifier, TRIO, step / 20)
            assert len(frame) == 3
            for channel in frame:
                assert len(channel) == 3
                assert all(isinstance(value, int) and 0 <= value <= 255 for value in channel)


def test_phase_wraps_so_the_cycle_is_seamless() -> None:
    for identifier in effects.REGISTRY:
        assert effects.render_frame(identifier, TRIO, 0.0) == effects.render_frame(identifier, TRIO, 1.0)
        assert effects.render_frame(identifier, TRIO, 0.25) == effects.render_frame(identifier, TRIO, 3.25)
        assert effects.render_frame(identifier, TRIO, -0.75) == effects.render_frame(identifier, TRIO, 0.25)


def test_unknown_effect_and_bad_phase_fall_back_to_static() -> None:
    assert effects.render_frame("nope", TRIO, 0.4) == [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    assert effects.render_frame("static", TRIO, float("nan")) == [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    assert effects.resolve("nope").id == "static"


def test_short_and_malformed_colors_are_padded_with_black() -> None:
    assert effects.render_frame("static", [RED], 0.0) == [(255, 0, 0), BLACK, BLACK]
    assert effects.render_frame("static", ["#00FF00", "zzz", ""], 0.0) == [(0, 255, 0), BLACK, BLACK]


def test_hardware_modes_match_firmware_semantics() -> None:
    assert effects.render_frame("static", TRIO, 0.9) == [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    # 闪烁是 50% 占空的方波
    assert effects.render_frame("blink", TRIO, 0.2) == [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    assert effects.render_frame("blink", TRIO, 0.7) == [BLACK, BLACK, BLACK]
    # 呼吸一轮从灭到亮再回到灭
    assert effects.render_frame("breath", TRIO, 0.0) == [BLACK, BLACK, BLACK]
    assert effects.render_frame("breath", TRIO, 0.5) == [(255, 0, 0), (0, 255, 0), (0, 0, 255)]


def test_chase_lights_one_led_per_slot_and_honours_direction() -> None:
    assert effects.render_frame("chase", TRIO, 0.0) == [(255, 0, 0), BLACK, BLACK]
    assert effects.render_frame("chase", TRIO, 0.5) == [BLACK, (0, 255, 0), BLACK]
    assert effects.render_frame("chase", TRIO, 0.9) == [BLACK, BLACK, (0, 0, 255)]
    backward = effects.render_frame("chase", TRIO, 0.0, {"direction": "backward"})
    assert backward == [BLACK, BLACK, (0, 0, 255)]


def test_chase_duty_creates_a_gap_between_leds() -> None:
    params = {"duty": 0.5}
    assert effects.render_frame("chase", TRIO, 0.1, params) == [(255, 0, 0), BLACK, BLACK]
    # 时段后半段熄灭，形成间隔
    assert effects.render_frame("chase", TRIO, 0.3, params) == [BLACK, BLACK, BLACK]


def test_comet_head_is_brightest_and_tail_decays() -> None:
    frame = effects.render_frame("comet", [RED, RED, RED], 0.0, {"tail": 0.5})
    head, two_behind, one_behind = frame
    assert head == (255, 0, 0)
    assert head[0] > one_behind[0] > two_behind[0] > 0


def test_pulse_seq_with_zero_spread_is_fully_synchronised() -> None:
    frame = effects.render_frame("pulse_seq", [RED, RED, RED], 0.3, {"spread": 0.0})
    assert frame[0] == frame[1] == frame[2]
    staggered = effects.render_frame("pulse_seq", [RED, RED, RED], 0.3, {"spread": 1 / 3})
    assert len({item[0] for item in staggered}) == 3


def test_alternate_swaps_the_outer_pair_with_the_middle() -> None:
    assert effects.render_frame("alternate", TRIO, 0.1) == [(255, 0, 0), BLACK, (0, 0, 255)]
    assert effects.render_frame("alternate", TRIO, 0.8) == [BLACK, (0, 255, 0), BLACK]


def test_wipe_fills_then_clears_from_the_same_end() -> None:
    filling = effects.render_frame("wipe", [RED, RED, RED], 0.25)
    assert filling[0] == (255, 0, 0)
    assert filling[0][0] > filling[1][0] > filling[2][0]
    clearing = effects.render_frame("wipe", [RED, RED, RED], 0.75)
    assert clearing[0] == BLACK
    assert clearing[2][0] > clearing[1][0] > clearing[0][0]


def test_rainbow_ignores_configured_colors_and_lights_every_led() -> None:
    spec = effects.REGISTRY["rainbow"]
    assert spec.uses_colors is False
    frame = effects.render_frame("rainbow", ["000000", "000000", "000000"], 0.0)
    assert frame == [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    assert all(sum(channel) > 0 for channel in frame)


def test_normalize_params_clamps_numbers_and_rejects_unknown_enums() -> None:
    values = effects.normalize_params("comet", {"tail": 9.9, "direction": "sideways"})
    assert values["tail"] == 0.95
    assert values["direction"] == "forward"
    assert effects.normalize_params("comet", {"tail": -5})["tail"] == 0.05
    assert effects.normalize_params("comet", {"tail": "abc"})["tail"] == 0.35
    assert effects.normalize_params("comet", None)["tail"] == 0.35
    # 未声明的参数不会被带进来
    assert "bogus" not in effects.normalize_params("comet", {"bogus": 1})
    assert effects.normalize_params("static", {"tail": 1}) == {}


def test_scale_maps_intensity_onto_rgb() -> None:
    assert effects.scale((200, 100, 50), 0.0) == BLACK
    assert effects.scale((200, 100, 50), 1.0) == (200, 100, 50)
    assert effects.scale((200, 100, 50), 0.5) == (100, 50, 25)
    # 越界强度被夹住
    assert effects.scale((200, 100, 50), 5) == (200, 100, 50)
    assert effects.scale((200, 100, 50), -1) == BLACK


def test_registry_payload_is_json_friendly_for_the_web_ui() -> None:
    payload = effects.registry_payload()
    assert len(payload) == len(effects.REGISTRY)
    entry = next(item for item in payload if item["id"] == "chase")
    assert entry["kind"] == "software"
    assert entry["usesColors"] is True
    keys = {param["key"] for param in entry["params"]}
    assert keys == {"direction", "duty"}
    direction = next(param for param in entry["params"] if param["key"] == "direction")
    assert direction["choices"] == ["forward", "backward"]


def test_frame_rate_is_clamped_to_a_sane_range() -> None:
    assert effects.clamp_frame_rate(20) == 20
    assert effects.clamp_frame_rate(999) == effects.MAX_FRAME_RATE
    assert effects.clamp_frame_rate(0) == effects.MIN_FRAME_RATE
    assert effects.clamp_frame_rate("abc") == effects.DEFAULT_FRAME_RATE
    assert effects.clamp_frame_rate(None) == effects.DEFAULT_FRAME_RATE


def test_is_software_splits_firmware_modes_from_host_driven_ones() -> None:
    assert effects.is_software("chase") is True
    assert effects.is_software("static") is False
    assert effects.is_software("blink") is False
    assert effects.is_software("unknown") is False


def test_profile_migrates_legacy_mode_into_effect() -> None:
    profile = StateProfile.from_dict({"colors": [GREEN], "mode": "breath"})
    assert profile.effect == "breath"
    assert profile.mode == "breath"
    assert profile.is_software_effect is False


def test_profile_keeps_firmware_on_static_while_a_software_effect_runs() -> None:
    profile = StateProfile.from_dict({"colors": TRIO, "mode": "blink", "effect": "chase"})
    assert profile.effect == "chase"
    # 软件效果期间固件必须保持常亮，动画由主机推帧
    assert profile.mode == "static"
    assert profile.is_software_effect is True
    assert profile.frame(0.0) == [(255, 0, 0), BLACK, BLACK]


def test_profile_normalises_effect_params_and_survives_a_round_trip() -> None:
    profile = StateProfile.from_dict({
        "colors": TRIO,
        "effect": "comet",
        "effect_params": {"tail": 99, "direction": "backward", "junk": True},
    })
    assert profile.effect_params == {"tail": 0.95, "direction": "backward"}
    assert StateProfile.from_dict(profile.to_dict()) == profile
