import { useState } from "react";
import type {
  ChatEntryConfig,
  ObsLayoutId,
  RundownGameCatalogItem,
  RundownPlan,
  RundownPresetItem,
  RundownSegment,
  RundownSegmentKind,
} from "./consoleTypes";
import styles from "./LiveRulesScene.module.css";

const FALLBACK_GAME_CATALOG: RundownGameCatalogItem[] = [
  {
    kind: "draw_and_guess",
    label: "你画我猜 2.0",
    default_layout: "game",
    description: "Aqua 作画并渐进提示，弹幕抢答积分。",
  },
  {
    kind: "turtle_soup",
    label: "灵魂海龟汤",
    default_layout: "game",
    description: "悬疑/日常汤面推理，弹幕提问解锁线索。",
  },
  {
    kind: "roast_radio",
    label: "锐评辩论",
    default_layout: "chat",
    description: "正反方站队辩论或犀利电台吐槽。",
  },
  {
    kind: "song_party",
    label: "点歌听歌会",
    default_layout: "chat",
    description: "弹幕点歌合唱与轻氛围伴播。",
  },
  {
    kind: "stardew_valley",
    label: "星露谷物语",
    default_layout: "game",
    description: "牧场实机游戏联动与种田互动。",
  },
  {
    kind: "pvz",
    label: "植物大战僵尸",
    default_layout: "game",
    description: "草坪防线策略实机互动。",
  },
];

const SEGMENT_KIND_OPTIONS: Array<{
  kind: RundownSegmentKind;
  label: string;
  defaultLayout: ObsLayoutId;
}> = [
  { kind: "chat", label: "💬 自由闲聊 (chat)", defaultLayout: "chat" },
  { kind: "draw_and_guess", label: "🎨 你画我猜 2.0", defaultLayout: "game" },
  { kind: "turtle_soup", label: "🐢 灵魂海龟汤", defaultLayout: "game" },
  { kind: "roast_radio", label: "🎙️ 锐评辩论", defaultLayout: "chat" },
  { kind: "song_party", label: "🎵 点歌听歌会", defaultLayout: "chat" },
  { kind: "stardew_valley", label: "🌾 星露谷物语", defaultLayout: "game" },
  { kind: "pvz", label: "🌻 植物大战僵尸", defaultLayout: "game" },
];

export const DEFAULT_CHAT_ENTRY: ChatEntryConfig = {
  opening_mode: "chat_opening",
  opening_chat_minutes: 15,
  poll_duration_seconds: 90,
  min_votes: 1,
  include_continue_chat: true,
  avoid_repeat: true,
  allow_chat_vote_trigger: true,
  vote_trigger_threshold: 2,
  allow_chat_extend: true,
  extend_chat_minutes: 15,
  default_game_duration_minutes: 20,
  chat_adaptive_enabled: true,
  chat_hot_threshold_per_2m: 12,
};

export function createDefaultRundownPlan(): RundownPlan {
  return {
    name: "多游戏轮播→闲聊",
    enabled_games: ["draw_and_guess", "turtle_soup", "roast_radio", "song_party"],
    hold_on_last: true,
    auto_switch_layout: true,
    chat_entry: { ...DEFAULT_CHAT_ENTRY },
    segments: [
      {
        id: "seg-opening-chat",
        title: "开场闲聊 & 选游戏",
        kind: "chat",
        duration_minutes: 15,
        obs_layout: "chat",
        auto_start_show: false,
        graceful_finish: true,
        settings: {},
      },
      {
        id: "seg-draw-guess",
        title: "你画我猜 2.0",
        kind: "draw_and_guess",
        duration_minutes: 25,
        obs_layout: "game",
        auto_start_show: true,
        graceful_finish: true,
        settings: { round_duration_sec: 60, use_llm: true, themes: [] },
      },
      {
        id: "seg-closing-chat",
        title: "收尾闲聊",
        kind: "chat",
        duration_minutes: 20,
        obs_layout: "chat",
        auto_start_show: false,
        graceful_finish: true,
        settings: {},
      },
    ],
  };
}

