from __future__ import annotations

import re

from agentlight import effects
from agentlight.paths import webui_dir


def read(name: str) -> str:
    return (webui_dir() / name).read_text(encoding="utf-8")


def test_web_ui_implements_exactly_the_effects_the_backend_declares() -> None:
    """前端独立实现了一套渲染，效果清单必须和 Python 注册表保持一致。"""
    source = read("assets/app.js")
    block = re.search(r"const RENDERERS = \{(.*?)\n  \};", source, re.S)
    assert block, "app.js 里找不到 RENDERERS 定义"
    rendered = set(re.findall(r"^\s{4}(\w+):", block.group(1), re.M))

    assert rendered == set(effects.REGISTRY)


def test_web_ui_mirrors_the_state_list() -> None:
    from agentlight.models import VALID_STATES

    source = read("assets/app.js")
    declared = re.search(r'const STATES = \[(.*?)\];', source, re.S)
    assert declared
    states = tuple(re.findall(r'"(\w+)"', declared.group(1)))

    assert states == VALID_STATES


def test_page_is_self_contained_and_carries_the_token_placeholder() -> None:
    html = read("index.html")

    assert "__AGENTLIGHT_TOKEN__" in html
    # 打包后没有网络，也不该把本地状态泄露给第三方
    for marker in ("http://", "https://", "//cdn", "integrity="):
        assert marker not in html, marker


def test_stylesheet_never_animates_layout_or_filter_properties() -> None:
    css = read("assets/app.css")

    # 每帧重算 filter/box-shadow 会掉帧，辉光必须用径向渐变实现
    assert "filter: blur" not in css
    assert "prefers-reduced-motion" in css
    assert "will-change: opacity, transform" in css


def test_javascript_never_assigns_untrusted_html() -> None:
    """source/session_id 来自 Hook 载荷，一律用 textContent 渲染。"""
    source = read("assets/app.js")

    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source


def test_dashboard_quota_and_effects_manual_controls_keep_unique_targets() -> None:
    """总览与设备页共用配额数据，手动状态只能在灯效方案中保留一份。"""
    html = read("index.html")
    source = read("assets/app.js")

    for element_id in (
        "dash-quota-source",
        "dash-quota-dials",
        "dash-quota-usage",
        "dash-refresh-quota",
        "quota-source",
        "quota-dials",
        "quota-usage",
        "sync-quota",
        "manual-buttons",
        "set-quota-refresh-interval",
    ):
        assert html.count(f'id="{element_id}"') == 1

    dashboard = re.search(r'<section class="page is-active" data-page="dashboard".*?</section>', html, re.S)
    effects_page = re.search(r'<section class="page" data-page="effects".*?</section>', html, re.S)
    assert dashboard and "dash-quota-dials" in dashboard.group(0) and "manual-buttons" not in dashboard.group(0)
    assert effects_page and "manual-buttons" in effects_page.group(0)
    assert 'picker: "dash-quota-source"' in source
    assert 'picker: "quota-source"' in source


def test_requested_controls_keep_state_and_layout_contracts() -> None:
    html = read("index.html")
    source = read("assets/app.js")
    css = read("assets/app.css")

    assert html.index('id="toggle-mute"') < html.index('id="toggle-night"')
    assert html.index('id="dash-refresh-quota"') < html.index('id="dash-quota-source"')
    assert html.index('id="sync-quota"') < html.index('id="quota-source"')
    # 「恢复自动」不再动态创建，已写死在标题那一行的 HTML 里
    assert 'id="manual-auto"' in html
    assert "paintManualControls();" in source
    assert "quota_refresh_interval_seconds" in source
    assert 'id="set-quota-refresh-interval" min="0" max="86400" step="1"' in html
    # 自动刷新下限 15 秒。Codex 每次刷新要倒序扫会话文件（实测约 20ms），Claude
    # 本来就被服务端锁在 60 秒，1 秒一跑只是让 Codex 空转。手动「刷新」不走这里。
    assert "Math.max(15, Math.min(86400, Math.floor(configured)))" in source
    # min-height 而不是 height：既锁住下限（切换来源不跳），又允许被 stretch 拉齐邻居
    assert ".quota-card-dashboard { min-height:" in css
    assert ".quota-card-device { min-height:" in css
    assert "quota-card-device { height:" not in css
    assert ".quota-scroll { flex: 1 1 auto; min-height: 0; overflow: hidden; }" in css
    assert "scrollbar-gutter" not in css


