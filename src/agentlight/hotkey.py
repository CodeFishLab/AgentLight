"""全局快捷键的组合键解析。

配置里存的是人能读懂的写法（"Ctrl+Alt+L"），注册时换算成 RegisterHotKey 要的
修饰位和虚拟键码。解析放在这里而不是托盘里，是为了不依赖 Win32 也能校验和测试。
"""

from __future__ import annotations

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

# 显示顺序固定，同一个组合不管用户怎么写，存下来都是同一个字符串
_MODIFIERS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("Ctrl", MOD_CONTROL, ("ctrl", "control")),
    ("Alt", MOD_ALT, ("alt",)),
    ("Shift", MOD_SHIFT, ("shift",)),
    ("Win", MOD_WIN, ("win", "meta", "super")),
)


def _key_table() -> dict[str, tuple[str, int]]:
    keys: dict[str, tuple[str, int]] = {}
    for code in range(ord("A"), ord("Z") + 1):
        keys[chr(code).lower()] = (chr(code), code)
    for digit in range(10):
        keys[str(digit)] = (str(digit), 0x30 + digit)
    for index in range(1, 25):
        keys[f"f{index}"] = (f"F{index}", 0x6F + index)  # VK_F1 = 0x70
    return keys


_KEYS = _key_table()


def parse_combo(text: str) -> tuple[int, int, str]:
    """把 "Ctrl+Alt+L" 解析成 (修饰位, 虚拟键码, 规范写法)。

    至少要有 Ctrl、Alt、Win 之一：只按 Shift+字母就是在打大写字母，
    注册成全局热键会让这个字母在所有程序里都打不出来。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("快捷键不能为空")
    parts = [part.strip().lower() for part in text.split("+")]
    if any(not part for part in parts):
        raise ValueError(f"快捷键格式不对：{text}")

    modifiers = 0
    key: tuple[str, int] | None = None
    for part in parts:
        for _, flag, aliases in _MODIFIERS:
            if part in aliases:
                if modifiers & flag:
                    raise ValueError(f"快捷键里有重复的修饰键：{text}")
                modifiers |= flag
                break
        else:
            if part not in _KEYS:
                raise ValueError(f"不支持的按键「{part}」，请用字母、数字或 F1–F24")
            if key is not None:
                raise ValueError(f"快捷键只能有一个主键：{text}")
            key = _KEYS[part]

    if key is None:
        raise ValueError("快捷键缺少主键，例如 Ctrl+Alt+L 里的 L")
    if not modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        raise ValueError("快捷键至少要包含 Ctrl、Alt 或 Win 中的一个")

    names = [name for name, flag, _ in _MODIFIERS if modifiers & flag]
    return modifiers, key[1], "+".join([*names, key[0]])


def normalize_combo(text: str) -> str:
    return parse_combo(text)[2]