const FALLBACK_PRESETS: RundownPresetItem[] = [
  {
    id: "multi_game_chat",
    name: "多游戏轮播→闲聊",
    description: "15分钟闲聊开局后弹幕投票选游戏，轮播后回闲聊。",
    plan: createDefaultRundownPlan(),
  },
  {
    id: "device_and_shows",
    name: "游戏实机+互动→闲聊",
    description: "实机游戏与小剧场穿插，适合长时段带画面直播。",
    plan: {
      name: "游戏实机+互动→闲聊",
      enabled_games: ["stardew_valley", "pvz", "draw_and_guess", "turtle_soup"],
      hold_on_last: true,
      auto_switch_layout: true,
      chat_entry: { ...DEFAULT_CHAT_ENTRY },
      segments: [
        {
          id: "seg-opening",
          title: "开场唠嗑",
          kind: "chat",
          duration_minutes: 15,
          obs_layout: "chat",
          auto_start_show: false,
          graceful_finish: true,
          settings: {},
        },
        {
          id: "seg-stardew",
          title: "星露谷实机",
          kind: "stardew_valley",
          duration_minutes: 40,
          obs_layout: "game",
          auto_start_show: true,
          graceful_finish: true,
          settings: { owner: "studio" },
        },
        {
          id: "seg-end",
          title: "下播前闲聊",
          kind: "chat",
          duration_minutes: 15,
          obs_layout: "chat",
          auto_start_show: false,
          graceful_finish: true,
          settings: {},
        },
      ],
    },
  },
  {
    id: "late_night_llm",
    name: "纯LLM轻综艺夜场",
    description: "聚焦海龟汤、锐评辩论与点歌陪伴，轻量低打扰。",
    plan: {
      name: "纯LLM轻综艺夜场",
      enabled_games: ["turtle_soup", "roast_radio", "song_party", "draw_and_guess"],
      hold_on_last: true,
      auto_switch_layout: true,
      chat_entry: { ...DEFAULT_CHAT_ENTRY, opening_chat_minutes: 15 },
      segments: [
        {
          id: "seg-night-chat",
          title: "深夜暖场闲聊",
          kind: "chat",
          duration_minutes: 15,
          obs_layout: "chat",
          auto_start_show: false,
          graceful_finish: true,
          settings: {},
        },
        {
          id: "seg-night-soup",
          title: "深夜海龟汤",
          kind: "turtle_soup",
          duration_minutes: 25,
          obs_layout: "game",
          auto_start_show: true,
          graceful_finish: true,
          settings: { genre: "mystery", max_questions: 20, use_llm: true },
        },
        {
          id: "seg-night-roast",
          title: "锐评夜话电台",
          kind: "roast_radio",
          duration_minutes: 20,
          obs_layout: "chat",
          auto_start_show: true,
          graceful_finish: true,
          settings: { mode: "debate", topic: "熬夜写代码配白饭还是热茶" },
        },
      ],
    },
  },
];