def test_state_labels_match_the_backend_table() -> None:
    """状态栏（Python）和配置页（JS）各有一份中文名，漂了就会两处叫法不一致。"""
    from agentlight.models import STATE_LABELS

    source = read("assets/app.js")
    block = re.search(r"const STATE_LABELS = \{(.*?)\n  \};", source, re.S)
    assert block, "app.js 里找不到 STATE_LABELS 定义"
    labels = dict(re.findall(r'(\w+):\s*"([^"]+)"', block.group(1)))

    assert labels == STATE_LABELS


def test_quota_panel_offers_all_three_sources() -> None:
    """Codex / Claude / All 三选一，All 要能同时看两边的额度。"""
    source = read("assets/app.js")

    picker = re.search(r'for \(const \[key, label\] of \[(.*?)\]\) \{', source, re.S)
    assert picker, "app.js 里找不到配额来源选择器"
    assert re.findall(r'\["(\w+)"', picker.group(1)) == ["all", "codex", "claude"]
    assert 'quotaSource: "all"' in source
    assert source.index('source=codex"') < source.index('source=claude"')
    assert 'w.key === "nimbus_quill"' in source

    # 单来源时 quotaDials 要按 source 过滤，否则 All 和单选看起来一样
    assert 'if (source !== "claude")' in source
    assert 'if (source !== "codex")' in source


def test_every_element_the_script_reaches_for_actually_exists() -> None:
    """$("x") 拿不到元素就是一个静默的 TypeError，整段渲染会当场停住。

    新加控件时最容易漏的就是 HTML 那一半，所以这里把两边对起来。
    """
    html = read("index.html")
    source = read("assets/app.js")
    declared = set(re.findall(r'\sid="([\w-]+)"', html))
    requested = set(re.findall(r'\$\("([\w-]+)"\)', source))

    assert requested <= declared, sorted(requested - declared)


def test_resting_is_not_reported_as_a_disconnected_device() -> None:
    """主动关掉的连接是休息，不是故障；说成「未连接」会让人去查线。"""
    source = read("assets/app.js")

    assert "设备连接已关闭" in source
    assert '"/v1/device/rest"' in source
    # 侧栏指示灯常驻、按钮在设备页，两个都不属于总览。塞进 renderDashboard()
    # 的话，一离开总览页按钮状态就再也不刷新，点了变不回去。
    assert "function renderDeviceStatus(" in source
    assert "renderDeviceStatus();" in source.split("function renderAll()")[1][:400]


def test_the_quota_note_shows_an_absolute_time_not_a_relative_one() -> None:
    """那一行不是实时刷新的。写「x 小时 y 分前」会随着页面停留越读越不准，
    停一晚上再看就是彻底的假话。"""
    source = read("assets/app.js")

    assert "function formatMoment(" in source
    assert "formatMoment(claude.quota_updated_at)" in source
    assert "relativeTime(claude.quota_age_seconds)" not in source
    assert "前`" not in source.split("function quotaNote")[1].split("function paintQuotaPanel")[0]


def test_the_refresh_button_never_claims_success_when_a_source_failed() -> None:
    """任一来源刷新失败时，界面必须显示真实结果。"""
    source = read("assets/app.js")

    assert "function quotaOutcome(" in source
    block = source.split("function quotaOutcome")[1].split("function paintQuotaPanel")[0]
    assert "ok: false" in block
    # 两个刷新按钮都要走这条判断，不能只修一个
    assert source.count("quotaOutcome(store.quotaSource, store.quota)") == 2
    assert 'say("✓ Agent 配额已刷新")' not in source


