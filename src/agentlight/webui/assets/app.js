/* Agent 状态灯 · 配置页
 *
 * 灯效渲染在这里独立实现一遍（Python 侧在 effects.py）。效果清单和参数范围由
 * GET /v1/effects 下发，是单一来源；这里只重写渲染数学。tests/test_webui_parity.py
 * 会断言两边的效果 id 一致，防止长期漂移。
 */
(() => {
  "use strict";

  const TOKEN = document.querySelector('meta[name="agentlight-token"]').content;
  const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const PAGE_REFRESH_MS = 30_000;

  const STATES = ["ready", "thinking", "busy", "subagent", "attention", "permission", "error", "done", "stale", "off"];
  // 数字键 1–9 的索引源，顺序即键号。stale 不在里面 —— 键已经用满了，
  // 它只出现在按钮上，拿不到快捷键。改这个数组等于改快捷键映射，别随手动。
  const MANUAL_STATES = STATES.filter((state) => state !== "stale");
  // 按钮渲染用的列表。stale 追加在末尾而不是插在中间：这样前 9 个按钮的位置
  // 严格等于数字键 1–9，第 10 个没有键，数一遍就明白，不用写一行说明。
  const MANUAL_BUTTON_STATES = [...MANUAL_STATES, "stale"];
  const STATE_LABELS = {
    ready: "就绪", thinking: "思考中", busy: "工作中", subagent: "子任务",
    attention: "等待输入", permission: "请求授权", error: "错误", done: "已完成",
    stale: "状态存疑", off: "熄灭",
  };
  const STATE_COLORS = {
    ready: "#22c55e", thinking: "#38bdf8", busy: "#f59e0b", subagent: "#a78bfa",
    attention: "#ef4444", permission: "#f43f5e", error: "#dc2626", done: "#16a34a",
    stale: "#94a3b8", off: "#64748b",
  };
  const SOUNDS = ["stop", "beep", "double-beep", "success", "failure", "alarm"];
  // 常用色，省得每次去调 RGB 数字
  const PRESET_COLORS = [
    ["ff0000", "红"], ["ff8000", "橙"], ["ffd000", "黄"], ["00ff00", "绿"], ["00d0d0", "青"],
    ["0080ff", "蓝"], ["8000ff", "紫"], ["ff00a0", "品红"], ["ffffff", "白"], ["000000", "熄灭"],
  ];
  const SOUND_LABELS = { stop: "无", beep: "单响", "double-beep": "双响", success: "成功音", failure: "失败音", alarm: "警报" };

  const TIMELINE_SLOTS = 120;
  const TIMELINE_WINDOW_MS = 30 * 60 * 1000;

  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------ 灯效渲染

  const LED_COUNT = 3;
  const BLACK = [0, 0, 0];

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const mod = (a, n) => ((a % n) + n) % n;
  const step = (dir) => (dir === "backward" ? -1 : 1);
  const orderedIndex = (i, dir) => (step(dir) > 0 ? i : LED_COUNT - 1 - i);

  function scaleRgb(rgb, intensity) {
    const f = clamp(intensity, 0, 1);
    return [Math.round(rgb[0] * f), Math.round(rgb[1] * f), Math.round(rgb[2] * f)];
  }

  function parseColor(value) {
    const text = String(value || "").replace("#", "").trim();
    if (text.length !== 6) return BLACK;
    const n = Number.parseInt(text, 16);
    return Number.isNaN(n) ? BLACK : [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function hsvToRgb(h, s, v) {
    const i = Math.floor(h * 6);
    const f = h * 6 - i;
    const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
    const table = [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]];
    return table[i % 6].map((c) => Math.round(c * 255));
  }

  const RENDERERS = {
    static: (c) => c.slice(),
    blink: (c, p) => (p < 0.5 ? c.slice() : [BLACK, BLACK, BLACK]),
    breath: (c, p) => {
      const intensity = 0.5 - 0.5 * Math.cos(2 * Math.PI * p);
      return c.map((x) => scaleRgb(x, intensity));
    },
    chase: (c, p, q) => {
      const duty = clamp(q.duty ?? 1, 0.05, 1);
      const slot = p * LED_COUNT;
      const position = Math.floor(slot) % LED_COUNT;
      const lit = slot - Math.floor(slot) < duty;
      const active = orderedIndex(position, q.direction);
      return c.map((x, i) => (i === active && lit ? x : BLACK));
    },
    comet: (c, p, q) => {
      const tail = clamp(q.tail ?? 0.35, 0.05, 0.95);
      const head = p * LED_COUNT;
      const forward = step(q.direction) > 0;
      return c.map((x, i) => {
        const distance = forward ? mod(head - i, LED_COUNT) : mod(i - head, LED_COUNT);
        return scaleRgb(x, Math.pow(tail, distance));
      });
    },
    pulse_seq: (c, p, q) => {
      const spread = clamp(q.spread ?? 1 / 3, 0, 1);
      return c.map((x, i) => {
        const offset = orderedIndex(i, q.direction) * spread;
        return scaleRgb(x, 0.5 - 0.5 * Math.cos(2 * Math.PI * (p - offset)));
      });
    },
    alternate: (c, p, q) => {
      const duty = clamp(q.duty ?? 0.5, 0.05, 0.95);
      const first = p < duty;
      return [first ? c[0] : BLACK, first ? BLACK : c[1], first ? c[2] : BLACK];
    },
    wipe: (c, p, q) => {
      const doubled = p * 2;
      const filling = doubled < 1;
      const progress = (filling ? doubled : doubled - 1) * LED_COUNT;
      return c.map((x, i) => {
        const edge = progress - orderedIndex(i, q.direction);
        return scaleRgb(x, filling ? clamp(edge, 0, 1) : clamp(1 - edge, 0, 1));
      });
    },
    rainbow: (c, p, q) => {
      const spread = clamp(q.spread ?? 1 / 3, 0, 1);
      const saturation = clamp(q.saturation ?? 1, 0, 1);
      const direction = step(q.direction);
      return [0, 1, 2].map((i) => hsvToRgb(mod(p * direction + i * spread, 1), saturation, 1));
    },
  };

  /* 预览要回答的是「哪颗灯亮、什么颜色、什么节奏」。若按设备亮度等比换算，3% 在
     屏幕上就是纯黑（rgb(17,0,0)），颜色完全看不出来。这里保留 0.5 的下限，让亮度
     只做轻微区分——预览不是照度模拟。 */
  function previewGain(brightness) {
    const level = clamp((Number(brightness) || 0) / 100, 0, 1);
    return 0.5 + 0.5 * Math.pow(level, 1 / 2.2);
  }

  function renderFrame(effect, colors, phase, params) {
    const renderer = RENDERERS[effect] || RENDERERS.static;
    const rgb = [0, 1, 2].map((i) => parseColor(colors[i]));
    return renderer(rgb, mod(phase, 1), params || {});
  }

  // ------------------------------------------------------------------ 灯珠

  const strips = new Set();
  let rafId = 0;

  class LampStrip {
    constructor(el) {
      this.el = el;
      this.el.textContent = "";
      this.lamps = [];
      for (let i = 0; i < LED_COUNT; i += 1) {
        const lamp = document.createElement("span");
        lamp.className = "lamp";
        const glow = document.createElement("i");
        glow.className = "lamp-glow";
        const core = document.createElement("i");
        core.className = "lamp-core";
        lamp.append(glow, core);
        this.el.append(lamp);
        this.lamps.push({ core, glow, lastFill: "", lastGlow: "", lastOpacity: -1 });
      }
      this.source = null;
      strips.add(this);
    }

    destroy() {
      strips.delete(this);
    }

    render(nowMs) {
      const spec = this.source && this.source();
      if (!spec) return;
      const period = Math.max(50, spec.periodMs || 1000);
      // 减弱动态效果时停在最亮相位，而不是让灯闪个不停
      const phase = REDUCED_MOTION ? 0.25 : (nowMs / period) % 1;
      const frame = renderFrame(spec.effect, spec.colors, phase, spec.params);

      const gain = previewGain(spec.brightness);

      for (let i = 0; i < LED_COUNT; i += 1) {
        const lamp = this.lamps[i];
        const [r, g, b] = scaleRgb(frame[i] || BLACK, gain);
        const lit = r + g + b > 12;
        const fill = lit ? `rgb(${r},${g},${b})` : "";
        if (fill !== lamp.lastFill) {
          lamp.core.style.backgroundColor = fill;
          lamp.lastFill = fill;
        }
        const glowColor = lit ? `rgba(${r},${g},${b},.85)` : "";
        if (glowColor !== lamp.lastGlow) {
          lamp.glow.style.setProperty("--glow", glowColor);
          lamp.lastGlow = glowColor;
        }
        const opacity = lit ? clamp(((r + g + b) / 765) * 1.3, 0, 1) : 0;
        if (Math.abs(opacity - lamp.lastOpacity) > 0.01) {
          lamp.glow.style.opacity = opacity.toFixed(2);
          lamp.lastOpacity = opacity;
        }
      }
    }
  }

  function tick(now) {
    for (const strip of strips) strip.render(now);
    rafId = requestAnimationFrame(tick);
  }
  function startLoop() { if (!rafId) rafId = requestAnimationFrame(tick); }
  function stopLoop() { if (rafId) { cancelAnimationFrame(rafId); rafId = 0; } }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopLoop();
    } else {
      startLoop();
      refreshPageData();
    }
  });

  // ------------------------------------------------------------------ 状态

  const store = {
    snapshot: null,
    config: null,
    effects: [],
    effectMap: {},
    autostart: false,
    editing: "ready",
    draft: null,
    timeline: [],
    page: "dashboard",
    lamp: 0,
    quota: null,
    quotaUpdatedAt: 0,
    quotaSource: "all",
    quotaTimer: 0,
    quotaFollowupTimer: 0,
    quotaFollowupAttempts: 0,
    quotaRefreshing: false,
    quotaIntervalSeconds: null,
    logTimer: 0,
    pageRefreshTimer: 0,
    pageRefreshing: false,
    apiPort: 0,
  };

  async function api(method, path, body) {
    const headers = { Authorization: `Bearer ${TOKEN}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let data = {};
    if (text) { try { data = JSON.parse(text); } catch { data = {}; } }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  let feedbackTimer = 0;
  function say(message, bad) {
    const el = $("feedback");
    el.textContent = message;
    el.dataset.tone = bad ? "bad" : "ok";
    el.classList.add("is-shown");
    clearTimeout(feedbackTimer);
    feedbackTimer = setTimeout(() => el.classList.remove("is-shown"), 3600);
  }

  async function run(action, okMessage) {
    try {
      const result = await action();
      if (okMessage) say(okMessage);
      return result;
    } catch (error) {
      say(String(error.message || error), true);
      return null;
    }
  }

  const profileOf = (state) => (store.config && store.config.profiles[state]) || null;

  function specOf(effect) {
    return store.effectMap[effect] || store.effectMap.static;
  }

  function defaultParams(effect) {
    const spec = specOf(effect);
    const values = {};
    if (spec) for (const param of spec.params) values[param.key] = param.default;
    return values;
  }

  // ------------------------------------------------------------------ 总览

  function relativeTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    if (seconds < 60) return `${Math.floor(seconds)} 秒`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
    return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
  }

  function stateTag(state) {
    const tag = document.createElement("span");
    tag.className = "state-tag";
    tag.style.color = STATE_COLORS[state] || "var(--text-dim)";
    const dot = document.createElement("i");
    tag.append(dot, document.createTextNode(STATE_LABELS[state] || state));
    return tag;
  }

  let heroStrip = null;
  let timelineSlots = [];

  function buildTimeline() {
    const host = $("timeline");
    host.textContent = "";
    timelineSlots = [];
    for (let i = 0; i < TIMELINE_SLOTS; i += 1) {
      const slot = document.createElement("div");
      slot.className = "timeline-slot";
      host.append(slot);
      timelineSlots.push({ el: slot, last: "" });
    }
    const legend = $("timeline-legend");
    legend.textContent = "";
    for (const state of STATES) {
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.style.background = STATE_COLORS[state];
      item.append(swatch, document.createTextNode(STATE_LABELS[state]));
      legend.append(item);
    }
  }

  // 时间线的权威数据来自后端：它知道每一次状态变化，而且不会因为你刷新页面、
  // 或者切到别的页签就断片。前端只做两件事 —— 启动时拉一份，之后跟着实时快照追加。
  async function fetchTimeline() {
    const data = await run(() => api("GET", "/v1/timeline"));
    if (!data || !Array.isArray(data.entries)) return;
    store.timeline = data.entries.map((item) => ({ t: item.at * 1000, state: item.state }));
    paintTimeline();
  }

  function recordTimeline(state) {
    const now = Date.now();
    const last = store.timeline[store.timeline.length - 1];
    // 只记「变化」，和后端一致。每秒推一条相同的状态既没信息量，又会把 30 分钟
    // 窗口撑成 1800 条。
    if (last && last.state === state) return;
    store.timeline.push({ t: now, state });
    // 留一条窗口外的作为基线，否则状态长时间不变时整条线会空掉
    const cutoff = now - TIMELINE_WINDOW_MS;
    while (store.timeline.length > 1 && store.timeline[1].t < cutoff) store.timeline.shift();
  }

  function paintTimeline() {
    const now = Date.now();
    const slotMs = TIMELINE_WINDOW_MS / TIMELINE_SLOTS;
    const buckets = new Array(TIMELINE_SLOTS).fill(null);
    for (const sample of store.timeline) {
      const index = Math.floor((sample.t - (now - TIMELINE_WINDOW_MS)) / slotMs);
      if (index >= 0 && index < TIMELINE_SLOTS) buckets[index] = sample.state;
    }
    let carried = null;
    for (let i = 0; i < TIMELINE_SLOTS; i += 1) {
      if (buckets[i]) carried = buckets[i];
      const color = buckets[i] || carried ? STATE_COLORS[buckets[i] || carried] : "";
      const slot = timelineSlots[i];
      if (slot && slot.last !== color) {
        slot.el.style.background = color;
        slot.last = color;
      }
    }
  }

  function renderDashboard() {
    const snap = store.snapshot;
    if (!snap) return;
    const effective = snap.state.effective;
    const sessions = snap.state.sessions || [];

    $("stat-state").textContent = STATE_LABELS[effective.state] || effective.state;
    $("stat-state").style.color = STATE_COLORS[effective.state] || "";
    $("stat-source").textContent = effective.manual
      ? "手动控制"
      : `${effective.source || "系统"}${effective.session_id ? ` · ${effective.session_id}` : ""}`;

    const waiting = sessions
      .filter((item) => ["attention", "permission", "error"].includes(item.state))
      .sort((a, b) => a.updated_at - b.updated_at);
    const oldest = waiting[0];
    $("stat-wait").textContent = oldest ? relativeTime(Date.now() / 1000 - oldest.updated_at) : "—";
    $("stat-wait-sub").textContent = oldest
      ? `${oldest.source} 已等待你处理`
      : "没有需要你处理的会话";

    const counts = {};
    for (const item of sessions) counts[item.state] = (counts[item.state] || 0) + 1;
    $("stat-sessions").textContent = String(sessions.length);
    $("stat-sessions-sub").textContent = sessions.length
      ? STATES.filter((s) => counts[s]).map((s) => `${STATE_LABELS[s]} ${counts[s]}`).join(" · ")
      : "暂无活动会话";

    const device = snap.device || {};
    const runningEffect = device.animating ? device.effect : (profileOf(effective.state) || {}).effect;
    const spec = specOf(runningEffect || "static");
    $("stat-effect").textContent = spec ? spec.label : "常亮";
    $("stat-effect-sub").textContent = device.animating
      ? `软件推帧 · ${device.frame_rate} fps`
      : "固件原生";

    const lastOff = snap.state.last_off;
    $("last-off").textContent = lastOff ? `最近一次熄灯：${lastOff.code}` : "";

    // 关注队列
    const list = $("attention-list");
    list.textContent = "";
    $("attention-count").textContent = String(waiting.length);
    $("attention-count").dataset.tone = waiting.length ? "bad" : "";
    if (!waiting.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "当前没有需要你介入的会话。";
      list.append(empty);
    } else {
      for (const item of waiting) {
        const row = document.createElement("div");
        row.className = "attention-item";
        row.style.borderLeftColor = STATE_COLORS[item.state];
        const left = document.createElement("div");
        const who = document.createElement("div");
        who.className = "who";
        who.textContent = `${item.source} · ${STATE_LABELS[item.state]}`;
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = item.session_id;
        left.append(who, meta);
        const age = document.createElement("div");
        age.className = "age";
        age.style.color = STATE_COLORS[item.state];
        age.textContent = relativeTime(Date.now() / 1000 - item.updated_at);
        row.append(left, age);
        list.append(row);
      }
    }

    // 会话表
    const body = $("sessions-body");
    body.textContent = "";
    if (!sessions.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "empty";
      cell.textContent = "暂无活动会话。Codex 或 Claude 发起任务后会出现在这里。";
      row.append(cell);
      body.append(row);
    }
    for (const item of sessions) {
      const row = document.createElement("tr");
      const source = document.createElement("td");
      source.textContent = item.source;
      const session = document.createElement("td");
      session.textContent = item.session_id;
      const state = document.createElement("td");
      state.append(stateTag(item.state));
      const when = document.createElement("td");
      when.textContent = new Date(item.updated_at * 1000).toLocaleTimeString();
      const actions = document.createElement("td");
      const remove = document.createElement("button");
      remove.className = "ghost small";
      remove.textContent = "移除";
      remove.addEventListener("click", () => run(
        () => api("DELETE", `/v1/sessions/${encodeURIComponent(item.source)}/${encodeURIComponent(item.session_id)}`),
        "✓ 会话已移除",
      ));
      actions.append(remove);
      row.append(source, session, state, when, actions);
      body.append(row);
    }

    paintTimeline();
  }

  // ------------------------------------------------------------------ 灯效方案

  let editorStrip = null;
  const previewStrips = [];

  function draftSpec() {
    const draft = store.draft;
    if (!draft) return null;
    return {
      effect: draft.effect,
      colors: draft.colors,
      params: draft.effect_params,
      periodMs: draft.period_ms,
      brightness: draft.brightness,
    };
  }

  function loadDraft(state) {
    const profile = profileOf(state);
    if (!profile) return;
    store.editing = state;
    store.draft = {
      colors: profile.colors.slice(),
      effect: profile.effect || profile.mode || "static",
      effect_params: { ...defaultParams(profile.effect), ...(profile.effect_params || {}) },
      brightness: profile.brightness,
      period_ms: profile.period_ms,
      sound: profile.sound,
    };
    syncEditorInputs();
  }

  function selectLamp(index) {
    store.lamp = index;
    for (const cell of $("field-colors").children) {
      cell.classList.toggle("is-target", Number(cell.dataset.index) === index);
    }
  }

  function stateDot(state) {
    const dot = document.createElement("i");
    dot.className = "state-dot";
    dot.style.background = STATE_COLORS[state];
    return dot;
  }

  function buildStatePicker() {
    const picker = $("state-picker");
    picker.textContent = "";
    for (const state of STATES) {
      const button = document.createElement("button");
      button.type = "button";
      button.append(stateDot(state), document.createTextNode(STATE_LABELS[state]));
      button.dataset.state = state;
      button.addEventListener("click", () => { loadDraft(state); refreshPickerActive(); });
      picker.append(button);
    }
  }

  function refreshPickerActive() {
    for (const button of $("state-picker").children) {
      button.classList.toggle("is-active", button.dataset.state === store.editing);
    }
    for (const card of $("preview-grid").children) {
      card.classList.toggle("is-active", card.dataset.effect === (store.draft || {}).effect);
    }
  }

  function buildEffectControls() {
    const select = $("field-effect");
    select.textContent = "";
    for (const spec of store.effects) {
      const option = document.createElement("option");
      option.value = spec.id;
      option.textContent = `${spec.label}（${spec.kind === "software" ? "软件" : "固件"}）`;
      select.append(option);
    }

    const sound = $("field-sound");
    sound.textContent = "";
    for (const value of SOUNDS) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = SOUND_LABELS[value];
      sound.append(option);
    }

    const colors = $("field-colors");
    colors.textContent = "";
    for (let i = 0; i < LED_COUNT; i += 1) {
      const cell = document.createElement("label");
      cell.className = "color-cell";
      cell.dataset.index = String(i);
      const input = document.createElement("input");
      input.type = "color";
      input.dataset.index = String(i);
      input.addEventListener("input", () => {
        store.draft.colors[i] = input.value.replace("#", "");
      });
      // 点哪颗就选中哪颗，下面那排常用色作用在它上面
      cell.addEventListener("click", () => selectLamp(i));
      const caption = document.createElement("span");
      caption.textContent = `第 ${i + 1} 颗`;
      cell.append(input, caption);
      colors.append(cell);
    }

    const presets = $("color-presets");
    presets.textContent = "";
    for (const [hex, name] of PRESET_COLORS) {
      const swatch = document.createElement("button");
      swatch.type = "button";
      swatch.className = "swatch";
      swatch.style.background = `#${hex}`;
      swatch.title = `${name} #${hex}`;
      swatch.addEventListener("click", () => {
        store.draft.colors[store.lamp] = hex;
        const input = colors.querySelector(`input[data-index="${store.lamp}"]`);
        if (input) input.value = `#${hex}`;
      });
      presets.append(swatch);
    }
    selectLamp(store.lamp);

    const grid = $("preview-grid");
    grid.textContent = "";
    previewStrips.length = 0;
    for (const spec of store.effects) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "preview-card";
      card.dataset.effect = spec.id;

      const text = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = spec.label;
      const kind = document.createElement("em");
      kind.className = "kind";
      kind.textContent = spec.kind === "software" ? "软件推帧" : "固件原生";
      // 卡片右边的灯珠已经把「用不用档案颜色」演示出来了，再写一行是重复
      text.append(title, kind);

      const lamps = document.createElement("div");
      lamps.className = "lamps";
      card.append(text, lamps);
      card.title = spec.description;
      card.addEventListener("click", () => {
        store.draft.effect = spec.id;
        store.draft.effect_params = defaultParams(spec.id);
        syncEditorInputs();
      });
      grid.append(card);

      const strip = new LampStrip(lamps);
      strip.source = () => {
        const base = draftSpec();
        if (!base) return null;
        return { ...base, effect: spec.id, params: defaultParams(spec.id) };
      };
      previewStrips.push(strip);
    }
  }

  function renderEffectParams() {
    const host = $("effect-params");
    host.textContent = "";
    const spec = specOf(store.draft.effect);
    if (!spec || !spec.params.length) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    for (const param of spec.params) {
      const wrap = document.createElement("label");
      wrap.className = "field";
      const label = document.createElement("span");
      wrap.append(label);

      if (param.type === "enum") {
        label.textContent = param.label;
        const select = document.createElement("select");
        for (const choice of param.choices) {
          const option = document.createElement("option");
          option.value = choice;
          option.textContent = choice === "backward" ? "反向" : "正向";
          select.append(option);
        }
        select.value = store.draft.effect_params[param.key] ?? param.default;
        select.addEventListener("change", () => { store.draft.effect_params[param.key] = select.value; });
        wrap.append(select);
      } else {
        const output = document.createElement("b");
        const input = document.createElement("input");
        input.type = "range";
        input.min = String(param.minimum ?? 0);
        input.max = String(param.maximum ?? 1);
        input.step = "0.01";
        input.value = String(store.draft.effect_params[param.key] ?? param.default);
        const show = () => { output.textContent = Number(input.value).toFixed(2); };
        input.addEventListener("input", () => {
          store.draft.effect_params[param.key] = Number(input.value);
          show();
        });
        show();
        label.textContent = `${param.label} `;
        label.append(output);
        wrap.append(input);
      }
      if (param.help) wrap.title = param.help;
      host.append(wrap);
    }
  }

  function syncEditorInputs() {
    const draft = store.draft;
    if (!draft) return;
    $("field-effect").value = draft.effect;
    $("field-sound").value = draft.sound;
    $("field-brightness").value = String(draft.brightness);
    $("field-period").value = String(clamp(draft.period_ms, 100, 5000));
    $("out-brightness").textContent = `${draft.brightness}%`;
    $("out-period").textContent = `${(draft.period_ms / 1000).toFixed(2)} 秒`;
    for (const input of $("field-colors").querySelectorAll("input[type=color]")) {
      input.value = `#${draft.colors[Number(input.dataset.index)] || "000000"}`;
    }
    const spec = specOf(draft.effect);
    $("editor-caption").textContent = spec
      ? `${spec.description} 预览侧重颜色与节奏，不等比反映实际亮度。`
      : "";
    renderEffectParams();
    refreshPickerActive();
  }

  // ------------------------------------------------------------------ 其他页面

  async function renderIntegrations() {
    // 广播的快照不带集成状态（它每次都要读两个磁盘文件），所以这里按需拉取
    const integrations = await run(() => api("GET", "/v1/integrations"));
    if (!integrations) return;
    const describe = (value) => {
      if (!value) return "未知";
      if (value.error) return `读取失败：${value.error}`;
      if (!value.installed) return "未安装";
      return value.path_matches === false ? "安装路径已变化" : "已安装";
    };
    $("codex-state").textContent = describe(integrations.codex);
    $("claude-state").textContent = describe(integrations.claude);
    // Codex 审核的是 PowerShell 脚本，Claude 用的是 exe —— 这两条路径不一样，
    // 之前两边都写 ctl_path，用户在 /hooks 里根本对不上号。
    const codex = integrations.codex || {};
    const claude = integrations.claude || {};
    if (codex.hook_path) $("codex-hook-path").textContent = codex.hook_path;
    if (claude.ctl_path) $("claude-statusline-path").textContent = `"${claude.ctl_path}" statusline`;
  }

  // 重置时间要短到能塞进环形图下面。周几用英文简写，一周之内光看 Sat 就够定位，
  // 超过一周才补日期（Codex 的多日窗口会走到这一支）。
  function formatReset(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "";
    const at = new Date(seconds * 1000);
    const weekday = at.toLocaleDateString("en-US", { weekday: "short" });
    const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const days = Math.floor((at - new Date()) / 86400000);
    const date = days >= 6 ? ` ${at.getMonth() + 1}/${at.getDate()}` : "";
    return `${weekday}${date} ${time} 重置`;
  }

  // 这一行不是实时刷新的，写「x 小时 y 分前」会随着页面停留越读越不准。
  // 直接给绝对时间：今天只给时刻，跨天补上日期。
  function formatMoment(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "";
    const at = new Date(seconds * 1000);
    const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    if (at.toDateString() === new Date().toDateString()) return time;
    return `${at.getMonth() + 1}/${at.getDate()} ${time}`;
  }

  function dial(label, value, size = 76, sub = "") {
    const wrap = document.createElement("div");
    wrap.className = "dial";
    const known = Number.isFinite(value) && value >= 0 && value <= 100;
    const radius = (size - 20) / 2;
    const circumference = 2 * Math.PI * radius;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", String(size));
    svg.setAttribute("height", String(size));
    svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
    const track = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    const arc = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    for (const circle of [track, arc]) {
      circle.setAttribute("cx", String(size / 2));
      circle.setAttribute("cy", String(size / 2));
      circle.setAttribute("r", String(radius));
      circle.setAttribute("fill", "none");
      circle.setAttribute("stroke-width", size >= 88 ? "7" : "6");
    }
    track.setAttribute("class", "track");
    arc.setAttribute("class", "value");
    arc.setAttribute("stroke-dasharray", String(circumference));
    arc.setAttribute("stroke-dashoffset", String(circumference * (1 - (known ? value : 0) / 100)));
    // 传进来的是「剩余」百分比，所以是越低越危险
    if (known && value <= 15) arc.style.stroke = "var(--danger)";
    else if (known && value <= 40) arc.style.stroke = "var(--warn)";
    svg.append(track, arc);

    const text = document.createElement("div");
    text.className = "dial-text";
    text.textContent = known ? `${value}%` : "—";
    const caption = document.createElement("div");
    caption.className = "dial-label";
    caption.textContent = label;
    wrap.append(svg, text, caption);
    if (sub) {
      const reset = document.createElement("div");
      reset.className = "dial-sub";
      reset.textContent = sub;
      wrap.append(reset);
    }
    return wrap;
  }

  function buildQuotaPicker(id) {
    const picker = $(id);
    if (picker.children.length) return;
    for (const [key, label] of [["all", "All"], ["codex", "Codex"], ["claude", "Claude"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.dataset.source = key;
      button.addEventListener("click", () => {
        store.quotaSource = key;
        paintQuota();
      });
      picker.append(button);
    }
  }

  // 两处面板共用一套渲染，尺寸也共用：Codex 和 Claude 必须看起来一样大。
  // 为环形图下方的重置时间预留空间。
  // 两张卡都只放环形图：token 用量对判断「还能用多久」没有帮助，去掉。
  const QUOTA_PANELS = [
    { picker: "dash-quota-source", dials: "dash-quota-dials", usage: "dash-quota-usage", updated: "dash-quota-updated", dialSize: 58 },
    { picker: "quota-source", dials: "quota-dials", usage: "quota-usage", note: "quota-note", dialSize: 76 },
  ];

  // Claude 的 5h 与 Weekly 是固定两格，没采到也留占位 —— 空着比消失更好懂，
  // 也让两个来源的排版对得上。多出来的窗口（Opus/Fable 周额度）排在后面。
  const CLAUDE_FIXED_WINDOWS = [["five_hour", "5h"], ["seven_day", "Weekly"]];

  // 只有这两个状态代表「屏幕上的数字就是刚取回来的」。其余是上一次成功的缓存；
  // 数字保持正常配色，但环下方仍写明缓存时间，避免误解数据的新鲜度。
  const CLAUDE_LIVE_STATUSES = new Set(["connected", "refreshing", "statusline"]);

  function quotaDials(source, data) {
    const out = [];
    if (source !== "claude") {
      const windows = (data.codex || {}).windows || [];
      // 窗口名字由 window_minutes 推出，不能靠 primary/secondary 的位置去猜
      for (const w of windows) out.push({ origin: "Codex", label: w.label, value: w.remaining_percent, resetsAt: w.resets_at });
      if (!windows.length) out.push({ origin: "Codex", label: "配额", value: null, stale: !(data.codex || {}).available });
    }
    if (source !== "codex") {
      const claude = data.claude || {};
      const windows = claude.quota_windows || [];
      const stale = !CLAUDE_LIVE_STATUSES.has(claude.status);
      // 环下面那行：数字是新的就写重置时间，是缓存就写它到底是什么时候的
      const sub = stale ? `缓存 ${formatMoment(claude.quota_updated_at) || "时间未知"}` : null;
      const byKey = new Map(windows.map((w) => [w.key, w]));
      for (const [key, label] of CLAUDE_FIXED_WINDOWS) {
        const w = byKey.get(key);
        out.push({ origin: "Claude", label, value: w ? w.remaining_percent : null, resetsAt: w && w.resets_at, stale, sub });
      }
      for (const w of windows) {
        if (CLAUDE_FIXED_WINDOWS.some(([key]) => key === w.key)) continue;
        // Anthropic 的未公开内部桶，没有重置时间或可操作额度，不展示给用户。
        if (w.key === "nimbus_quill") continue;
        out.push({ origin: "Claude", label: w.label, value: w.remaining_percent, resetsAt: w.resets_at, stale, sub });
      }
    }
    return out;
  }

  // 手动刷新后根据每个来源的实际结果显示成功或失败。
  function quotaOutcome(source, data) {
    const claude = data && data.claude;
    if (source !== "codex" && claude && !CLAUDE_LIVE_STATUSES.has(claude.status)) {
      const when = formatMoment(claude.quota_updated_at);
      const detail = claude.error || claude.reason || "刷新失败";
      return { ok: false, text: `Claude 配额没取到：${detail}。显示的仍是${when ? ` ${when} 的` : ""}缓存` };
    }
    const codex = data && data.codex;
    if (source !== "claude" && codex && !codex.available) {
      return { ok: false, text: `Codex 配额没取到：${codex.error || "未找到本地快照"}` };
    }
    return { ok: true, text: "✓ Agent 配额已刷新" };
  }

  // 取不到新数字时该说的话。credential_stale 是最常见的一种，而且它有明确解法，
  // 所以直接把解法写出来，别让人对着「凭据过期」四个字发愣。
  const CLAUDE_TROUBLE = {
    credential_stale: "Claude 登录已过期 —— 在终端运行 claude 重新登录，之后会自动恢复",
    auth_required: "Claude 尚未登录 —— 在终端运行 claude 登录后即可读取",
    offline: "Claude 网络不可用，下面是缓存",
    rate_limited: "Claude 请求过于频繁，下面是缓存",
  };

  // 返回若干行，由调用方各占一行渲染。挤成一行读起来费劲。
  function quotaNote(source, data) {
    const lines = [];
    if (source !== "claude") {
      const codex = data.codex || {};
      lines.push(codex.available
        ? `Codex ${(codex.windows || []).map((w) => `${w.label} 已用 ${w.used_percent}%`).join("，")}`
        : `Codex：${codex.error || "未找到本地配额快照"}`);
    }
    if (source !== "codex") {
      const claude = data.claude || {};
      const trouble = CLAUDE_TROUBLE[claude.status];
      if (trouble) {
        lines.push(trouble);
      } else if (claude.quota_updated_at) {
        // statusline 来的数字同样是实时的，只是不经 OAuth，所以不写「原生 OAuth」
        const origin = claude.quota_source === "native_oauth" ? "Claude 原生 OAuth" : "Claude";
        const moment = formatMoment(claude.quota_updated_at);
        lines.push(`${origin}${moment ? `，更新于 ${moment}` : ""}`);
      } else {
        lines.push(`Claude：${claude.reason || "暂无可读数据"}`);
      }
    }
    return lines.filter(Boolean);
  }

  function paintQuotaPanel(panel, data) {
    buildQuotaPicker(panel.picker);
    for (const button of $(panel.picker).children) {
      const active = button.dataset.source === store.quotaSource;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    }

    const source = store.quotaSource;
    const dials = $(panel.dials);
    dials.textContent = "";
    $(panel.usage).textContent = "";

    const items = quotaDials(source, data);
    // 只有 All 模式才需要标出来源，单来源时前缀纯属噪音
    for (const item of items) {
      const label = source === "all" ? `${item.origin} ${item.label}` : item.label;
      dials.append(dial(label, item.value, panel.dialSize, item.sub || formatReset(item.resetsAt)));
    }
    dials.classList.toggle("dial-row-dense", items.length > 2);

    if (panel.updated) {
      const updated = $(panel.updated);
      updated.textContent = `更新时间：${formatMoment(store.quotaUpdatedAt) || "—"}`;
    }

    if (panel.note) {
      const note = $(panel.note);
      note.textContent = "";
      for (const line of quotaNote(source, data)) {
        const row = document.createElement("div");
        row.textContent = line;
        note.append(row);
      }
    }
  }

  function paintQuota() {
    if (!store.quota) return;
    for (const panel of QUOTA_PANELS) paintQuotaPanel(panel, store.quota);
  }

  function setQuotaRefreshBusy(busy) {
    for (const id of ["dash-refresh-quota", "sync-quota"]) {
      const button = $(id);
      if (!button) continue;
      button.disabled = busy;
      button.setAttribute("aria-busy", String(busy));
    }
  }

  async function renderQuota(refresh, fetchSnapshot = false) {
    const needsFetch = refresh || fetchSnapshot || !store.quota;
    if (needsFetch && store.quotaRefreshing) return false;
    if (needsFetch) {
      const requestedSource = store.quotaSource;
      store.quotaRefreshing = true;
      setQuotaRefreshBusy(true);
      try {
        let data;
        if (refresh && requestedSource === "all") {
          // Codex 是本地快照，先单独刷新并立刻绘制；不要让 Claude 的代理请求拖住它。
          data = await run(() => api("GET", "/v1/quota?refresh=1&source=codex"));
          if (!data) return false;
          store.quota = data;
          store.quotaUpdatedAt = Math.floor(Date.now() / 1000);
          paintQuota();
          data = await run(() => api("GET", "/v1/quota?refresh=1&source=claude"));
        } else {
          const query = refresh ? `?refresh=1&source=${encodeURIComponent(requestedSource)}` : "";
          data = await run(() => api("GET", `/v1/quota${query}`));
        }
        if (!data) return false;
        store.quota = data;
        store.quotaUpdatedAt = Math.floor(Date.now() / 1000);
        clearTimeout(store.quotaFollowupTimer);
        store.quotaFollowupTimer = 0;
        const claudeStatus = data.claude && data.claude.status;
        if (requestedSource !== "codex" && (claudeStatus === "cached" || claudeStatus === "refreshing") && store.quotaFollowupAttempts < 5) {
          // 启动时先画缓存，再短轮询一次后台结果；不会绕过后端 60 秒请求冷却。
          store.quotaFollowupAttempts += 1;
          store.quotaFollowupTimer = setTimeout(() => renderQuota(true), 1200);
        } else if (claudeStatus !== "cached" && claudeStatus !== "refreshing") {
          store.quotaFollowupAttempts = 0;
        }
      } finally {
        store.quotaRefreshing = false;
        setQuotaRefreshBusy(false);
      }
    }
    paintQuota();
    return true;
  }

  function scheduleQuotaRefresh() {
    const configured = Number(store.config && store.config.quota_refresh_interval_seconds);
    let seconds = 0;
    if (Number.isFinite(configured) && configured > 0) {
      seconds = Math.max(15, Math.min(86400, Math.floor(configured)));
    }
    // pullConfig 每 30 秒同步一次页面数据。配置没变时不要重建计时器，否则较长的
    // 配额周期会不断从零开始，永远等不到真正执行。
    if (store.quotaIntervalSeconds === seconds && (seconds === 0 || store.quotaTimer)) return;
    clearInterval(store.quotaTimer);
    store.quotaTimer = 0;
    store.quotaIntervalSeconds = seconds;
    if (seconds === 0) return;
    // 下限 15 秒：Codex 每次刷新要倒序扫会话文件（实测约 20ms），而 Claude 本来
    // 就被服务端锁在 60 秒。1 秒一跑只是让 Codex 空转，换不来任何新数字。
    // 手动点「刷新」走的是另一条路，不受这个下限影响。
    store.quotaTimer = setInterval(() => {
      if (!document.hidden) renderQuota(true);
    }, seconds * 1000);
  }

  function renderDevice() {
    const snap = store.snapshot;
    const config = store.config;
    if (!snap || !config) return;
    const status = (snap.device && snap.device.status) || {};


    $("device-detail").textContent = snap.device && snap.device.status
      ? JSON.stringify(snap.device.status, null, 2)
      : `未连接${snap.device && snap.device.error ? `：${snap.device.error}` : ""}`;

    if (store.pageRefreshing && store.page === "device") return;
    if (document.activeElement && document.activeElement.closest && document.activeElement.closest(".page[data-page=device]")) return;
    const device = config.device;
    $("dev-base").value = String(device.base_brightness);
    $("dev-max").value = String(device.max_brightness);
    $("dev-frame").value = String(device.frame_rate);
    $("out-base").textContent = `${device.base_brightness}%`;
    $("out-max").textContent = `${device.max_brightness}%`;
    $("out-frame").textContent = `${device.frame_rate} fps`;

    $("dev-rotation").value = String(device.screen_rotation);
    $("dev-sound-style").value = device.sound_style;
    $("dev-quota-interval").value = String(device.quota_sync_interval);

    $("dev-keep-5v").checked = Boolean(device.keep_5v);
    $("dev-quota-sync").checked = Boolean(device.codex_quota_sync);
  }

  function renderHotkeyStatus() {
    const error = store.snapshot && store.snapshot.hotkey ? store.snapshot.hotkey.error : null;
    const status = $("hotkey-status");
    status.textContent = error || "";
    status.hidden = !error;
  }

  // 只认 Ctrl/Alt/Win + 字母、数字或 F1–F24，和后端 hotkey.parse_combo 的规则一致
  function hotkeyFromEvent(event) {
    const modifiers = [];
    if (event.ctrlKey) modifiers.push("Ctrl");
    if (event.altKey) modifiers.push("Alt");
    if (event.shiftKey) modifiers.push("Shift");
    if (event.metaKey) modifiers.push("Win");
    const code = event.code || "";
    let key = null;
    if (/^Key[A-Z]$/.test(code)) key = code.slice(3);
    else if (/^Digit[0-9]$/.test(code)) key = code.slice(5);
    else if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) key = code;
    const strong = event.ctrlKey || event.altKey || event.metaKey;
    return { preview: [...modifiers, key || "…"].join("+"), combo: key && strong ? [...modifiers, key].join("+") : null };
  }

  function renderSettings() {
    const config = store.config;
    if (!config) return;
    renderHotkeyStatus();
    if (store.pageRefreshing && store.page === "settings") return;
    if (document.activeElement && document.activeElement.closest && document.activeElement.closest(".page[data-page=settings]")) return;
    $("set-ttl").value = String(config.default_ttl_seconds);
    $("set-hold").value = String(config.done_hold_seconds);
    $("set-reapply").value = String(config.force_reapply_seconds);
    $("set-manual-timeout").value = String(config.manual_timeout_seconds);
    $("set-stale-after").value = String(config.stale_after_seconds);
    $("set-quota-refresh-interval").value = String(config.quota_refresh_interval_seconds ?? 300);
    $("set-api-port").value = String(config.api.port);
    const scale = (config.night_mode || {}).scale || 35;
    $("set-night-scale").value = String(scale);
    $("out-night").textContent = `${scale}%`;
    $("set-autostart").checked = Boolean(store.autostart);
    // 录完会主动 blur，上面的焦点保护就拦不住了；状态推送一来就会把没保存的
    // 草稿冲回旧值。所以只在已保存的值真的变了（如保存成功）时才同步。
    const hotkey = config.hotkey || { enabled: false, combo: "" };
    const savedHotkey = `${Boolean(hotkey.enabled)}|${hotkey.combo}`;
    if ($("set-hotkey").dataset.saved !== savedHotkey) {
      $("set-hotkey").dataset.saved = savedHotkey;
      $("set-hotkey").value = hotkey.combo;
      $("set-hotkey").dataset.combo = hotkey.combo;
      $("set-hotkey-enabled").checked = Boolean(hotkey.enabled);
    }

    const grid = $("priority-grid");
    if (grid.children.length !== STATES.length) {
      grid.textContent = "";
      for (const state of STATES) {
        const field = document.createElement("label");
        field.className = "field";
        const label = document.createElement("span");
        label.textContent = STATE_LABELS[state];
        const input = document.createElement("input");
        input.type = "number";
        input.min = "0";
        input.max = "1000";
        input.dataset.state = state;
        field.append(label, input);
        grid.append(field);
      }
    }
    for (const input of grid.querySelectorAll("input")) {
      input.value = String(config.priorities[input.dataset.state]);
    }
  }

  function paintManualControls(selectedOverride) {
    const host = $("manual-buttons");
    if (!host) return;
    const activeManual = store.snapshot && store.snapshot.state && store.snapshot.state.manual;
    const selected = selectedOverride || (activeManual ? activeManual.state : "auto");
    // 查整张卡而不是只查 chip 行 —— 「恢复自动」已经挪到标题那一行了
    for (const button of host.closest(".manual-bar").querySelectorAll("button[data-manual-state]")) {
      button.setAttribute("aria-pressed", String(button.dataset.manualState === selected));
    }
  }

  // ---------------------------------------------------------------- 邻近放大

  // 鼠标越近越大，衰减是平滑的，所以相邻按钮自然拿到中间值 —— 这就是「联动缩放」，
  // 不需要单独处理邻居。
  //
  // 两个数都是量出来的，不能随手改：
  // · radius 必须远大于按钮中心间距（实测约 96px），否则邻居全落在作用域外，
  //   只有光标正下方那个会动，联动就没了。230 覆盖左右各两个邻居。
  // · maxScale 受限于 chip 之间的缝隙。这些按钮有自己的边框和背景，1.15 时
  //   一个 86px 的 chip 向两侧各外扩约 6.5px，邻居自己再涨约 3.9px，加起来正好
  //   等于 gap 10px —— 再大就会撞在一起。改文案或改 gap 都要重新算。
  const DOCK = { maxScale: 1.15, radius: 230 };
  // 触屏没有光标，别装；减少动效时也完全不放大 —— CSS 那个 prefers-reduced-motion
  // 块管不到这里，因为 transform 是 JS 逐帧直接写上去的。
  const canMagnify = () =>
    matchMedia("(hover: hover) and (pointer: fine)").matches &&
    !matchMedia("(prefers-reduced-motion: reduce)").matches;

  const dock = { host: null, buttons: [], boxes: [], frame: 0, x: null, y: null };

  // 量一次存起来。绝不能在 pointermove 里读 getBoundingClientRect：
  // 十来个按钮 × 每帧 = 强制重排，必卡。
  function measureDock() {
    if (!dock.host) return;
    const base = dock.host.getBoundingClientRect();
    dock.boxes = dock.buttons.map((button) => {
      const rect = button.getBoundingClientRect();
      return {
        center: rect.left - base.left + rect.width / 2,
        top: rect.top - base.top,
        bottom: rect.bottom - base.top,
      };
    });
  }

  function paintDock() {
    dock.frame = 0;
    const { x, y } = dock;
    dock.buttons.forEach((button, index) => {
      const box = dock.boxes[index];
      // 窄屏时 chip 会换行。距离只算 X 的话，悬停第一行会让第二行同一列的按钮
      // 跟着放大 —— 所以先把不在光标那一行的排除掉。
      if (x === null || !box || y < box.top || y > box.bottom) {
        button.style.transform = "";
        return;
      }
      const distance = Math.abs(x - box.center);
      const t = Math.max(0, Math.min(1, 1 - distance / DOCK.radius));
      const ease = t * t * (3 - 2 * t);
      button.style.transform = `scale(${(1 + (DOCK.maxScale - 1) * ease).toFixed(3)})`;
    });
  }

  function scheduleDock() {
    if (dock.frame) return;
    dock.frame = requestAnimationFrame(paintDock);
  }

  function bindDock(host) {
    dock.host = host;
    dock.buttons = [...host.querySelectorAll("button[data-manual-state]")];

    host.addEventListener("pointerenter", () => {
      if (!canMagnify()) return;
      measureDock();
    });
    host.addEventListener("pointermove", (event) => {
      if (!canMagnify()) return;
      const base = host.getBoundingClientRect();
      dock.x = event.clientX - base.left;
      dock.y = event.clientY - base.top;
      scheduleDock();
    });
    host.addEventListener("pointerleave", () => {
      dock.x = null;
      dock.y = null;
      scheduleDock();
    });
    // chip 会换行，宽度一变中心点全变
    addEventListener("resize", measureDock);
  }

  async function chooseManualState(state) {
    const message = state ? `✓ 已切换为${STATE_LABELS[state]}` : "✓ 已恢复自动控制";
    const result = await run(() => api("POST", "/v1/manual", { state }), message);
    if (!result) return;
    if (store.snapshot && store.snapshot.state) {
      store.snapshot.state.manual = state ? { state, until: null } : null;
      if (result.effective) store.snapshot.state.effective = result.effective;
    }
    paintManualControls(state || "auto");
  }

  async function renderApiPage() {
    const data = await run(() => api("GET", "/v1/api-token"));
    if (!data) return;
    $("api-endpoint").value = data.endpoint;
    $("api-token").value = data.token;
    $("api-examples").textContent = [
      "CLI 示例",
      "agentlightctl emit --source my-agent --session task-123 --state busy --ttl 1800",
      "agentlightctl end --source my-agent --session task-123",
      "",
      "HTTP 示例（PowerShell）",
      "$headers = @{ Authorization = 'Bearer <token>' }",
      `Invoke-RestMethod -Method Post -Uri '${data.endpoint}/v1/events' -Headers $headers \``,
      "  -ContentType 'application/json' \\",
      `  -Body '{"source":"my-agent","sessionId":"task-123","state":"busy"}'`,
      "",
      `WebSocket: ${data.endpoint.replace("http", "ws")}/v1/stream（Authorization: Bearer <token>）`,
    ].join("\n");
  }

  async function refreshLogs() {
    const data = await run(() => api("GET", "/v1/logs?tail=200000"));
    if (!data) return;
    const view = $("log-view");
    const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 40;
    view.textContent = data.text || "（暂无日志）";
    if ($("log-follow").checked || atBottom) view.scrollTop = view.scrollHeight;
  }

  function setLogPolling(active) {
    clearInterval(store.logTimer);
    store.logTimer = 0;
    if (active) store.logTimer = setInterval(() => { if (!document.hidden) refreshLogs(); }, 2000);
  }

  // ------------------------------------------------------------------ 渲染入口

  const lastTopbar = { muted: null, paused: null, night: null };

  function renderTopbar() {
    const snap = store.snapshot;
    const muted = Boolean(snap && snap.muted);
    const paused = Boolean(snap && snap.paused);
    const night = Boolean(store.config && store.config.night_mode && store.config.night_mode.enabled);
    if (muted === lastTopbar.muted && paused === lastTopbar.paused && night === lastTopbar.night) return;
    lastTopbar.muted = muted;
    lastTopbar.paused = paused;
    lastTopbar.night = night;

    const scale = (store.config && store.config.night_mode && store.config.night_mode.scale) || 35;
    const nightButton = $("toggle-night");
    nightButton.setAttribute("aria-pressed", String(night));
    nightButton.textContent = "";
    const nightDot = document.createElement("span");
    nightDot.className = "pulse-dot";
    nightDot.dataset.tone = night ? "warn" : "";
    nightButton.append(nightDot, document.createTextNode(night ? `夜间模式 ${scale}%` : "夜间模式"));
    nightButton.title = night
      ? `所有灯光已按 ${scale}% 压低，点击关闭`
      : "点击按比例压低所有状态的亮度，不改各状态的档案";

    const mute = $("toggle-mute");
    mute.setAttribute("aria-pressed", String(muted));
    mute.textContent = muted ? "✓ 设备蜂鸣已静音" : "设备蜂鸣静音";
    mute.title = muted ? "Orvyn 蜂鸣器已静音，点击恢复" : "点击静音 Orvyn 蜂鸣器（不影响灯光）";

    // 用文字加色点直接说明当前处于哪种状态，而不是只给一个动作名
    const pause = $("toggle-pause");
    pause.setAttribute("aria-pressed", String(paused));
    pause.textContent = "";
    const dot = document.createElement("span");
    dot.className = "pulse-dot";
    dot.dataset.tone = paused ? "bad" : "ok";
    pause.append(dot, document.createTextNode(paused ? "已暂停联动" : "联动中"));
    pause.title = paused ? "点击恢复自动联动" : "点击暂停自动联动";
  }

  // 侧栏那颗设备指示灯常驻，「关闭连接」按钮在设备页 —— 两个都不属于总览。
  // 之前塞在 renderDashboard() 里，导致离开总览页后按钮状态再也不刷新，
  // 点了「关闭连接」文字不变、也变不回去，看起来就像功能是单向的。
  // 状态灯关闭和蜂鸣器静音是同一种东西：一个时段驱动一个开关。共用渲染和提交。
  const SCHEDULES = [
    { prefix: "rest-schedule", key: "device_rest_schedule", start: "23:00", end: "07:00", label: "状态灯关闭" },
    { prefix: "mute-schedule", key: "mute_schedule", start: "22:00", end: "08:00", label: "蜂鸣器静音" },
  ];

  function renderRestSchedule() {
    const config = store.config;
    if (!config) return;
    for (const spec of SCHEDULES) {
      const schedule = config[spec.key] || {};
      const enabled = Boolean(schedule.enabled);
      const toggle = $(`${spec.prefix}-toggle`);
      const row = toggle.closest(".rest-schedule");
      // 正在改的时候别回填，否则输入到一半会被服务端推来的旧值顶掉
      const editing = document.activeElement && document.activeElement.closest
        && document.activeElement.closest(".rest-schedule") === row;
      if (!editing) {
        $(`${spec.prefix}-start`).value = schedule.start || spec.start;
        $(`${spec.prefix}-end`).value = schedule.end || spec.end;
      }
      toggle.setAttribute("aria-pressed", String(enabled));
      toggle.textContent = enabled ? "✓ 已开启" : "开启";
      row.classList.toggle("is-off", !enabled);
    }
  }

  // 5–30 分钟，5 分钟一档。设备固件收的是秒。
  const IDLE_SLEEP_MINUTES = [5, 10, 15, 20, 25, 30];
  // 下拉里的「不启用」。用空串而不是 0，免得跟一个真的时长混淆。
  const IDLE_SLEEP_OFF = "";

  function renderIdleSleep() {
    const device = store.config && store.config.device;
    if (!device) return;
    const select = $("idle-sleep-minutes");
    if (!select.options.length) {
      // 「--」本身就表达不启用，不用再配一个开关按钮
      for (const [value, text] of [[IDLE_SLEEP_OFF, "--"], ...IDLE_SLEEP_MINUTES.map((m) => [String(m), `${m} 分钟`])]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        select.append(option);
      }
    }
    if (document.activeElement === select) return;
    if (!device.auto_sleep) {
      select.value = IDLE_SLEEP_OFF;
      return;
    }
    // 落到最近的一档：配置里存的是秒，可能是旧版本写进去的任意值
    const minutes = Math.round(Number(device.sleep_timeout || 300) / 60);
    select.value = String(IDLE_SLEEP_MINUTES.reduce(
      (best, item) => (Math.abs(item - minutes) < Math.abs(best - minutes) ? item : best)));
  }

  function renderDeviceStatus() {
    const snap = store.snapshot;
    if (!snap) return;
    const connected = Boolean(snap.device && snap.device.connected);
    // 主动关掉的连接不是故障，别用同一句话吓人
    const resting = Boolean(snap.device_resting || (snap.device || {}).detached);
    $("device-pill-text").textContent = resting
      ? "设备连接已关闭"
      : connected ? "Orvyn 已连接" : "设备未连接（预览仍可用）";
    $("device-pill").querySelector(".pulse-dot").dataset.tone = resting ? "idle" : connected ? "ok" : "bad";

    const rest = $("device-rest");
    rest.setAttribute("aria-pressed", String(resting));
    rest.textContent = resting ? "✓ 已关闭连接" : "关闭连接";
  }

  function renderAll() {
    renderTopbar();
    paintManualControls();
    renderDeviceStatus();
    renderRestSchedule();
    renderIdleSleep();
    // 在页面分支之前记录，确保切换页面不会中断时间线。
    if (store.snapshot) recordTimeline(store.snapshot.state.effective.state);
    if (store.page === "dashboard") renderDashboard();
    if (store.page === "device") renderDevice();
    if (store.page === "settings") renderSettings();

    const connected = store.snapshot && store.snapshot.device && store.snapshot.device.connected;
    $("conn-text").textContent = store.snapshot ? (connected ? "已连接设备" : "后台运行中 · 设备未连接") : "连接中…";
    $("conn-state").querySelector(".pulse-dot").dataset.tone = store.snapshot ? (connected ? "ok" : "warn") : "idle";
  }

  function showPage(name) {
    store.page = name;
    for (const button of document.querySelectorAll(".nav-item")) {
      const active = button.dataset.page === name;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
    }
    for (const page of document.querySelectorAll(".page")) {
      page.classList.toggle("is-active", page.dataset.page === name);
    }
    setLogPolling(name === "logs");
    if (name === "logs") refreshLogs();
    if (name === "api") renderApiPage();
    if (name === "integrations") renderIntegrations();
    if (name === "dashboard" || name === "device") renderQuota(false);
    // 睡眠/断网期间本地不会有快照，补拉一次让后端把空洞填上
    if (name === "dashboard") fetchTimeline();
    renderAll();
  }

  // ------------------------------------------------------------------ WebSocket

  let socket = null;
  let retryDelay = 800;

  function connect() {
    const url = `${location.origin.replace("http", "ws")}/v1/stream`;
    // 浏览器的 WebSocket API 不能自定义请求头，token 只能借道子协议
    socket = new WebSocket(url, [`agentlight.bearer.${TOKEN}`]);
    socket.addEventListener("open", () => { retryDelay = 800; });
    socket.addEventListener("message", (event) => {
      try {
        store.snapshot = JSON.parse(event.data);
      } catch { return; }
      renderAll();
    });
    socket.addEventListener("close", () => {
      socket = null;
      setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 1.8, 15000);
    });
    socket.addEventListener("error", () => socket && socket.close());
  }

  async function pullConfig() {
    const data = await run(() => api("GET", "/v1/config"));
    if (!data) return;
    store.config = data.config;
    store.autostart = data.autostart;
    store.apiPort = Number(data.config.api.port);
    scheduleQuotaRefresh();
    $("brand-version").textContent = `v${data.version}`;
    $("about-text").textContent = `Agent 状态灯 v${data.version} · 安装于 ${data.installDir} · 数据目录 ${data.dataDir}`;
    $("uninstall-app").disabled = !data.uninstallAvailable;
    $("uninstall-app").title = data.uninstallAvailable ? "" : "当前运行方式没有可用的卸载程序";
    if (!store.draft) loadDraft("ready");
    renderAll();
  }

  async function pullStatus() {
    const data = await run(() => api("GET", "/v1/status"));
    if (!data) return;
    store.snapshot = data;
    renderAll();
  }

  async function refreshPageData() {
    if (document.hidden || store.pageRefreshing) return;
    store.pageRefreshing = true;
    try {
      await Promise.all([pullStatus(), pullConfig(), fetchTimeline()]);
      const quotaEnabled = Number(store.config && store.config.quota_refresh_interval_seconds) > 0;
      if (quotaEnabled) await renderQuota(false, true);
      if (store.page === "logs") await refreshLogs();
      if (store.page === "api") await renderApiPage();
      if (store.page === "integrations") await renderIntegrations();
      renderAll();
    } finally {
      store.pageRefreshing = false;
    }
  }

  function schedulePageRefresh() {
    clearInterval(store.pageRefreshTimer);
    store.pageRefreshTimer = setInterval(refreshPageData, PAGE_REFRESH_MS);
  }

  // ------------------------------------------------------------------ 绑定

  function bind() {
    for (const button of document.querySelectorAll(".nav-item")) {
      button.addEventListener("click", () => showPage(button.dataset.page));
    }

    $("theme-toggle").addEventListener("click", () => {
      const dark = document.documentElement.dataset.theme === "dark";
      document.documentElement.dataset.theme = dark ? "light" : "dark";
      $("theme-toggle").textContent = dark ? "切换深色" : "切换浅色";
      try { localStorage.setItem("agentlight-theme", dark ? "light" : "dark"); } catch { /* 无痕模式 */ }
    });

    // 这三个开关的状态直接写在按钮上，不再额外弹提示；出错时 run() 仍会报错
    $("toggle-mute").addEventListener("click", () => {
      const next = $("toggle-mute").getAttribute("aria-pressed") !== "true";
      run(() => api("POST", "/v1/mute", { muted: next }));
    });
    $("toggle-pause").addEventListener("click", () => {
      const next = $("toggle-pause").getAttribute("aria-pressed") !== "true";
      run(() => api("POST", "/v1/pause", { paused: next }));
    });
    $("toggle-night").addEventListener("click", async () => {
      const next = $("toggle-night").getAttribute("aria-pressed") !== "true";
      await run(() => api("POST", "/v1/night-mode", { enabled: next }));
      await pullConfig();
    });

    // 手动状态
    const manual = $("manual-buttons");
    for (const state of MANUAL_BUTTON_STATES) {
      const button = document.createElement("button");
      button.className = "ghost";
      button.dataset.manualState = state;
      button.setAttribute("aria-pressed", "false");
      button.append(stateDot(state), document.createTextNode(STATE_LABELS[state]));
      button.addEventListener("click", () => chooseManualState(state));
      manual.append(button);
    }
    // 「恢复自动」在标题那一行，不在这排里 —— 它不是状态，而且不参与邻近放大
    $("manual-auto").addEventListener("click", () => chooseManualState(null));
    bindDock(manual);

    // 灯效编辑器
    $("field-effect").addEventListener("change", (event) => {
      store.draft.effect = event.target.value;
      store.draft.effect_params = defaultParams(event.target.value);
      syncEditorInputs();
    });
    $("field-sound").addEventListener("change", (event) => { store.draft.sound = event.target.value; });
    $("field-brightness").addEventListener("input", (event) => {
      store.draft.brightness = Number(event.target.value);
      $("out-brightness").textContent = `${store.draft.brightness}%`;
    });
    $("field-period").addEventListener("input", (event) => {
      store.draft.period_ms = Number(event.target.value);
      $("out-period").textContent = `${(store.draft.period_ms / 1000).toFixed(2)} 秒`;
    });

    $("save-profile").addEventListener("click", async () => {
      const payload = { profiles: { [store.editing]: { ...store.draft } } };
      await run(() => api("PUT", "/v1/profiles", payload), `✓ 已保存「${STATE_LABELS[store.editing]}」档案`);
      await pullConfig();
    });
    // 带上当前草稿，这样不保存也能直接试；灯光和提示音一起测
    $("preview-device").addEventListener("click", () => run(
      () => api("POST", "/v1/preview", { state: store.editing, profile: { ...store.draft } }),
      "✓ 正在设备上测试，5 秒后自动恢复",
    ));

    // 集成
    $("install-hooks").addEventListener("click", () => run(
      () => api("POST", "/v1/integrations", { action: "install" }),
      "✓ 已安装 / 修复，请重启 Codex 与 Claude Code，然后按下方指引完成信任",
    ));
    $("remove-hooks").addEventListener("click", () => {
      if (!confirm("移除 Agent Light 写入的 Hooks？其他工具的 Hook 不受影响。")) return;
      run(() => api("POST", "/v1/integrations", { action: "remove" }), "✓ 已移除本应用 Hooks");
    });
    $("open-backups").addEventListener("click", () => run(() => api("POST", "/v1/reveal", { target: "backups" })));
    $("open-logs").addEventListener("click", () => run(() => api("POST", "/v1/reveal", { target: "logs" })));
    $("open-install").addEventListener("click", () => run(() => api("POST", "/v1/reveal", { target: "install" })));

    const uninstallDialog = $("uninstall-dialog");
    $("uninstall-app").addEventListener("click", () => uninstallDialog.showModal());
    $("confirm-uninstall").addEventListener("click", async () => {
      const button = $("confirm-uninstall");
      button.disabled = true;
      button.textContent = "正在打开...";
      const result = await run(() => api("POST", "/v1/uninstall"), "Windows 卸载程序已打开");
      button.disabled = false;
      button.textContent = "打开卸载程序";
      if (result) uninstallDialog.close();
    });

    // 设备
    const showDeviceRange = (id, out, suffix) => {
      $(id).addEventListener("input", () => { $(out).textContent = `${$(id).value}${suffix}`; });
    };
    showDeviceRange("dev-base", "out-base", "%");
    showDeviceRange("dev-max", "out-max", "%");
    showDeviceRange("dev-frame", "out-frame", " fps");

    $("save-device").addEventListener("click", async () => {
      const base = Number($("dev-base").value);
      const max = Number($("dev-max").value);
      $("dev-brightness-warn").hidden = base <= max;
      if (base > max) return;
      await run(() => api("PUT", "/v1/device", {
        base_brightness: base,
        max_brightness: max,
        frame_rate: Number($("dev-frame").value),
        screen_rotation: Number($("dev-rotation").value),
        sound_style: $("dev-sound-style").value,
        quota_sync_interval: Number($("dev-quota-interval").value),
        keep_5v: $("dev-keep-5v").checked,
        codex_quota_sync: $("dev-quota-sync").checked,
      }), "✓ 设备设置已生效");
      await pullConfig();
    });
    $("device-sleep").addEventListener("click", async () => {
      const result = await run(() => api("POST", "/v1/device/sleep"));
      if (result) say(result.ok ? "✓ 设备已进入休眠" : "活动状态下保持唤醒，请先熄灭或暂停联动", !result.ok);
    });
    for (const spec of SCHEDULES) {
      const toggle = $(`${spec.prefix}-toggle`);
      const save = async (enabled) => {
        const start = $(`${spec.prefix}-start`).value;
        const end = $(`${spec.prefix}-end`).value;
        if (enabled && (!start || !end)) {
          say("请先填好开始和结束时间", true);
          return;
        }
        await run(() => api("PUT", "/v1/settings", {
          [spec.key]: { enabled, start: start || spec.start, end: end || spec.end },
        }), enabled ? `✓ ${spec.label} ${start} 至 ${end}` : `✓ 已关闭${spec.label}`);
        await pullConfig();
      };
      toggle.addEventListener("click", () => save(toggle.getAttribute("aria-pressed") !== "true"));
      for (const suffix of ["start", "end"]) {
        // 改时间时保持当前开关状态，不要顺手把它打开
        $(`${spec.prefix}-${suffix}`).addEventListener("change",
          () => save(toggle.getAttribute("aria-pressed") === "true"));
      }
    }

    $("idle-sleep-minutes").addEventListener("change", async () => {
      const raw = $("idle-sleep-minutes").value;
      const enabled = raw !== IDLE_SLEEP_OFF;
      const minutes = enabled ? Number(raw) : IDLE_SLEEP_MINUTES[0];
      await run(() => api("PUT", "/v1/device", {
        auto_sleep: enabled,
        // 关闭时不动时长，下次选回来还是原来那档
        sleep_timeout: minutes * 60,
      }), enabled ? `✓ 无任务 ${minutes} 分钟后自动休眠` : "✓ 已关闭自动休眠");
      await pullConfig();
    });

    $("device-rest").addEventListener("click", async () => {
      const next = $("device-rest").getAttribute("aria-pressed") !== "true";
      await run(() => api("POST", "/v1/device/rest", { resting: next }),
        next ? "✓ 已关闭设备连接" : "✓ 已恢复设备连接");
    });
    $("sync-quota").addEventListener("click", async () => {
      const result = await run(() => api("POST", "/v1/quota/sync"));
      await renderQuota(true);
      if (!result) return;
      const synced = result.quota.pushed_to_screen
        ? `✓ 配额已同步到设备屏幕：5 小时 ${result.quota.five_hour}%，本周 ${result.quota.week}%`
        : `✓ 配额已刷新：5 小时 ${result.quota.five_hour}%，本周 ${result.quota.week}%（设备无屏幕，未推送）`;
      // 同步的是 Codex 两格，成功了就照说；但 Claude 那边挂了不能跟着一起报喜
      const outcome = quotaOutcome(store.quotaSource, store.quota);
      say(outcome.ok ? synced : `${synced}；但 ${outcome.text}`, !outcome.ok);
    });
    $("dash-refresh-quota").addEventListener("click", async () => {
      const refreshed = await renderQuota(true);
      if (!refreshed) return;
      const outcome = quotaOutcome(store.quotaSource, store.quota);
      say(outcome.text, !outcome.ok);
    });

    $("set-night-scale").addEventListener("input", () => {
      $("out-night").textContent = `${$("set-night-scale").value}%`;
    });

    $("reset-profiles").addEventListener("click", async () => {
      if (!confirm("把九个状态的灯效档案全部恢复成默认？设备参数、优先级和 Agent 集成不受影响。")) return;
      await run(() => api("POST", "/v1/profiles/reset"), "✓ 灯效档案已恢复默认");
      await pullConfig();
      loadDraft(store.editing);
    });


    // 接入
    $("reveal-token").addEventListener("click", () => {
      const input = $("api-token");
      const hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      $("reveal-token").textContent = hidden ? "隐藏" : "显示";
    });
    $("copy-token").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("api-token").value);
        say("✓ 令牌已复制");
      } catch {
        say("浏览器拒绝了剪贴板访问，请手动复制", true);
      }
    });

    // 日志
    $("log-refresh").addEventListener("click", refreshLogs);
    $("log-follow").addEventListener("change", () => setLogPolling($("log-follow").checked && store.page === "logs"));
    $("log-export").addEventListener("click", () => {
      const blob = new Blob([$("log-view").textContent], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `agentlight-${new Date().toISOString().slice(0, 10)}.log`;
      link.click();
      URL.revokeObjectURL(url);
    });
    $("log-clear").addEventListener("click", () => {
      if (!confirm("清空当前诊断日志？此操作不影响设置或备份。")) return;
      run(async () => { await api("DELETE", "/v1/logs"); await refreshLogs(); }, "✓ 日志已清空");
    });

    // 通用设置
    $("save-settings").addEventListener("click", async () => {
      const quotaIntervalInput = $("set-quota-refresh-interval");
      const quotaInterval = Number(quotaIntervalInput.value);
      quotaIntervalInput.setCustomValidity(
        Number.isInteger(quotaInterval) && quotaInterval >= 0 && quotaInterval <= 86400
          ? ""
          : "请输入 0 到 86400 之间的整数秒数",
      );
      if (!quotaIntervalInput.reportValidity()) return;
      const portInput = $("set-api-port");
      const apiPort = Number(portInput.value);
      portInput.setCustomValidity(
        Number.isInteger(apiPort) && apiPort >= 1024 && apiPort <= 65535
          ? ""
          : "请输入 1024 到 65535 之间的整数端口",
      );
      if (!portInput.reportValidity()) return;
      const previousPort = store.apiPort;
      const priorities = {};
      for (const input of $("priority-grid").querySelectorAll("input")) {
        priorities[input.dataset.state] = Number(input.value);
      }
      const hotkey = { enabled: $("set-hotkey-enabled").checked, combo: $("set-hotkey").dataset.combo };
      const savedHotkey = store.config.hotkey || {};
      // 只在改过时才发：启动时就被占用的快捷键不该连累其他设置保存不了
      const hotkeyChanged = hotkey.enabled !== Boolean(savedHotkey.enabled) || hotkey.combo !== savedHotkey.combo;
      const result = await run(() => api("PUT", "/v1/settings", {
        ...(hotkeyChanged ? { hotkey } : {}),
        start_with_windows: $("set-autostart").checked,
        default_ttl_seconds: Number($("set-ttl").value),
        done_hold_seconds: Number($("set-hold").value),
        force_reapply_seconds: Number($("set-reapply").value),
        manual_timeout_seconds: Number($("set-manual-timeout").value),
        stale_after_seconds: Number($("set-stale-after").value),
        quota_refresh_interval_seconds: quotaInterval,
        api_port: apiPort,
        priorities,
      }), "✓ 通用设置已生效");
      if (!result) return;
      store.config = {
        ...store.config,
        quota_refresh_interval_seconds: result.quotaRefreshIntervalSeconds,
      };
      quotaIntervalInput.value = String(result.quotaRefreshIntervalSeconds);
      portInput.value = String(result.apiPort);
      store.apiPort = Number(result.apiPort);
      scheduleQuotaRefresh();
      await run(() => api("POST", "/v1/night-mode", { scale: Number($("set-night-scale").value) }));
      await pullConfig();
      if (Number(result.apiPort) !== previousPort) {
        const nextUrl = `${location.protocol}//${location.hostname}:${result.apiPort}/`;
        const restarted = await run(() => api("POST", "/v1/restart"), `端口已改为 ${result.apiPort}，正在重启...`);
        if (restarted) setTimeout(() => location.replace(nextUrl), 1800);
      }
    });

    // 快捷键录制：聚焦后按下组合键即记录，Esc 放弃
    const hotkeyInput = $("set-hotkey");
    hotkeyInput.addEventListener("focus", () => {
      hotkeyInput.value = "";
      hotkeyInput.placeholder = "按下组合键，Esc 取消";
    });
    hotkeyInput.addEventListener("blur", () => {
      hotkeyInput.value = hotkeyInput.dataset.combo || "";
      hotkeyInput.placeholder = "点击后按下组合键";
    });
    hotkeyInput.addEventListener("keydown", (event) => {
      if (event.key === "Tab") return;
      event.preventDefault();
      if (event.key === "Escape") {
        hotkeyInput.blur();
        return;
      }
      const { preview, combo } = hotkeyFromEvent(event);
      hotkeyInput.value = preview;
      if (combo) {
        hotkeyInput.dataset.combo = combo;
        hotkeyInput.blur();
      }
    });
    hotkeyInput.addEventListener("keyup", () => {
      // 只按了修饰键就松开：清掉「Ctrl+…」这类半截预览
      if (document.activeElement === hotkeyInput) hotkeyInput.value = "";
    });

    $("quit-app").addEventListener("click", () => {
      if (!confirm("退出 Agent Light 后台？灯光联动会停止，托盘图标也会消失。")) return;
      run(() => api("POST", "/v1/quit"), "正在退出…");
    });

    // 快捷键
    document.addEventListener("keydown", (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const tag = (event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "select" || tag === "textarea") return;
      if (event.key === "0") {
        chooseManualState(null);
        return;
      }
      const index = Number(event.key) - 1;
      if (index >= 0 && index < MANUAL_STATES.length) {
        const state = MANUAL_STATES[index];
        chooseManualState(state);
      }
    });
  }

  // ------------------------------------------------------------------ 启动

  async function boot() {
    try {
      const stored = localStorage.getItem("agentlight-theme");
      if (stored) {
        document.documentElement.dataset.theme = stored;
        $("theme-toggle").textContent = stored === "dark" ? "切换浅色" : "切换深色";
      }
    } catch { /* 无痕模式下忽略 */ }

    buildTimeline();
    buildStatePicker();

    const registry = await run(() => api("GET", "/v1/effects"));
    if (registry) {
      store.effects = registry.effects;
      store.effectMap = Object.fromEntries(registry.effects.map((item) => [item.id, item]));
      $("dev-frame").min = String(registry.frameRate.minimum);
      $("dev-frame").max = String(registry.frameRate.maximum);
    }

    bind();
    buildEffectControls();

    heroStrip = new LampStrip($("hero-lamps"));
    heroStrip.source = () => {
      const snap = store.snapshot;
      if (!snap || !store.config) return null;
      const state = snap.state.effective.state;
      const profile = profileOf(state);
      if (!profile) return null;
      return {
        effect: profile.effect || profile.mode || "static",
        colors: profile.colors,
        params: profile.effect_params || {},
        periodMs: profile.period_ms,
        brightness: profile.brightness,
      };
    };

    editorStrip = new LampStrip($("editor-lamps"));
    editorStrip.source = draftSpec;

    await pullConfig();
    await renderQuota(false);
    await fetchTimeline();
    connect();
    startLoop();
    schedulePageRefresh();
    setInterval(renderAll, 1000);
  }

  boot();
})();