function parseIntOrFallback(raw: string, fallback: number, min: number, max: number): number {
  const n = Number.parseInt(raw, 10);
  if (Number.isNaN(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

function segmentThemesText(settings: Record<string, unknown>): string {
  if (Array.isArray(settings.themes)) {
    return settings.themes.map((t) => String(t)).join(", ");
  }
  if (typeof settings.theme === "string") {
    return settings.theme;
  }
  return "";
}

export interface RundownBuilderPanelProps {
  plan: RundownPlan;
  presets?: RundownPresetItem[];
  gameCatalog?: RundownGameCatalogItem[];
  dirty: boolean;
  saving: boolean;
  statusMessage?: string;
  statusError?: boolean;
  onChangePlan: (nextPlan: RundownPlan) => void;
  onSave: () => void;
  onResetDraft?: () => void;
}

export function RundownBuilderPanel({
  plan,
  presets,
  gameCatalog,
  dirty,
  saving,
  statusMessage,
  statusError,
  onChangePlan,
  onSave,
  onResetDraft,
}: RundownBuilderPanelProps) {
  const effectivePresets = presets && presets.length > 0 ? presets : FALLBACK_PRESETS;
  const effectiveCatalog = gameCatalog && gameCatalog.length > 0 ? gameCatalog : FALLBACK_GAME_CATALOG;
  const [selectedPresetId, setSelectedPresetId] = useState<string>(effectivePresets[0]?.id ?? "multi_game_chat");

  const applyPreset = (presetId: string) => {
    const target = effectivePresets.find((p) => p.id === presetId) ?? effectivePresets[0];
    if (!target) return;
    setSelectedPresetId(target.id);
    onChangePlan(structuredClone(target.plan));
  };

  const toggleGameInPool = (kind: string) => {
    const exists = plan.enabled_games.includes(kind);
    const nextGames = exists
      ? plan.enabled_games.filter((item) => item !== kind)
      : [...plan.enabled_games, kind];
    onChangePlan({
      ...plan,
      enabled_games: nextGames,
    });
  };

  const updateChatEntry = (patch: Partial<ChatEntryConfig>) => {
    onChangePlan({
      ...plan,
      chat_entry: {
        ...plan.chat_entry,
        ...patch,
      },
    });
  };

  const updateSegment = (index: number, patch: Partial<RundownSegment>) => {
    const nextSegments = plan.segments.map((seg, idx) => (idx === index ? { ...seg, ...patch } : seg));
    onChangePlan({ ...plan, segments: nextSegments });
  };

  const updateSegmentSettings = (index: number, patch: Record<string, unknown>) => {
    const seg = plan.segments[index];
    if (!seg) return;
    updateSegment(index, {
      settings: {
        ...(seg.settings || {}),
        ...patch,
      },
    });
  };

  const moveSegment = (index: number, delta: -1 | 1) => {
    const targetIndex = index + delta;
    if (targetIndex < 0 || targetIndex >= plan.segments.length) return;
    const nextSegments = [...plan.segments];
    const [moved] = nextSegments.splice(index, 1);
    nextSegments.splice(targetIndex, 0, moved);
    onChangePlan({ ...plan, segments: nextSegments });
  };

  const removeSegment = (index: number) => {
    const nextSegments = plan.segments.filter((_, idx) => idx !== index);
    onChangePlan({ ...plan, segments: nextSegments });
  };

  const addSegment = () => {
    const idSuffix =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID().slice(0, 6)
        : String(Date.now()).slice(-6);
    const nextSeg: RundownSegment = {
      id: `seg-${idSuffix}`,
      title: "新环节 · 闲聊",
      kind: "chat",
      duration_minutes: 15,
      obs_layout: "chat",
      auto_start_show: false,
      graceful_finish: true,
      settings: {},
    };
    onChangePlan({
      ...plan,
      segments: [...plan.segments, nextSeg],
    });
  };

  return (
    <div id="rundown-builder-panel" className={styles.tabPanel}>
      <article className="win-group-box">
        <div className="panel-heading">
          <div className="win-group-title" style={{ margin: 0 }}>
            🎬 节目单预设模板
          </div>
          <span className="hint">一键套用后可继续微调环节、时长与投票池</span>
        </div>
        <div className={styles.compactFormGrid}>
          <label>
            选择预设方案
            <select
              id="rundown-preset-select"
              value={selectedPresetId}
              onChange={(e) => setSelectedPresetId(e.target.value)}
            >
              {effectivePresets.map((preset) => (
                <option key={preset.id} value={preset.id}>
                  {preset.name}
                </option>
              ))}
            </select>
          </label>
          <div className="actions" style={{ alignSelf: "end" }}>
            <button
              type="button"
              id="rundown-apply-preset-btn"
              className="primary win-btn"
              disabled={saving}
              onClick={() => applyPreset(selectedPresetId)}
            >
              一键套用预设
            </button>
          </div>
          <label>
            当前节目单名称
            <input
              id="rundown-plan-name"
              type="text"
              maxLength={80}
              value={plan.name}
              onChange={(e) => onChangePlan({ ...plan, name: e.target.value })}
            />
          </label>
        </div>

        <div className={styles.presetGrid}>
          {effectivePresets.map((preset) => {
            const isCurrent = plan.name === preset.name;
            return (
              <div
                key={preset.id}
                className={`${styles.presetCard} ${isCurrent ? styles.presetCardActive : ""}`}
                data-rundown-preset={preset.id}
              >
                <div className={styles.presetHeader}>
                  <strong>{preset.name}</strong>
                  {isCurrent && <span className="badge">当前模板</span>}
                </div>
                <p className="hint" style={{ margin: 0 }}>
                  {preset.description}
                </p>
                <button
                  type="button"
                  className="win-btn"
                  data-apply-preset={preset.id}
                  disabled={saving}
                  onClick={() => applyPreset(preset.id)}
                >
                  套用此预设
                </button>
              </div>
            );
          })}
        </div>
      </article>

      <article className="win-group-box" id="rundown-game-pool-card">
        <div className="panel-heading">
          <div className="win-group-title" style={{ margin: 0 }}>
            🗳️ 观众投票可选游戏池（已选 {plan.enabled_games.length} / {effectiveCatalog.length}）
          </div>
          <span className="hint">开场闲聊后或弹幕触发选游戏投票时，仅从已勾选项目中生成候选</span>
        </div>
        <div className={styles.gamePoolGrid}>
          {effectiveCatalog.map((item) => {
            const checked = plan.enabled_games.includes(item.kind);
            return (
              <label
                key={item.kind}
                className={`${styles.gamePoolCard} ${checked ? styles.gamePoolCardChecked : ""}`}
              >
                <input
                  type="checkbox"
                  data-game-pool-toggle={item.kind}
                  checked={checked}
                  onChange={() => toggleGameInPool(item.kind)}
                />
                <span className={styles.gamePoolInfo}>
                  <span className={styles.gamePoolTitleRow}>
                    <strong>{item.label}</strong>
                    <span
                      className={`${styles.layoutTag} ${
                        item.default_layout === "game" ? styles.layoutTagGame : styles.layoutTagChat
                      }`}
                    >
                      {item.default_layout === "game" ? "🎮 游戏布局" : "💬 闲聊布局"}
                    </span>
                  </span>
                  <small className="hint" style={{ margin: 0 }}>
                    {item.description}
                  </small>
                </span>
              </label>
            );
          })}
        </div>
      </article>

      <article className="win-group-box" id="rundown-chat-entry-card">
        <div className="panel-heading">
          <div className="win-group-title" style={{ margin: 0 }}>
            💬 Chat 入口与投票触发设置
          </div>
          <span className="hint">控制开播初始阶段、投票阈值、热聊自适应及 OBS 自动切换</span>
        </div>

        <div className={styles.compactFormGrid}>
          <label>
            开播入口模式
            <select
              id="rundown-opening-mode"
              value={plan.chat_entry.opening_mode}
              onChange={(e) =>
                updateChatEntry({
                  opening_mode: e.target.value as ChatEntryConfig["opening_mode"],
                })
              }
            >
              <option value="chat_opening">默认 Chat 开局</option>
              <option value="direct_rundown">按节目单直开</option>
              <option value="manual">手动控制</option>
            </select>
          </label>
          <label>
            开场闲聊时长（分钟）
            <input
              id="rundown-opening-minutes"
              type="number"
              min={1}
              max={360}
              value={plan.chat_entry.opening_chat_minutes}
              onChange={(e) =>
                updateChatEntry({
                  opening_chat_minutes: parseIntOrFallback(e.target.value, 15, 1, 360),
                })
              }
            />
          </label>
          <label>
            投票持续时间（秒）
            <input
              id="rundown-poll-duration"
              type="number"
              min={10}
              max={600}
              value={plan.chat_entry.poll_duration_seconds}
              onChange={(e) =>
                updateChatEntry({
                  poll_duration_seconds: parseIntOrFallback(e.target.value, 90, 10, 600),
                })
              }
            />
          </label>
          <label>
            「继续闲聊」默认延长（分钟）
            <input
              id="rundown-extend-chat-minutes"
              type="number"
              min={1}
              max={360}
              value={plan.chat_entry.extend_chat_minutes ?? 15}
              onChange={(e) =>
                updateChatEntry({
                  extend_chat_minutes: parseIntOrFallback(e.target.value, 15, 1, 360),
                })
              }
            />
          </label>
          <label>
            投票切入游戏默认时长（分钟）
            <input
              id="rundown-default-game-minutes"
              type="number"
              min={1}
              max={360}
              value={plan.chat_entry.default_game_duration_minutes ?? 20}
              onChange={(e) =>
                updateChatEntry({
                  default_game_duration_minutes: parseIntOrFallback(e.target.value, 20, 1, 360),
                })
              }
            />
          </label>
          <label>
            最低有效票数（不足则兜底推进）
            <input
              id="rundown-min-votes"
              type="number"
              min={0}
              max={100}
              value={plan.chat_entry.min_votes}
              onChange={(e) =>
                updateChatEntry({
                  min_votes: parseIntOrFallback(e.target.value, 0, 0, 100),
                })
              }
            />
          </label>
          <label>
            #投票选游戏 触发门槛人数
            <input
              id="rundown-vote-trigger-threshold"
              type="number"
              min={1}
              max={20}
              value={plan.chat_entry.vote_trigger_threshold}
              onChange={(e) =>
                updateChatEntry({
                  vote_trigger_threshold: parseIntOrFallback(e.target.value, 2, 1, 20),
                })
              }
            />
          </label>
          <label>
            热聊自适应阈值（条 / 2分钟）
            <input
              id="rundown-hot-threshold"
              type="number"
              min={1}
              max={200}
              value={plan.chat_entry.chat_hot_threshold_per_2m}
              onChange={(e) =>
                updateChatEntry({
                  chat_hot_threshold_per_2m: parseIntOrFallback(e.target.value, 12, 1, 200),
                })
              }
            />
          </label>
        </div>

        <div className={styles.checksRow}>
          <label className="check-field">
            <input
              id="rundown-include-continue"
              type="checkbox"
              checked={plan.chat_entry.include_continue_chat}
              onChange={(e) => updateChatEntry({ include_continue_chat: e.target.checked })}
            />
            投票附带「继续闲聊」选项
          </label>
          <label className="check-field">
            <input
              id="rundown-avoid-repeat"
              type="checkbox"
              checked={plan.chat_entry.avoid_repeat}
              onChange={(e) => updateChatEntry({ avoid_repeat: e.target.checked })}
            />
            尽量避开刚玩过的项目
          </label>
          <label className="check-field">
            <input
              id="rundown-allow-vote-trigger"
              type="checkbox"
              checked={plan.chat_entry.allow_chat_vote_trigger}
              onChange={(e) => updateChatEntry({ allow_chat_vote_trigger: e.target.checked })}
            />
            允许弹幕发送 #投票选游戏
          </label>
          <label className="check-field">
            <input
              id="rundown-allow-chat-extend"
              type="checkbox"
              checked={plan.chat_entry.allow_chat_extend}
              onChange={(e) => updateChatEntry({ allow_chat_extend: e.target.checked })}
            />
            允许弹幕发送 #继续聊 延长闲聊
          </label>
          <label className="check-field">
            <input
              id="rundown-chat-adaptive"
              type="checkbox"
              checked={plan.chat_entry.chat_adaptive_enabled}
              onChange={(e) => updateChatEntry({ chat_adaptive_enabled: e.target.checked })}
            />
            启用热聊自适应提示
          </label>
          <label className="check-field">
            <input
              id="rundown-hold-on-last"
              type="checkbox"
              checked={plan.hold_on_last}
              onChange={(e) => onChangePlan({ ...plan, hold_on_last: e.target.checked })}
            />
            末段结束后停留保持
          </label>
          <label className="check-field">
            <input
              id="rundown-auto-switch-layout"
              type="checkbox"
              checked={plan.auto_switch_layout}
              onChange={(e) => onChangePlan({ ...plan, auto_switch_layout: e.target.checked })}
            />
            换段时自动切换 OBS 布局
          </label>
        </div>
      </article>

      <article className="win-group-box" id="rundown-segments-card">
        <div className="panel-heading">
          <div className="win-group-title" style={{ margin: 0 }}>
            📋 默认兜底节目单环节（{plan.segments.length} 段）
          </div>
          <button
            type="button"
            id="rundown-add-segment-btn"
            className="win-btn"
            disabled={saving}
            onClick={addSegment}
          >
            ＋ 添加环节
          </button>
        </div>
        <p className="hint" style={{ marginTop: 4 }}>
          当观众投票未达最低票数或处于默认流程时，将按以下顺序自动推进并切换对应 OBS 布局。
        </p>

        <div className={styles.segmentList}>
          {plan.segments.map((seg, idx) => (
            <div key={seg.id} className={styles.segmentCard} data-rundown-segment={seg.id}>
              <div className={styles.segmentHeader}>
                <div className={styles.segmentTitleBadge}>
                  <span className={styles.segmentIndex}>{idx + 1}</span>
                  <span>{seg.title || "未命名环节"}</span>
                  <span
                    className={`${styles.layoutTag} ${
                      seg.obs_layout === "game" ? styles.layoutTagGame : styles.layoutTagChat
                    }`}
                  >
                    {seg.obs_layout === "game" ? "🎮 game" : "💬 chat"}
                  </span>
                </div>
                <div className={styles.rowActions}>
                  <button
                    type="button"
                    data-segment-up={seg.id}
                    disabled={idx === 0 || saving}
                    onClick={() => moveSegment(idx, -1)}
                  >
                    ↑ 上移
                  </button>
                  <button
                    type="button"
                    data-segment-down={seg.id}
                    disabled={idx === plan.segments.length - 1 || saving}
                    onClick={() => moveSegment(idx, 1)}
                  >
                    ↓ 下移
                  </button>
                  <button
                    type="button"
                    className="danger"
                    data-segment-remove={seg.id}
                    disabled={plan.segments.length <= 1 || saving}
                    onClick={() => removeSegment(idx)}
                  >
                    删除
                  </button>
                </div>
              </div>

              <div className={styles.compactFormGrid}>
                <label>
                  环节名称
                  <input
                    type="text"
                    data-segment-title={seg.id}
                    maxLength={80}
                    value={seg.title}
                    onChange={(e) => updateSegment(idx, { title: e.target.value })}
                  />
                </label>
                <label>
                  环节类型
                  <select
                    data-segment-kind={seg.id}
                    value={seg.kind}
                    onChange={(e) => {
                      const nextKind = e.target.value as RundownSegmentKind;
                      const match = SEGMENT_KIND_OPTIONS.find((opt) => opt.kind === nextKind);
                      updateSegment(idx, {
                        kind: nextKind,
                        obs_layout: match?.defaultLayout ?? seg.obs_layout,
                      });
                    }}
                  >
                    {SEGMENT_KIND_OPTIONS.map((opt) => (
                      <option key={opt.kind} value={opt.kind}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  计划时长（分钟）
                  <input
                    type="number"
                    data-segment-duration={seg.id}
                    min={1}
                    max={360}
                    value={seg.duration_minutes}
                    onChange={(e) =>
                      updateSegment(idx, {
                        duration_minutes: parseIntOrFallback(e.target.value, 15, 1, 360),
                      })
                    }
                  />
                </label>
                <label>
                  OBS 布局模式
                  <select
                    data-segment-layout={seg.id}
                    value={seg.obs_layout}
                    onChange={(e) =>
                      updateSegment(idx, { obs_layout: e.target.value as ObsLayoutId })
                    }
                  >
                    <option value="chat">💬 闲聊全屏布局 (chat)</option>
                    <option value="game">🎮 游戏画中画布局 (game)</option>
                  </select>
                </label>
              </div>

              <div className={styles.checksRow}>
                <label className="check-field">
                  <input
                    type="checkbox"
                    data-segment-autostart={seg.id}
                    checked={seg.auto_start_show}
                    onChange={(e) => updateSegment(idx, { auto_start_show: e.target.checked })}
                  />
                  切入时自动开局
                </label>
                <label className="check-field">
                  <input
                    type="checkbox"
                    data-segment-graceful={seg.id}
                    checked={seg.graceful_finish}
                    onChange={(e) => updateSegment(idx, { graceful_finish: e.target.checked })}
                  />
                  等待当前轮次自然结束后换段
                </label>
              </div>

              {seg.kind === "draw_and_guess" && (
                <div className={styles.segmentFineBox}>
                  <strong style={{ fontSize: 12 }}>🎨 你画我猜环节专属参数</strong>
                  <div className={styles.compactFormGrid}>
                    <label>
                      单轮作画时长（秒）
                      <input
                        type="number"
                        data-segment-dg-duration={seg.id}
                        min={15}
                        max={300}
                        value={Number(seg.settings?.round_duration_sec ?? seg.settings?.duration_sec ?? 60)}
                        onChange={(e) =>
                          updateSegmentSettings(idx, {
                            round_duration_sec: parseIntOrFallback(e.target.value, 60, 15, 300),
                          })
                        }
                      />
                    </label>
                    <label>
                      主题词库（逗号分隔，留空随机）
                      <input
                        type="text"
                        data-segment-dg-themes={seg.id}
                        placeholder="美食, 二次元, 日常"
                        value={segmentThemesText(seg.settings || {})}
                        onChange={(e) =>
                          updateSegmentSettings(idx, {
                            themes: e.target.value
                              .split(/[,，]/)
                              .map((s) => s.trim())
                              .filter(Boolean),
                          })
                        }
                      />
                    </label>
                    <label className="check-field" style={{ alignSelf: "end" }}>
                      <input
                        type="checkbox"
                        data-segment-dg-llm={seg.id}
                        checked={Boolean(seg.settings?.use_llm ?? true)}
                        onChange={(e) => updateSegmentSettings(idx, { use_llm: e.target.checked })}
                      />
                      启用 LLM 现画现猜
                    </label>
                  </div>
                </div>
              )}

              {seg.kind === "turtle_soup" && (
                <div className={styles.segmentFineBox}>
                  <strong style={{ fontSize: 12 }}>🐢 海龟汤环节专属参数</strong>
                  <div className={styles.compactFormGrid}>
                    <label>
                      题材偏好
                      <input
                        type="text"
                        data-segment-ts-genre={seg.id}
                        placeholder="mystery / daily / scifi"
                        value={String(seg.settings?.genre ?? "")}
                        onChange={(e) => updateSegmentSettings(idx, { genre: e.target.value })}
                      />
                    </label>
                    <label>
                      单汤最大提问数
                      <input
                        type="number"
                        data-segment-ts-maxq={seg.id}
                        min={5}
                        max={60}
                        value={Number(seg.settings?.max_questions ?? 20)}
                        onChange={(e) =>
                          updateSegmentSettings(idx, {
                            max_questions: parseIntOrFallback(e.target.value, 20, 5, 60),
                          })
                        }
                      />
                    </label>
                    <label className="check-field" style={{ alignSelf: "end" }}>
                      <input
                        type="checkbox"
                        data-segment-ts-llm={seg.id}
                        checked={Boolean(seg.settings?.use_llm ?? true)}
                        onChange={(e) => updateSegmentSettings(idx, { use_llm: e.target.checked })}
                      />
                      启用 LLM 现编与判题
                    </label>
                  </div>
                </div>
              )}

              {seg.kind === "roast_radio" && (
                <div className={styles.segmentFineBox}>
                  <strong style={{ fontSize: 12 }}>🎙️ 锐评辩论环节参数</strong>
                  <div className={styles.compactFormGrid}>
                    <label>
                      电台模式
                      <select
                        value={String(seg.settings?.mode ?? "debate")}
                        onChange={(e) => updateSegmentSettings(idx, { mode: e.target.value })}
                      >
                        <option value="debate">正反方辩论台</option>
                        <option value="roast">单口锐评</option>
                      </select>
                    </label>
                    <label>
                      预设话题（可选）
                      <input
                        type="text"
                        placeholder="例如：熬夜写代码配白饭还是咖啡"
                        value={String(seg.settings?.topic ?? "")}
                        onChange={(e) => updateSegmentSettings(idx, { topic: e.target.value })}
                      />
                    </label>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      </article>

      <div className={styles.saveBar}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span
            id="rundown-dirty-badge"
            className={dirty ? styles.dirtyPill : styles.cleanPill}
          >
            {dirty ? "节目单有未保存修改" : "节目单已同步"}
          </span>
          {statusMessage && (
            <span
              id="rundown-save-state"
              className={statusError ? "hint error-text" : "hint"}
              role="status"
            >
              {statusMessage}
            </span>
          )}
        </div>
        <div className="actions">
          {onResetDraft && (
            <button
              type="button"
              id="rundown-reset-draft-btn"
              className="win-btn"
              disabled={!dirty || saving}
              onClick={onResetDraft}
            >
              撤销修改
            </button>
          )}
          <button
            type="button"
            id="rundown-save-btn"
            className="primary win-btn"
            disabled={saving}
            onClick={onSave}
          >
            {saving ? "保存中…" : "保存节目单与投票配置"}
          </button>
        </div>
      </div>
    </div>
  );
}