def test_cached_quota_numbers_keep_normal_color_and_show_cache_time() -> None:
    """缓存值仍按正常配额显示，但环下面要写清这是什么时候的数据。"""
    source = read("assets/app.js")
    css = read("assets/app.css")

    assert "CLAUDE_LIVE_STATUSES" in source
    assert '`缓存 ${formatMoment(claude.quota_updated_at)' in source
    assert 'wrap.className = "dial"' in source
    assert ".dial.is-stale" not in css


def test_dashboard_quota_card_shows_its_last_update_time() -> None:
    html = read("index.html")
    source = read("assets/app.js")
    css = read("assets/app.css")

    assert 'id="dash-quota-updated"' in html
    assert 'updated: "dash-quota-updated"' in source
    assert "quotaUpdatedAt" in source
    assert "更新时间：${formatMoment(store.quotaUpdatedAt)" in source
    assert ".quota-updated" in css


def test_the_refresh_interval_label_is_short_and_its_caveats_live_in_a_tooltip() -> None:
    """边界条件全塞进标签会撑成三行。标签只说这是什么，细则收进「!」。"""
    html = read("index.html")
    css = read("assets/app.css")

    assert "<span>配额自动刷新（秒）</span>" in html
    assert "实际下限 Codex 15 秒" not in html.split("<span>配额自动刷新（秒）</span>")[0]
    assert 'id="quota-refresh-hint"' in html and 'id="quota-refresh-note"' in html
    # 数字微调箭头点一下只加 1，对「秒」没用，位置让给「!」
    assert 'class="no-spin"' in html
    assert ".no-spin::-webkit-inner-spin-button" in css
    # 提示框与字段等宽，否则它在某些栅格列会戳出卡片
    assert "position: absolute; z-index: 30; left: 0; right: 0;" in css


def test_about_page_has_a_guarded_uninstall_entry() -> None:
    html = read("index.html")
    source = read("assets/app.js")
    css = read("assets/app.css")

    about = html.split('<div class="card-head"><h2>关于</h2></div>')[1].split("</article>")[0]
    assert 'id="uninstall-app"' in about
    assert 'id="uninstall-dialog"' in html
    assert 'id="uninstall-title"' in html and 'aria-labelledby="uninstall-title"' in html
    assert "其他 Claude 或 Codex 配置不会被修改" in html
    assert "用户设置、日志与备份会保留" in html
    assert 'value="cancel" autofocus' in html
    assert 'api("POST", "/v1/uninstall")' in source
    assert "showModal()" in source
    assert ".about-danger" in css and ".confirm-dialog::backdrop" in css


def test_general_settings_can_change_the_local_port_and_restart_cleanly() -> None:
    html = read("index.html")
    source = read("assets/app.js")

    assert 'class="no-spin" id="set-api-port" min="1024" max="65535" step="1"' in html
    assert 'id="api-port-hint"' in html and 'id="api-port-note"' in html
    assert "修改后保存会自动重启，并跳转到新地址" in html
    assert "「自定义接入」中的 API 地址也会同步更新" in html
    assert "api_port: apiPort" in source
    assert 'api("POST", "/v1/restart")' in source
    assert "location.replace(nextUrl)" in source
    assert '$("set-api-port").value = String(config.api.port);' in source
    assert '$("api-endpoint").value = data.endpoint;' in source


def test_page_data_refreshes_in_the_background_every_thirty_seconds() -> None:
    """刷新数据不能粗暴 reload，否则当前页签、表单输入和弹窗都会被打断。"""
    source = read("assets/app.js")

    assert "const PAGE_REFRESH_MS = 30_000;" in source
    assert "setInterval(refreshPageData, PAGE_REFRESH_MS)" in source
    assert "location.reload(" not in source
    refresh = source.split("async function refreshPageData()", 1)[1].split("function schedulePageRefresh()", 1)[0]
    for call in ("pullStatus()", "pullConfig()", "fetchTimeline()", "renderQuota(false, true)"):
        assert call in refresh
    assert "store.quotaIntervalSeconds === seconds" in source
    assert 'store.pageRefreshing && store.page === "device"' in source
    assert 'store.pageRefreshing && store.page === "settings"' in source


def test_the_quota_note_renders_one_line_per_source() -> None:
    """两个来源挤在一行读起来费劲，用户要求分行。"""
    source = read("assets/app.js")

    block = source.split("function quotaNote")[1].split("function paintQuotaPanel")[0]
    assert "return lines.filter(Boolean);" in block
    # 渲染方必须逐行建元素；textContent 塞不进换行
    assert "for (const line of quotaNote(source, data))" in source


def test_a_failed_claude_fetch_tells_you_how_to_fix_it() -> None:
    """「凭据过期」四个字对着看半天也不知道该干嘛。直接把解法写出来。"""
    source = read("assets/app.js")

    assert "CLAUDE_TROUBLE" in source
    trouble = source.split("const CLAUDE_TROUBLE = {")[1].split("};")[0]
    assert "credential_stale" in trouble
    assert "claude" in trouble and "重新登录" in trouble
    # 这句是「数据从哪来」的内部细节，用户不关心
    assert "取自运行中的 Claude Code" not in source


def test_the_device_buttons_do_not_say_rest_anymore() -> None:
    html = read("index.html")
    source = read("assets/app.js")

    assert "关闭连接（休息）" not in html
    assert "关闭连接（休息）" not in source
    assert '关闭连接</button>' in html
    # 两条啰嗦的说明也一并删了
    assert "有活动灯光状态时会临时阻止休眠" not in html
    assert "USB 上照样在轮询" not in html


def test_the_codex_guide_spells_out_the_easy_to_miss_steps() -> None:
    """Codex 指南必须说明完整退出程序并在新任务中验证。"""
    html = read("index.html")
    guide = html.split("<h3>Codex</h3>")[1].split("</div>")[0]

    assert "Hooks need review" in guide
    assert "Trust all and continue" in guide
    assert "系统托盘" in guide
    assert "新建一个任务" in guide


def test_stale_is_selectable_but_gets_no_shortcut() -> None:
    """数字键 1–9 已经被现有 9 个状态占满。stale 只上按钮，不上快捷键 ——
    靠的是渲染列表和快捷键索引源分开，而不是在按键处理里打补丁。"""
    source = read("assets/app.js")

    assert 'const MANUAL_BUTTON_STATES = [...MANUAL_STATES, "stale"];' in source
    assert "for (const state of MANUAL_BUTTON_STATES)" in source
    # 快捷键仍然索引不含 stale 的那个数组，所以 stale 天然拿不到键
    assert "const state = MANUAL_STATES[index];" in source
    assert "index < MANUAL_STATES.length" in source


def test_the_stale_button_carries_no_extra_annotation() -> None:
    """用户明确要求不加解释文字。位置本身已经说明问题（第 10 个，没有键）。"""
    html = read("index.html")

    assert "无快捷键" not in html
    assert "没有快捷键" not in html
    # 原来那句提示依然准确，不该被改掉
    assert "数字键 1–9 切换，0 恢复自动" in html


def test_dock_magnification_never_measures_inside_the_move_handler() -> None:
    """十来个按钮 × 每帧 getBoundingClientRect = 强制重排，必卡。
    位置必须提前量好缓存起来。"""
    source = read("assets/app.js")

    paint = source.split("function paintDock()")[1].split("function scheduleDock")[0]
    assert "getBoundingClientRect" not in paint
    # 窄屏 chip 会换行，只算 X 会让另一行同列的按钮跟着放大
    assert "y < box.top || y > box.bottom" in paint
    assert "function measureDock()" in source
    # 逐帧只写 transform，不碰任何布局属性
    assert "button.style.transform = `scale(" in paint
    assert "requestAnimationFrame(paintDock)" in source


def test_dock_magnification_is_gated_on_pointer_and_reduced_motion() -> None:
    """CSS 那个 prefers-reduced-motion 块管不到 JS 逐帧写上去的 transform，
    所以这两道门禁必须在 JS 里显式加。触屏也没有光标可言。"""
    source = read("assets/app.js")

    gate = source.split("const canMagnify =")[1].split(";")[0]
    assert "(hover: hover) and (pointer: fine)" in gate
    assert "(prefers-reduced-motion: reduce)" in gate

def test_the_manual_row_stays_plain_chips() -> None:
    """按钮保持独立 chip（各自有背景和边框）。之前那版毛玻璃底座 + 滑动胶囊
    用户不要，别再长回来。"""
    html = read("index.html")
    css = read("assets/app.css")
    source = read("assets/app.js")

    assert '<div class="chip-row" id="manual-buttons"></div>' in html
    for gone in ("manual-dock", "dock-capsule", "--glass-top", "--glass-edge"):
        assert gone not in css, gone
    for gone in ("capsule", "manual-dock"):
        assert gone not in source, gone


def test_the_manual_heading_sits_on_its_own_line() -> None:
    """加上「状态存疑」之后是 11 个按钮，跟标题挤同一行放不下。"""
    css = read("assets/app.css")

    bar = css.split(".manual-bar {")[1].split("}")[0]
    assert "flex-direction: column" in bar


def test_the_magnification_is_tuned_to_the_gap_between_chips() -> None:
    """这些 chip 有自己的边框，放大过头就会撞在一起。gap 是按**最坏情况实测**定的：
    最宽的 chip 101px，1.15 倍下向两侧各外扩约 7.6px。缝隙 10px 时「等待输入」和
    「请求授权」会重叠 1.56px，14px 才留出约 2.4px 余量。
    改 gap、改 maxScale、或改按钮文案（宽度变了）都要重新量一遍。
    """
    css = read("assets/app.css")
    source = read("assets/app.js")

    assert ".chip-row { display: flex; flex-wrap: wrap; gap: 14px; }" in css
    dock = source.split("const DOCK = {")[1].split("}")[0]
    assert "maxScale: 1.15" in dock
    # radius 必须远大于按钮间距（实测约 96px），否则邻居落在作用域外就没有联动
    assert "radius: 230" in dock
    # 只缩放，不上浮 —— chip 有边框，上下动会显得跳
    assert "transform-origin: center bottom" in css
    assert "lift" not in dock


def test_the_timeline_comes_from_the_backend_not_from_local_sampling() -> None:
    """时间线使用后端记录，避免页面刷新或切换造成数据缺口。"""
    source = read("assets/app.js")

    assert "async function fetchTimeline()" in source
    assert '"/v1/timeline"' in source
    # 启动拉种子 + 切回总览补一次
    assert "await fetchTimeline();" in source
    assert 'if (name === "dashboard") fetchTimeline();' in source


def test_the_timeline_records_on_every_page() -> None:
    """recordTimeline 必须位于页面分支之前，确保所有页面都持续记录。"""
    source = read("assets/app.js")

    render_all = source.split("function renderAll()")[1].split("function showPage")[0]
    gate = render_all.index('if (store.page === "dashboard")')
    assert render_all.index("recordTimeline(") < gate

    dashboard = source.split("function renderDashboard()")[1].split("function renderAll")[0]
    assert "recordTimeline(" not in dashboard


def test_restoring_auto_sits_beside_the_heading() -> None:
    """「恢复自动」不是一个状态，是「退出手动模式」，跟那排状态分开更清楚；
    顺带把 chip 行从 11 个减到 10 个。"""
    html = read("index.html")
    css = read("assets/app.css")
    source = read("assets/app.js")

    head = html.split('<div class="manual-head">')[1].split("</div>")[0]
    assert "<h2>手动状态</h2>" in head
    assert 'id="manual-auto"' in head
    assert ".manual-head { display: flex;" in css
    # aria-pressed 要覆盖挪出去的那个按钮，不能只查 chip 行
    assert 'host.closest(".manual-bar").querySelectorAll("button[data-manual-state]")' in source


def test_the_rest_schedule_is_wired_end_to_end() -> None:
    """三个控件、渲染、提交缺一不可 —— 少一环就是「设置了但不生效」。"""
    html = read("index.html")
    source = read("assets/app.js")
    css = read("assets/app.css")

    for prefix in ("rest-schedule", "mute-schedule"):
        for suffix in ("toggle", "start", "end"):
            assert f'id="{prefix}-{suffix}"' in html, f"{prefix}-{suffix}"
    # 两个时段共用一张表和一套渲染/提交，别再各写一份
    assert "const SCHEDULES = [" in source
    assert '"device_rest_schedule"' in source and '"mute_schedule"' in source
    assert "function renderRestSchedule()" in source
    assert "renderRestSchedule();" in source
    assert "device_rest_schedule" in source
    assert ".rest-schedule {" in css
    # 开关做成和「关闭连接」同款按钮，不是勾选框
    assert 'id="rest-schedule-toggle" type="button" aria-pressed' in read("index.html")
    assert "<h2>状态灯关闭设置</h2>" in read("index.html")
    assert "到点自动关闭连接" not in read("index.html")
    # 蜂鸣器测试整块已按要求移除
    assert "蜂鸣器测试" not in html and 'id="device-play"' not in html
    assert "<h2>蜂鸣器静音设置</h2>" in html


def test_the_schedule_inputs_are_not_clobbered_while_being_edited() -> None:
    """服务端每秒推快照。正在输入时回填，会把你打到一半的时间顶掉。"""
    source = read("assets/app.js")
    block = source.split("function renderRestSchedule()")[1].split("function renderDeviceStatus")[0]

    assert "document.activeElement" in block
    assert 'closest(".rest-schedule")' in block


def test_native_controls_follow_the_theme() -> None:
    """不声明 color-scheme，浏览器一律按浅色画原生控件 —— 深色主题下时间输入的
    数字、箭头和弹出的时钟面板全是白底，这正是它当初难看的原因。"""
    css = read("assets/app.css")

    assert "color-scheme: light;" in css
    assert "color-scheme: dark;" in css
    # 漏掉 time 就会一路裸奔到浏览器默认样式
    assert 'input[type="time"], select {' in css


def test_the_frontend_uses_the_verbs_the_api_actually_registers() -> None:
    """写错动词是静默失败：界面照常提示成功，服务端回 405，设置根本没保存。
    定时休息这条就踩过（写成 POST，实际是 PUT）。"""
    import re
    from pathlib import Path

    api_source = Path(__file__).parents[1].joinpath("src/agentlight/api.py").read_text(encoding="utf-8")
    registered: dict[str, set[str]] = {}
    for verb, path in re.findall(r'web\.(get|post|put|delete)\("([^"]+)"', api_source):
        registered.setdefault(path, set()).add(verb.upper())

    source = read("assets/app.js")
    for verb, path in re.findall(r'api\("(GET|POST|PUT|DELETE)",\s*"(/v1/[^"?]+)"', source):
        if path in registered:
            assert verb in registered[path], f"{verb} {path} 未注册，实际支持 {sorted(registered[path])}"


def test_the_two_schedules_sit_together_above_the_manual_actions() -> None:
    """两个时段设置挨在一起，手动动作（立即休眠 / 关闭连接）沉到底部 ——
    读起来是「先设规则，再放手动操作」。"""
    html = read("index.html")

    assert html.index("<h2>蜂鸣器静音设置</h2>") < html.index("<h2>状态灯关闭设置</h2>") < html.index("<h2>休眠</h2>")


def test_idle_sleep_reuses_the_existing_firmware_setting() -> None:
    """不要为「空闲多久后休眠」再造一套机制 —— device.auto_sleep + sleep_timeout
    已经在做这件事（_sync_runtime_sleep 只在 state==off 或已暂停时放行，
    也就是「没有任务状态」）。这里只是把它换成看得懂的说法。"""
    html = read("index.html")
    source = read("assets/app.js")

    assert 'id="idle-sleep-minutes"' in html
    assert "const IDLE_SLEEP_MINUTES = [5, 10, 15, 20, 25, 30];" in source
    assert "sleep_timeout: minutes * 60" in source          # 界面给分钟，固件收秒
    assert "auto_sleep: enabled" in source


def test_the_old_raw_sleep_controls_are_gone() -> None:
    """两处控同一个值，改了这边那边不动，最后谁也说不清生效的是哪个。"""
    html = read("index.html")
    source = read("assets/app.js")

    for stale in ('id="dev-sleep-timeout"', 'id="dev-auto-sleep"'):
        assert stale not in html, stale
        assert stale.split('"')[1] not in source, stale
    assert "休眠超时（秒）" not in html


def test_idle_sleep_uses_a_dash_option_instead_of_a_toggle() -> None:
    """下拉里的「--」本身就表达不启用，再配一个开关按钮是重复。"""
    html = read("index.html")
    source = read("assets/app.js")
    block = html.split("<h2>休眠</h2>")[1]

    assert 'class="rest-schedule"' in block
    assert "idle-sleep-toggle" not in html and "idle-sleep-toggle" not in source
    assert 'const IDLE_SLEEP_OFF = "";' in source
    assert '"--"' in source
    # 选到「--」就是关闭；用空串而不是 0，免得跟一个真的时长混淆
    assert "const enabled = raw !== IDLE_SLEEP_OFF;" in source


def test_the_three_device_rows_share_one_grid_so_they_line_up() -> None:
    """每行是各自独立的栅格容器，只要有一列是 auto，它就会按各自内容算宽 ——
    结果就是「至」和开关在行与行之间左右错位。所有列都得定宽。"""
    css = read("assets/app.css")

    assert "grid-template-columns: 96px 20px 96px 1fr;" in css
    assert 'input[type="time"] {' in css and "width: 96px;" in css
    # 空闲休眠那行结构不同，靠跨列让开关照样落在第四列
    assert ".rest-schedule-span { grid-column: 1 / 4;" in css


def test_section_spacing_lives_in_css_not_inline_styles() -> None:
    """原来靠 style="margin-top:18px" 一处处手写，改一个值要翻遍 HTML，
    而且第一个分区没有、后面的有，节奏本身就不齐。"""
    html = read("index.html")
    css = read("assets/app.css")

    assert 'style="margin-top:18px"' not in html
    assert ".rest-schedule + .card-head," in css
    assert ".button-row + .card-head," in css


def test_settings_page_carries_the_global_hotkey_controls() -> None:
    html = read("index.html")
    source = read("assets/app.js")

    for element_id in ("set-hotkey", "set-hotkey-enabled", "hotkey-status"):
        assert f'id="{element_id}"' in html, element_id
    # 只在改过时才把 hotkey 发给后端，避免启动时就被占用的组合连累其他设置
    assert "hotkeyChanged ? { hotkey }" in source
    assert "snapshot.hotkey" in source
    # 录完会 blur，焦点保护失效；只在已保存的值变化时同步，否则状态推送会冲掉草稿
    assert "dataset.saved !== savedHotkey" in source
