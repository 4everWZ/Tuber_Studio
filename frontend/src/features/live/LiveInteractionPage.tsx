import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut, apiRequest, studioPath } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import type {
  LiveFineSettings,
  RundownGameCatalogItem,
  RundownPlan,
  RundownPresetItem,
  RundownStatusResponse,
} from "./consoleTypes";
import styles from "./LiveRulesScene.module.css";
import { createDefaultRundownPlan, RundownBuilderPanel } from "./RundownBuilderPanel";
import type {
  GameStatusData,
  InteractiveRulesData,
  LiveScheduleItem,
  StardewActionReceipt,
  StardewDeviceState,
  StardewDeviceStatus,
} from "./types";

export type InteractionSubTab = "rundown" | "schedules" | "rules" | "games";

const SUB_TABS: ReadonlyArray<{ id: InteractionSubTab; label: string }> = [
  { id: "rundown", label: "节目单与投票" },
  { id: "schedules", label: "排班与加班参数" },
  { id: "rules", label: "礼物与弹幕规则" },
  { id: "games", label: "实机游戏与设备" },
];

const STARDEW_ACTIONS = [
  { action: "water_crops", label: "💧 浇水" },
  { action: "chop_wood", label: "🪓 砍树" },
  { action: "go_fishing", label: "🎣 钓鱼" },
  { action: "harvest", label: "🌾 收获作物" },
  { action: "eat_snack", label: "🍪 吃点心补体力" },
] as const;

const PVZ_ACTIONS = [
  { action: "plant_sunflower", label: "🌻 种向日葵" },
  { action: "plant_peashooter", label: "🌱 种豌豆射手" },
  { action: "plant_wallnut", label: "🥜 种坚果墙" },
  { action: "use_cherry_bomb", label: "💣 樱桃炸弹" },
] as const;

const WEEKDAY_CHOICES: ReadonlyArray<[number, string]> = [
  [0, "一"],
  [1, "二"],
  [2, "三"],
  [3, "四"],
  [4, "五"],
  [5, "六"],
  [6, "日"],
];

function formatLocalDateTime(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function defaultOnceWindow(): { start: string; end: string } {
  const now = Date.now();
  return {
    start: formatLocalDateTime(now + 5 * 60 * 1000),
    end: formatLocalDateTime(now + 65 * 60 * 1000),
  };
}

function numStr(value: unknown, fallback: number): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && value.trim() !== "") return value;
  return String(fallback);
}

function parseNumberOr(raw: string, fallback: number): number {
  if (raw.trim() === "") return fallback;
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

function parseIntOr(raw: string, fallback: number): number {
  const n = Number.parseInt(raw, 10);
  return Number.isNaN(n) ? fallback : n;
}

export interface LiveInteractionPageProps {
  visible?: boolean;
  initialTab?: InteractionSubTab;
  onFeedback?: (message: string, error?: boolean) => void;
}

export function LiveInteractionPage({
  visible = true,
  initialTab = "rundown",
  onFeedback,
}: LiveInteractionPageProps) {
  const [activeTab, setActiveTab] = useState<InteractionSubTab>(() => {
    if (typeof window !== "undefined" && window.location.hash.includes("live-game-panel")) {
      return "games";
    }
    return initialTab;
  });

  // ── Rundown Builder State ──
  const [savedRundownPlan, setSavedRundownPlan] = useState<RundownPlan>(() =>
    createDefaultRundownPlan()
  );
  const [draftRundownPlan, setDraftRundownPlan] = useState<RundownPlan>(() =>
    createDefaultRundownPlan()
  );
  const [rundownPresets, setRundownPresets] = useState<RundownPresetItem[]>([]);
  const [rundownCatalog, setRundownCatalog] = useState<RundownGameCatalogItem[]>([]);
  const [rundownDirty, setRundownDirty] = useState(false);
  const [savingRundown, setSavingRundown] = useState(false);
  const [rundownStateText, setRundownStateText] = useState("");
  const [rundownStateError, setRundownStateError] = useState(false);

  // ── Schedules CRUD State ──
  const [schedules, setSchedules] = useState<LiveScheduleItem[]>([]);
  const [scheduleBusy, setScheduleBusy] = useState(false);
  const [scheduleStatusText, setScheduleStatusText] = useState("");
  const [scheduleErrorText, setScheduleErrorText] = useState("");
  const [scheduleLabel, setScheduleLabel] = useState("晚间直播");
  const [scheduleKind, setScheduleKind] = useState<"duration" | "daily_window" | "once_window">(
    "duration"
  );
  const [scheduleDuration, setScheduleDuration] = useState("60");
  const [scheduleStartLocal, setScheduleStartLocal] = useState("20:00");
  const [scheduleEndLocal, setScheduleEndLocal] = useState("22:00");
  const [scheduleOnceStart, setScheduleOnceStart] = useState(() => defaultOnceWindow().start);
  const [scheduleOnceEnd, setScheduleOnceEnd] = useState(() => defaultOnceWindow().end);
  const [scheduleWeekdays, setScheduleWeekdays] = useState<number[]>([0, 1, 2, 3, 4]);
  const [scheduleTimezone, setScheduleTimezone] = useState("Asia/Shanghai");
  const [scheduleBaseMinutes, setScheduleBaseMinutes] = useState("60");
  const [scheduleMaxOvertime, setScheduleMaxOvertime] = useState("120");
  const [scheduleExtStep, setScheduleExtStep] = useState("15");
  const [scheduleAutoPause, setScheduleAutoPause] = useState(true);

  // ── Fine Settings State ──
  const [fineRaw, setFineRaw] = useState<LiveFineSettings | null>(null);
  const [fineDirty, setFineDirty] = useState(false);
  const [savingFine, setSavingFine] = useState(false);
  const [fineStateText, setFineStateText] = useState("");
  const [fineStateError, setFineStateError] = useState(false);

  // Cold director fine fields
  const [cdTier1Delay, setCdTier1Delay] = useState("90");
  const [cdTier2Delay, setCdTier2Delay] = useState("300");
  const [cdTier3Delay, setCdTier3Delay] = useState("900");
  const [cdTier1Cooldown, setCdTier1Cooldown] = useState("90");
  const [cdTier2Cooldown, setCdTier2Cooldown] = useState("150");
  const [cdTier3Cadence, setCdTier3Cadence] = useState("300");

  // Overtime fine fields
  const [otThreshold, setOtThreshold] = useState("100");
  const [otGiftMultiplier, setOtGiftMultiplier] = useState("1.5");
  const [otChatEnergy, setOtChatEnergy] = useState("6");
  const [otChatCooldown, setOtChatCooldown] = useState("15");
  const [otDecayPerMin, setOtDecayPerMin] = useState("4");
  const [otDefaultBase, setOtDefaultBase] = useState("60");
  const [otDefaultMax, setOtDefaultMax] = useState("120");
  const [otDefaultStep, setOtDefaultStep] = useState("15");

  // Draw & Guess / Turtle Soup fine fields
  const [dgHintL2Ratio, setDgHintL2Ratio] = useState("0.35");
  const [dgHintL3Ratio, setDgHintL3Ratio] = useState("0.7");
  const [tsMaxQuestions, setTsMaxQuestions] = useState("20");
  const [tsAutoHintInterval, setTsAutoHintInterval] = useState("5");

  // ── Interactive Rules State ──
  const [rules, setRules] = useState<InteractiveRulesData | null>(null);
  const [rulesDirty, setRulesDirty] = useState(false);
  const [savingRules, setSavingRules] = useState(false);
  const [rulesStateText, setRulesStateText] = useState("");
  const [rulesStateError, setRulesStateError] = useState(false);
  const [songMode, setSongMode] = useState("free");
  const [songCoin, setSongCoin] = useState("0");
  const [songGifts, setSongGifts] = useState("");
  const [dgEnabled, setDgEnabled] = useState(true);
  const [dgMode, setDgMode] = useState("gift_only");
  const [dgCoin, setDgCoin] = useState("10");
  const [dgGifts, setDgGifts] = useState("画笔, 小心心, 辣条");
  const [dgDuration, setDgDuration] = useState("60");

  // ── Games & Device State ──
  const [gameStatus, setGameStatus] = useState<GameStatusData | null>(null);
  const [gameSummaryError, setGameSummaryError] = useState("");
  const [gameBusy, setGameBusy] = useState(false);
  const [device, setDevice] = useState<StardewDeviceState | null>(null);
  const [deviceStateText, setDeviceStateText] = useState("正在读取游戏状态…");
  const [deviceResultText, setDeviceResultText] = useState("");
  const [deviceBusyId, setDeviceBusyId] = useState<string | null>(null);

  // Refs for dirty protection & stale response cancellation
  const rundownDirtyRef = useRef(rundownDirty);
  rundownDirtyRef.current = rundownDirty;
  const savingRundownRef = useRef(savingRundown);
  savingRundownRef.current = savingRundown;
  const rundownRequestRef = useRef(0);

  const fineDirtyRef = useRef(fineDirty);
  fineDirtyRef.current = fineDirty;
  const savingFineRef = useRef(savingFine);
  savingFineRef.current = savingFine;
  const fineRequestRef = useRef(0);

  const rulesDirtyRef = useRef(rulesDirty);
  rulesDirtyRef.current = rulesDirty;
  const savingRulesRef = useRef(savingRules);
  savingRulesRef.current = savingRules;
  const rulesRequestRef = useRef(0);

  const deviceReadRef = useRef(0);
  const deviceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const deviceBusyRef = useRef<string | null>(null);
  deviceBusyRef.current = deviceBusyId;
  const mountedRef = useRef(true);

  useUnsavedGuard("live-interaction-rundown", rundownDirty);
  useUnsavedGuard("live-interaction-fine", fineDirty);
  useUnsavedGuard("live-interaction-rules", rulesDirty);

  useEffect(() => {
    mountedRef.current = true;
    const onHashChange = () => {
      if (typeof window !== "undefined" && window.location.hash.includes("live-game-panel")) {
        setActiveTab("games");
      }
    };
    if (typeof window !== "undefined") {
      window.addEventListener("hashchange", onHashChange);
    }
    return () => {
      mountedRef.current = false;
      if (typeof window !== "undefined") {
        window.removeEventListener("hashchange", onHashChange);
      }
      if (deviceTimerRef.current) clearTimeout(deviceTimerRef.current);
    };
  }, []);

  const populateRulesForm = useCallback((data: InteractiveRulesData) => {
    const sr = data.song_request;
    setSongMode(sr?.mode || "free");
    setSongCoin(String(sr?.min_coin ?? 0));
    setSongGifts((sr?.gift_names || []).join(", "));

    const dg = data.draw_and_guess;
    setDgEnabled(Boolean(dg?.enabled));
    setDgMode(dg?.trigger_mode || "gift_only");
    setDgCoin(String(dg?.min_coin ?? 10));
    setDgGifts((dg?.gift_names || []).join(", "));
    setDgDuration(String(dg?.round_duration_sec ?? 60));
  }, []);

  const populateFineForm = useCallback((data: LiveFineSettings) => {
    const cd = data.cold_director || {};
    setCdTier1Delay(numStr(cd.tier1_delay_sec, 90));
    setCdTier2Delay(numStr(cd.tier2_delay_sec, 300));
    setCdTier3Delay(numStr(cd.tier3_delay_sec, 900));
    setCdTier1Cooldown(numStr(cd.tier1_cooldown_sec, 90));
    setCdTier2Cooldown(numStr(cd.tier2_cooldown_sec, 150));
    setCdTier3Cadence(numStr(cd.tier3_cadence_sec ?? cd.tier3_cadence, 300));

    const ot = data.overtime || {};
    setOtThreshold(numStr(ot.threshold, 100));
    setOtGiftMultiplier(numStr(ot.gift_coin_multiplier, 1.5));
    setOtChatEnergy(numStr(ot.chat_base_energy, 6));
    setOtChatCooldown(numStr(ot.chat_cooldown_seconds, 15));
    setOtDecayPerMin(numStr(ot.decay_per_minute, 4));
    setOtDefaultBase(numStr(ot.default_base_duration_minutes, 60));
    setOtDefaultMax(numStr(ot.default_max_overtime_minutes, 120));
    setOtDefaultStep(numStr(ot.default_extension_step_minutes, 15));

    const dg = data.draw_and_guess || {};
    setDgHintL2Ratio(numStr(dg.hint_l2_ratio, 0.35));
    setDgHintL3Ratio(numStr(dg.hint_l3_ratio, 0.7));

    const ts = data.turtle_soup || {};
    setTsMaxQuestions(numStr(ts.max_questions, 20));
    setTsAutoHintInterval(numStr(ts.auto_hint_every_n_questions, 5));
  }, []);

  const fetchRundown = useCallback(async () => {
    const seq = ++rundownRequestRef.current;
    try {
      const data = await apiRequest<Partial<RundownStatusResponse>>("/api/v1/live/rundown", {
        method: "GET",
      });
      if (!mountedRef.current || seq !== rundownRequestRef.current) return;
      if (Array.isArray(data?.presets)) setRundownPresets(data.presets);
      if (Array.isArray(data?.game_catalog)) setRundownCatalog(data.game_catalog);
      if (data?.plan && typeof data.plan === "object" && Array.isArray(data.plan.segments)) {
        setSavedRundownPlan(structuredClone(data.plan));
        if (!rundownDirtyRef.current && !savingRundownRef.current) {
          setDraftRundownPlan(structuredClone(data.plan));
        }
      }
      setRundownStateError(false);
    } catch {
      // Rundown endpoint optional in legacy mock tests
    }
  }, []);

  const fetchSchedules = useCallback(async () => {
    try {
      const raw = await apiRequest<LiveScheduleItem[] | { schedules?: LiveScheduleItem[] }>(
        "/api/v1/live/schedules",
        { method: "GET" }
      );
      if (!mountedRef.current) return;
      const list = Array.isArray(raw)
        ? raw
        : Array.isArray(raw?.schedules)
          ? raw.schedules
          : [];
      setSchedules(list);
    } catch {
      // Optional in standalone rule-only tests
    }
  }, []);

  const fetchFineSettings = useCallback(async () => {
    const seq = ++fineRequestRef.current;
    try {
      const data = await apiRequest<Partial<LiveFineSettings>>("/api/v1/live/fine-settings", {
        method: "GET",
      });
      if (!mountedRef.current || seq !== fineRequestRef.current) return;
      if (data && typeof data === "object" && (data.cold_director || data.overtime)) {
        const normalized: LiveFineSettings = {
          cold_director: data.cold_director || {},
          overtime: data.overtime || {},
          draw_and_guess: data.draw_and_guess || {},
          turtle_soup: data.turtle_soup || {},
        };
        setFineRaw(normalized);
        if (!fineDirtyRef.current && !savingFineRef.current) {
          populateFineForm(normalized);
        }
      }
    } catch {
      // Optional in legacy tests
    }
  }, [populateFineForm]);

  const fetchRules = useCallback(async () => {
    const seq = ++rulesRequestRef.current;
    try {
      const data = await apiGet<"/api/v1/live/interactive-rules", InteractiveRulesData>(
        "/api/v1/live/interactive-rules"
      );
      if (!mountedRef.current || seq !== rulesRequestRef.current) return;
      setRules(data);
      if (!rulesDirtyRef.current && !savingRulesRef.current) {
        populateRulesForm(data);
      }
      setRulesStateError(false);
    } catch (error) {
      if (!mountedRef.current || seq !== rulesRequestRef.current) return;
      setRulesStateText(`规则读取失败：${errorMessage(error)}`);
      setRulesStateError(true);
    }
  }, [populateRulesForm]);

  const fetchGameStatus = useCallback(async () => {
    try {
      const data = await apiGet<"/api/v1/live/games/status", GameStatusData>(
        "/api/v1/live/games/status"
      );
      if (!mountedRef.current) return;
      setGameStatus(data);
      setGameSummaryError("");
    } catch (error) {
      if (!mountedRef.current) return;
      setGameSummaryError(`游戏状态读取失败：${errorMessage(error)}`);
    }
  }, []);

  const fetchDevice = useCallback(async () => {
    if (deviceTimerRef.current) {
      clearTimeout(deviceTimerRef.current);
      deviceTimerRef.current = null;
    }
    const seq = ++deviceReadRef.current;
    try {
      const { online, state, configured } = await apiGet<
        "/api/v1/live/games/device/stardew",
        StardewDeviceStatus
      >("/api/v1/live/games/device/stardew");
      if (!mountedRef.current || seq !== deviceReadRef.current) return;
      setDevice(online ? state : null);
      if (!configured) {
        setDeviceStateText("尚未配置实机通信目录");
      } else if (!online || !state) {
        setDeviceStateText("游戏已断开");
      } else {
        const f = state.farm;
        const who = state.owner === "operator" ? "号主控制" : "工坊控制";
        setDeviceStateText(
          f
            ? `${f.save} · ${f.location} (${f.x}, ${f.y}) · ${f.season}第 ${f.day} 天 · ${f.time} · 体力 ${f.stamina} · ${f.money}g · 手持 ${f.inventory[f.slot]?.item || "空"} · ${who}`
            : `等待进入存档 · ${who}`
        );
      }
      if (configured && visible) {
        deviceTimerRef.current = setTimeout(() => {
          if (mountedRef.current && visible && !(typeof document !== "undefined" && document.hidden)) {
            void fetchDevice();
          }
        }, 2000);
      }
    } catch (error) {
      if (!mountedRef.current || seq !== deviceReadRef.current) return;
      setDevice(null);
      setDeviceStateText(`实机读取失败：${errorMessage(error)}`);
    }
  }, [visible]);

  useEffect(() => {
    if (!visible) {
      if (deviceTimerRef.current) {
        clearTimeout(deviceTimerRef.current);
        deviceTimerRef.current = null;
      }
      return undefined;
    }
    void Promise.all([
      fetchRundown(),
      fetchSchedules(),
      fetchFineSettings(),
      fetchRules(),
      fetchGameStatus(),
      fetchDevice(),
    ]);
    return () => {
      if (deviceTimerRef.current) {
        clearTimeout(deviceTimerRef.current);
        deviceTimerRef.current = null;
      }
    };
  }, [visible, fetchRundown, fetchSchedules, fetchFineSettings, fetchRules, fetchGameStatus, fetchDevice]);

  // ── Rundown Handlers ──
  const handleChangeRundownPlan = (nextPlan: RundownPlan) => {
    setDraftRundownPlan(nextPlan);
    setRundownDirty(true);
    rundownDirtyRef.current = true;
    setRundownStateText("");
  };

  const handleSaveRundown = async () => {
    if (savingRundown) return;
    setSavingRundown(true);
    savingRundownRef.current = true;
    ++rundownRequestRef.current;
    setRundownStateText("正在保存…");
    setRundownStateError(false);

    try {
      const saved = await apiRequest<Partial<RundownStatusResponse> | RundownPlan>(
        "/api/v1/live/rundown",
        {
          method: "PUT",
          body: draftRundownPlan,
        }
      );
      const nextPlan =
        saved && typeof saved === "object" && "plan" in saved && saved.plan
          ? saved.plan
          : draftRundownPlan;
      setSavedRundownPlan(structuredClone(nextPlan));
      setDraftRundownPlan(structuredClone(nextPlan));
      setRundownDirty(false);
      rundownDirtyRef.current = false;
      setRundownStateText("节目单与投票配置已保存。");
      setRundownStateError(false);
      onFeedback?.("节目单与投票配置保存成功！");
    } catch (error) {
      setRundownStateText(`保存失败：${errorMessage(error)}`);
      setRundownStateError(true);
      onFeedback?.(errorMessage(error), true);
    } finally {
      setSavingRundown(false);
      savingRundownRef.current = false;
    }
  };

  const handleResetRundownDraft = () => {
    setDraftRundownPlan(structuredClone(savedRundownPlan));
    setRundownDirty(false);
    rundownDirtyRef.current = false;
    setRundownStateText("");
    setRundownStateError(false);
  };

  // ── Schedules CRUD Handlers ──
  const handleCreateSchedule = async (event: FormEvent) => {
    event.preventDefault();
    if (scheduleBusy) return;
    setScheduleBusy(true);
    setScheduleErrorText("");
    setScheduleStatusText("");
    try {
      const parsedDuration = parseIntOr(scheduleDuration, 60);
      const parsedBase = parseIntOr(scheduleBaseMinutes, 60);
      const parsedMaxOt = parseIntOr(scheduleMaxOvertime, 120);
      const parsedStep = parseIntOr(scheduleExtStep, 15);

      const body: Record<string, unknown> = {
        kind: scheduleKind,
        label: scheduleLabel.trim() || "直播排班",
        timezone: scheduleTimezone.trim() || "Asia/Shanghai",
        base_duration_minutes: parsedBase,
        max_overtime_minutes: parsedMaxOt,
        extension_step_minutes: parsedStep,
        auto_pause_operations: scheduleAutoPause,
      };
      if (scheduleKind === "duration") {
        body.duration_minutes = parsedDuration;
      } else if (scheduleKind === "once_window") {
        body.start_local = scheduleOnceStart.trim();
        body.end_local = scheduleOnceEnd.trim();
      } else {
        body.start_time = scheduleStartLocal.trim();
        body.end_time = scheduleEndLocal.trim();
        body.weekdays = scheduleWeekdays;
      }

      await apiRequest("/api/v1/live/schedules", { method: "POST", body });
      setScheduleStatusText("已新增直播排班。");
      onFeedback?.("已新增直播排班。");
      await fetchSchedules();
    } catch (error) {
      const msg = errorMessage(error);
      setScheduleErrorText(msg);
      onFeedback?.(msg, true);
    } finally {
      setScheduleBusy(false);
    }
  };

  const handleToggleSchedule = async (item: LiveScheduleItem) => {
    if (scheduleBusy) return;
    setScheduleBusy(true);
    setScheduleErrorText("");
    try {
      await apiRequest(`/api/v1/live/schedules/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        body: { enabled: !(item.enabled ?? true) },
      });
      await fetchSchedules();
    } catch (error) {
      const msg = errorMessage(error);
      setScheduleErrorText(msg);
      onFeedback?.(msg, true);
    } finally {
      setScheduleBusy(false);
    }
  };

  const handleDeleteSchedule = async (id: string) => {
    if (scheduleBusy) return;
    setScheduleBusy(true);
    setScheduleErrorText("");
    try {
      await apiRequest(`/api/v1/live/schedules/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      setScheduleStatusText("已删除直播排班。");
      await fetchSchedules();
    } catch (error) {
      const msg = errorMessage(error);
      setScheduleErrorText(msg);
      onFeedback?.(msg, true);
    } finally {
      setScheduleBusy(false);
    }
  };

  // ── Fine Settings Save Handler ──
  const handleSaveFineSettings = async (event: FormEvent) => {
    event.preventDefault();
    if (savingFine) return;
    setSavingFine(true);
    savingFineRef.current = true;
    ++fineRequestRef.current;
    setFineStateText("正在保存…");
    setFineStateError(false);

    const l2Ratio = parseNumberOr(dgHintL2Ratio, 0.35);
    const l3Ratio = parseNumberOr(dgHintL3Ratio, 0.7);
    const maxQ = parseIntOr(tsMaxQuestions, 20);
    const autoHintFreq = parseIntOr(tsAutoHintInterval, 5);

    const payload: LiveFineSettings = {
      cold_director: {
        ...(fineRaw?.cold_director || {}),
        tier1_delay_sec: parseNumberOr(cdTier1Delay, 90),
        tier2_delay_sec: parseNumberOr(cdTier2Delay, 300),
        tier3_delay_sec: parseNumberOr(cdTier3Delay, 900),
        tier1_cooldown_sec: parseNumberOr(cdTier1Cooldown, 90),
        tier2_cooldown_sec: parseNumberOr(cdTier2Cooldown, 150),
        tier3_cadence_sec: parseNumberOr(cdTier3Cadence, 300),
      },
      overtime: {
        ...(fineRaw?.overtime || {}),
        threshold: parseNumberOr(otThreshold, 100),
        gift_coin_multiplier: parseNumberOr(otGiftMultiplier, 1.5),
        chat_base_energy: parseNumberOr(otChatEnergy, 6),
        chat_cooldown_seconds: parseNumberOr(otChatCooldown, 15),
        decay_per_minute: parseNumberOr(otDecayPerMin, 4),
        default_base_duration_minutes: parseIntOr(otDefaultBase, 60),
        default_max_overtime_minutes: parseIntOr(otDefaultMax, 120),
        default_extension_step_minutes: parseIntOr(otDefaultStep, 15),
      },
      draw_and_guess: {
        ...(fineRaw?.draw_and_guess || {}),
        hint_l2_ratio: l2Ratio,
        hint_l3_ratio: l3Ratio,
      },
      turtle_soup: {
        ...(fineRaw?.turtle_soup || {}),
        max_questions: maxQ,
        auto_hint_every_n_questions: autoHintFreq,
      },
    };

    try {
      const saved = await apiRequest<LiveFineSettings>("/api/v1/live/fine-settings", {
        method: "PUT",
        body: payload,
      });
      if (saved && typeof saved === "object" && (saved.cold_director || saved.overtime)) {
        setFineRaw(saved);
        populateFineForm(saved);
      } else {
        setFineRaw(payload);
      }
      setFineDirty(false);
      fineDirtyRef.current = false;
      setFineStateText("冷场与加班精调参数已保存。");
      setFineStateError(false);
      onFeedback?.("冷场与加班精调参数已保存！");
    } catch (error) {
      setFineStateText(`保存失败：${errorMessage(error)}`);
      setFineStateError(true);
      onFeedback?.(errorMessage(error), true);
    } finally {
      setSavingFine(false);
      savingFineRef.current = false;
    }
  };

  // ── Interactive Rules Handler (preserving numeric 0 accurately) ──
  const handleSaveRules = async (event: FormEvent) => {
    event.preventDefault();
    if (savingRules) return;
    setSavingRules(true);
    savingRulesRef.current = true;
    ++rulesRequestRef.current;
    setRulesStateText("正在保存…");
    setRulesStateError(false);

    const parsedSongCoin = Number.parseInt(songCoin || "0", 10);
    const parsedDgCoin = Number.parseInt(dgCoin || "10", 10);
    const parsedDgDuration = Number.parseInt(dgDuration || "60", 10);

    const payload: InteractiveRulesData = {
      ...(rules || {}),
      song_request: {
        ...(rules?.song_request || {}),
        mode: songMode,
        min_coin: Number.isNaN(parsedSongCoin) ? 0 : parsedSongCoin,
        gift_names: songGifts
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean),
      },
      draw_and_guess: {
        ...(rules?.draw_and_guess || {}),
        enabled: dgEnabled,
        trigger_mode: dgMode,
        min_coin: Number.isNaN(parsedDgCoin) ? 0 : parsedDgCoin,
        gift_names: dgGifts
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean),
        round_duration_sec: Number.isNaN(parsedDgDuration) ? 60 : parsedDgDuration,
      },
    };

    try {
      const data = await apiPut<"/api/v1/live/interactive-rules", InteractiveRulesData>(
        "/api/v1/live/interactive-rules",
        { body: payload }
      );
      setRules(data);
      setRulesDirty(false);
      rulesDirtyRef.current = false;
      populateRulesForm(data);
      setRulesStateText("互动规则已保存。");
      setRulesStateError(false);
      onFeedback?.("互动与礼物门槛规则保存成功！");
    } catch (error) {
      setRulesStateText(`保存失败：${errorMessage(error)}`);
      setRulesStateError(true);
      onFeedback?.(errorMessage(error), true);
    } finally {
      setSavingRules(false);
      savingRulesRef.current = false;
    }
  };

  // ── Game & Device Handlers ──
  const handleSwitchGame = async (gameId: string) => {
    if (gameBusy) return;
    setGameBusy(true);
    try {
      const data = await apiPost<"/api/v1/live/games/switch", { spoken_text?: string }>(
        "/api/v1/live/games/switch",
        { body: { game_id: gameId } }
      );
      onFeedback?.(data.spoken_text || `已切换游戏: ${gameId}`);
      await fetchGameStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setGameBusy(false);
    }
  };

  const handleGameAction = async (action: string) => {
    if (gameBusy) return;
    setGameBusy(true);
    try {
      const data = await apiPost<"/api/v1/live/games/action", { message?: string }>(
        "/api/v1/live/games/action",
        { body: { action } }
      );
      onFeedback?.(`游戏指令【${action}】: ${data.message ?? ""}`);
      await fetchGameStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setGameBusy(false);
    }
  };

  const handleGameStep = async () => {
    if (gameBusy) return;
    setGameBusy(true);
    try {
      const data = await apiPost<
        "/api/v1/live/games/step",
        { message?: string; current_activity?: string }
      >("/api/v1/live/games/step");
      onFeedback?.(data.message || data.current_activity || "游戏已推进一步");
      await fetchGameStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setGameBusy(false);
    }
  };

  const handleTransferControl = async () => {
    if (gameBusy || !gameStatus) return;
    setGameBusy(true);
    try {
      const owner = gameStatus.owner === "operator" ? "studio" : "operator";
      const next = await apiPost<"/api/v1/live/games/control", GameStatusData>(
        "/api/v1/live/games/control",
        { body: { owner } }
      );
      setGameStatus(next);
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setGameBusy(false);
    }
  };

  const handleDeviceAction = async (action: string) => {
    if (action === "refresh") {
      await fetchDevice();
      return;
    }
    if (!device || (deviceBusyRef.current && action !== "takeover")) return;
    const state = device;
    const id =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `act-${Date.now()}`;
    setDeviceBusyId(id);
    deviceBusyRef.current = id;
    setDeviceResultText("等待游戏读回…");

    try {
      const command: Record<string, unknown> = {
        session: state.session,
        id,
        epoch: state.epoch,
        action,
      };
      if (action.endsWith("_front") && state.farm?.front) {
        command.target = state.farm.front.key;
        command.item = state.farm.inventory[state.farm.slot]?.item;
      }
      let receipt = await apiPost<"/api/v1/live/games/device/stardew/actions", StardewActionReceipt>(
        "/api/v1/live/games/device/stardew/actions",
        { body: command as never }
      );
      for (let i = 0; receipt.status === "pending" && i < 24; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 200));
        receipt = await apiGet<
          "/api/v1/live/games/device/stardew/actions/{session}/{identity}",
          StardewActionReceipt
        >("/api/v1/live/games/device/stardew/actions/{session}/{identity}", {
          pathParams: { session: state.session, identity: id },
        });
      }
      if (deviceBusyRef.current === id) {
        setDeviceResultText(
          receipt.status === "succeeded"
            ? "游戏已确认。"
            : `${receipt.status} · ${receipt.reason || id}`
        );
      }
    } catch (error) {
      if (deviceBusyRef.current === id) {
        setDeviceResultText(`${errorMessage(error)} · 动作编号 ${id}`);
      }
    } finally {
      if (deviceBusyRef.current === id) {
        setDeviceBusyId(null);
        deviceBusyRef.current = null;
      }
      await fetchDevice();
    }
  };

  const activeGame = gameStatus?.active_status;
  const gameActions = gameStatus?.active_game_id === "pvz" ? PVZ_ACTIONS : STARDEW_ACTIONS;
  const deviceActionDisabled = (action: string) => {
    if (action === "refresh") return false;
    if (action === "takeover") return !device;
    if (action === "release") return !device || Boolean(deviceBusyId);
    return Boolean(deviceBusyId) || !device?.can_act || device.owner !== "studio";
  };

  const isStandalone =
    typeof window !== "undefined" && window.location.pathname.endsWith("/live");
  const deskHref = isStandalone ? studioPath("/live") : "#live";

  return (
    <div data-live-page="live-interaction" className={styles.rulesRoot}>
      <div className={`page-heading win-heading ${styles.pageHeader}`}>
        <div>
          <h1>排班与规则</h1>
          <p className={styles.pageHeaderSub}>
            精细化配置直播节目单、观众选游戏投票池、定时排班、冷场/加班阈值及礼物弹幕门槛
          </p>
        </div>
        <a id="goto-live-desk-link" href={deskHref} className={styles.deskJumpLink}>
          前往导播台实时控场 →
        </a>
      </div>

      <div className={styles.subTabBar} role="tablist" aria-label="排班与规则分区">
        {SUB_TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={`interaction-tab-${tab.id}`}
            data-interaction-tab={tab.id}
            className={styles.subTabBtn}
            aria-selected={activeTab === tab.id}
            aria-controls={`interaction-panel-${tab.id}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Sub-tab 1: 节目单与投票 */}
      <section
        id="interaction-panel-rundown"
        role="tabpanel"
        aria-labelledby="interaction-tab-rundown"
        hidden={activeTab !== "rundown"}
      >
        <RundownBuilderPanel
          plan={draftRundownPlan}
          presets={rundownPresets}
          gameCatalog={rundownCatalog}
          dirty={rundownDirty}
          saving={savingRundown}
          statusMessage={rundownStateText}
          statusError={rundownStateError}
          onChangePlan={handleChangeRundownPlan}
          onSave={() => void handleSaveRundown()}
          onResetDraft={handleResetRundownDraft}
        />
      </section>

      {/* Sub-tab 2: 排班与加班参数 */}
      <section
        id="interaction-panel-schedules"
        role="tabpanel"
        aria-labelledby="interaction-tab-schedules"
        hidden={activeTab !== "schedules"}
        className={styles.tabPanel}
      >
        <div className={styles.twoColGrid}>
          <article className="win-group-box" id="live-schedules-card">
            <div className="panel-heading">
              <div className="win-group-title" style={{ margin: 0 }}>
                🗓️ 定时开播排班
              </div>
              <span className="badge" id="schedules-count-badge">
                {schedules.length} 条排班
              </span>
            </div>
            <form id="live-schedule-form" onSubmit={(e) => void handleCreateSchedule(e)}>
              <fieldset disabled={scheduleBusy} style={{ border: 0, padding: 0, margin: 0 }}>
                <div className={styles.compactFormGrid}>
                  <label>
                    排班名称
                    <input
                      id="schedule-label-input"
                      name="schedule_label"
                      value={scheduleLabel}
                      onChange={(e) => setScheduleLabel(e.target.value)}
                      required
                    />
                  </label>
                  <label>
                    排班类型
                    <select
                      id="schedule-kind-select"
                      name="schedule_kind"
                      value={scheduleKind}
                      onChange={(e) =>
                        setScheduleKind(e.target.value as "duration" | "daily_window" | "once_window")
                      }
                    >
                      <option value="duration">单次定时长</option>
                      <option value="once_window">单次时段</option>
                      <option value="daily_window">每日时段</option>
                    </select>
                  </label>
                  {scheduleKind === "duration" ? (
                    <label>
                      直播时长（分钟）
                      <input
                        id="schedule-duration-input"
                        name="duration_minutes"
                        type="number"
                        min={1}
                        max={1440}
                        value={scheduleDuration}
                        onChange={(e) => setScheduleDuration(e.target.value)}
                      />
                    </label>
                  ) : scheduleKind === "once_window" ? (
                    <>
                      <label>
                        开始时间 (YYYY-MM-DD HH:MM)
                        <input
                          id="schedule-once-start-input"
                          name="start_local"
                          type="text"
                          placeholder="YYYY-MM-DD HH:MM"
                          value={scheduleOnceStart}
                          onChange={(e) => setScheduleOnceStart(e.target.value)}
                        />
                      </label>
                      <label>
                        结束时间 (YYYY-MM-DD HH:MM)
                        <input
                          id="schedule-once-end-input"
                          name="end_local"
                          type="text"
                          placeholder="YYYY-MM-DD HH:MM"
                          value={scheduleOnceEnd}
                          onChange={(e) => setScheduleOnceEnd(e.target.value)}
                        />
                      </label>
                    </>
                  ) : (
                    <>
                      <label>
                        开始时间 (HH:MM)
                        <input
                          id="schedule-daily-start-input"
                          name="start_time"
                          type="text"
                          placeholder="20:00"
                          value={scheduleStartLocal}
                          onChange={(e) => setScheduleStartLocal(e.target.value)}
                        />
                      </label>
                      <label>
                        结束时间 (HH:MM)
                        <input
                          id="schedule-daily-end-input"
                          name="end_time"
                          type="text"
                          placeholder="22:00"
                          value={scheduleEndLocal}
                          onChange={(e) => setScheduleEndLocal(e.target.value)}
                        />
                      </label>
                    </>
                  )}
                  <label>
                    时区
                    <input
                      id="schedule-timezone-input"
                      name="timezone"
                      value={scheduleTimezone}
                      onChange={(e) => setScheduleTimezone(e.target.value)}
                    />
                  </label>
                  <label>
                    基础时长（分）
                    <input
                      id="schedule-base-input"
                      name="base_duration_minutes"
                      type="number"
                      min={1}
                      max={1440}
                      value={scheduleBaseMinutes}
                      onChange={(e) => setScheduleBaseMinutes(e.target.value)}
                    />
                  </label>
                  <label>
                    加班上限（分）
                    <input
                      id="schedule-max-ot-input"
                      name="max_overtime_minutes"
                      type="number"
                      min={0}
                      max={720}
                      value={scheduleMaxOvertime}
                      onChange={(e) => setScheduleMaxOvertime(e.target.value)}
                    />
                  </label>
                  <label>
                    单次延长（分）
                    <input
                      id="schedule-ext-step-input"
                      name="extension_step_minutes"
                      type="number"
                      min={1}
                      max={120}
                      value={scheduleExtStep}
                      onChange={(e) => setScheduleExtStep(e.target.value)}
                    />
                  </label>
                </div>

                {scheduleKind === "daily_window" && (
                  <div className={styles.quickActions} aria-label="重复星期">
                    {WEEKDAY_CHOICES.map(([day, label]) => {
                      const checked = scheduleWeekdays.includes(day);
                      return (
                        <label
                          key={day}
                          style={{ display: "inline-flex", alignItems: "center", gap: 4, marginTop: 0 }}
                        >
                          <input
                            type="checkbox"
                            data-schedule-weekday={day}
                            checked={checked}
                            onChange={(e) => {
                              setScheduleWeekdays((prev) =>
                                e.target.checked
                                  ? [...new Set([...prev, day])].sort((a, b) => a - b)
                                  : prev.filter((d) => d !== day)
                              );
                            }}
                          />
                          周{label}
                        </label>
                      );
                    })}
                  </div>
                )}

                <label className="check-field" style={{ marginTop: 10 }}>
                  <input
                    id="schedule-auto-pause-input"
                    type="checkbox"
                    name="auto_pause_operations"
                    checked={scheduleAutoPause}
                    onChange={(e) => setScheduleAutoPause(e.target.checked)}
                  />
                  开播时自动暂停后台运维巡检，下播后恢复
                </label>

                <div className="actions" style={{ marginTop: 12 }}>
                  <button
                    type="submit"
                    id="schedule-create-btn"
                    className="primary win-btn"
                    disabled={scheduleBusy}
                  >
                    新增定时开播
                  </button>
                </div>
              </fieldset>
              {(scheduleStatusText || scheduleErrorText) && (
                <p
                  id="schedule-feedback-text"
                  className={scheduleErrorText ? "hint error-text" : "hint"}
                  role="status"
                >
                  {scheduleErrorText || scheduleStatusText}
                </p>
              )}
            </form>

            <ul className={styles.scheduleList} id="live-schedules-list">
              {schedules.map((item) => {
                const isEnabled = item.enabled !== false;
                const isActive = item.status === "active" || Boolean(item.active);
                const startText =
                  item.start_local_display ||
                  item.next_start_local_display ||
                  item.start_local ||
                  item.start_time ||
                  "--:--";
                const endText =
                  item.end_local_display ||
                  item.next_end_local_display ||
                  item.end_local ||
                  item.end_time ||
                  "--:--";
                const windowText =
                  item.kind === "duration"
                    ? `${item.duration_minutes ?? item.base_duration_minutes ?? 60} 分钟`
                    : `${startText} ~ ${endText}`;
                return (
                  <li key={item.id} className={styles.scheduleRow} data-schedule-id={item.id}>
                    <div className={styles.scheduleMeta}>
                      <strong>
                        {item.label || item.kind} · {isEnabled ? "已启用" : "已暂停"}
                        {isActive ? " (开播中)" : ""}
                      </strong>
                      <small>
                        {item.kind} · {windowText} · 加班上限 {item.max_overtime_minutes ?? 120}m
                        {item.auto_pause_operations !== false ? " · 自动暂停运维" : ""}
                      </small>
                    </div>
                    <div className={styles.rowActions}>
                      <button
                        type="button"
                        data-schedule-toggle={item.id}
                        disabled={scheduleBusy}
                        onClick={() => void handleToggleSchedule(item)}
                      >
                        {isEnabled ? "暂停" : "启用"}
                      </button>
                      <button
                        type="button"
                        className="danger"
                        data-schedule-delete={item.id}
                        disabled={scheduleBusy}
                        onClick={() => void handleDeleteSchedule(item.id)}
                      >
                        删除
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
            <p className="hint" hidden={schedules.length > 0}>
              暂无定时开播排班。
            </p>
          </article>

          <article className="win-group-box" id="fine-settings-card">
            <div className="panel-heading">
              <div className="win-group-title" style={{ margin: 0 }}>
                🎛️ 冷场导演、加班能量与小游戏精调
              </div>
              <span className={fineDirty ? styles.dirtyPill : styles.cleanPill}>
                {fineDirty ? "未保存" : "已同步"}
              </span>
            </div>
            <form id="fine-settings-form" onSubmit={(e) => void handleSaveFineSettings(e)}>
              <fieldset disabled={savingFine} style={{ border: 0, padding: 0, margin: 0 }}>
                <fieldset className={styles.matrixGroup}>
                  <legend>一、冷场导演阶梯延迟与冷却（秒）</legend>
                  <div className={styles.compactFormGrid}>
                    <label>
                      Tier 1 暖场延迟
                      <input
                        id="fine-cd-t1-delay"
                        type="number"
                        min={10}
                        max={3600}
                        value={cdTier1Delay}
                        onChange={(e) => {
                          setFineDirty(true);
                          setCdTier1Delay(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      Tier 1 冷却间隔
                      <input
                        id="fine-cd-t1-cooldown"
                        type="number"
                        min={10}
                        max={3600}
                        value={cdTier1Cooldown}
                        onChange={(e) => {
                          setFineDirty(true);
                          setCdTier1Cooldown(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      Tier 2 自发节目延迟
                      <input
                        id="fine-cd-t2-delay"
                        type="number"
                        min={30}
                        max={7200}
                        value={cdTier2Delay}
                        onChange={(e) => {
                          setFineDirty(true);
                          setCdTier2Delay(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      Tier 2 冷却间隔
                      <input
                        id="fine-cd-t2-cooldown"
                        type="number"
                        min={10}
                        max={3600}
                        value={cdTier2Cooldown}
                        onChange={(e) => {
                          setFineDirty(true);
                          setCdTier2Cooldown(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      Tier 3 陪伴电台延迟
                      <input
                        id="fine-cd-t3-delay"
                        type="number"
                        min={60}
                        max={7200}
                        value={cdTier3Delay}
                        onChange={(e) => {
                          setFineDirty(true);
                          setCdTier3Delay(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      Tier 3 电台发言间隔
                      <input
                        id="fine-cd-t3-cadence"
                        type="number"
                        min={120}
                        max={600}
                        value={cdTier3Cadence}
                        onChange={(e) => {
                          setFineDirty(true);
                          setCdTier3Cadence(e.target.value);
                        }}
                      />
                    </label>
                  </div>
                </fieldset>

                <fieldset className={styles.matrixGroup} style={{ marginTop: 12 }}>
                  <legend>二、白饭加班能量条参数</legend>
                  <div className={styles.compactFormGrid}>
                    <label>
                      满条阈值
                      <input
                        id="fine-ot-threshold"
                        type="number"
                        min={10}
                        max={10000}
                        step="1"
                        value={otThreshold}
                        onChange={(e) => {
                          setFineDirty(true);
                          setOtThreshold(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      礼物电池倍率
                      <input
                        id="fine-ot-gift-mult"
                        type="number"
                        min={0.1}
                        max={100}
                        step="0.1"
                        value={otGiftMultiplier}
                        onChange={(e) => {
                          setFineDirty(true);
                          setOtGiftMultiplier(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      弹幕基础充能
                      <input
                        id="fine-ot-chat-energy"
                        type="number"
                        min={0.5}
                        max={50}
                        step="0.5"
                        value={otChatEnergy}
                        onChange={(e) => {
                          setFineDirty(true);
                          setOtChatEnergy(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      单用户充能冷却（秒）
                      <input
                        id="fine-ot-chat-cooldown"
                        type="number"
                        min={0}
                        max={600}
                        step="1"
                        value={otChatCooldown}
                        onChange={(e) => {
                          setFineDirty(true);
                          setOtChatCooldown(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      每分钟衰减
                      <input
                        id="fine-ot-decay"
                        type="number"
                        min={0}
                        max={100}
                        step="0.5"
                        value={otDecayPerMin}
                        onChange={(e) => {
                          setFineDirty(true);
                          setOtDecayPerMin(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      默认基础时长（分）
                      <input
                        id="fine-ot-base"
                        type="number"
                        min={1}
                        max={1440}
                        value={otDefaultBase}
                        onChange={(e) => {
                          setFineDirty(true);
                          setOtDefaultBase(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      默认加班上限（分）
                      <input
                        id="fine-ot-max"
                        type="number"
                        min={0}
                        max={720}
                        value={otDefaultMax}
                        onChange={(e) => {
                          setFineDirty(true);
                          setOtDefaultMax(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      默认延长步长（分）
                      <input
                        id="fine-ot-step"
                        type="number"
                        min={1}
                        max={120}
                        value={otDefaultStep}
                        onChange={(e) => {
                          setFineDirty(true);
                          setOtDefaultStep(e.target.value);
                        }}
                      />
                    </label>
                  </div>
                </fieldset>

                <fieldset className={styles.matrixGroup} style={{ marginTop: 12 }}>
                  <legend>三、你画我猜提示比例 & 海龟汤节奏</legend>
                  <div className={styles.compactFormGrid}>
                    <label>
                      你画我猜 L2 提示比例 (0.1–0.9)
                      <input
                        id="fine-dg-l2-ratio"
                        type="number"
                        min={0.1}
                        max={0.9}
                        step="0.05"
                        value={dgHintL2Ratio}
                        onChange={(e) => {
                          setFineDirty(true);
                          setDgHintL2Ratio(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      你画我猜 L3 提示比例 (0.2–0.95)
                      <input
                        id="fine-dg-l3-ratio"
                        type="number"
                        min={0.2}
                        max={0.95}
                        step="0.05"
                        value={dgHintL3Ratio}
                        onChange={(e) => {
                          setFineDirty(true);
                          setDgHintL3Ratio(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      海龟汤最大提问数
                      <input
                        id="fine-ts-max-q"
                        type="number"
                        min={5}
                        max={100}
                        value={tsMaxQuestions}
                        onChange={(e) => {
                          setFineDirty(true);
                          setTsMaxQuestions(e.target.value);
                        }}
                      />
                    </label>
                    <label>
                      海龟汤自动提示间隔（每 N 问）
                      <input
                        id="fine-ts-auto-hint"
                        type="number"
                        min={0}
                        max={50}
                        value={tsAutoHintInterval}
                        onChange={(e) => {
                          setFineDirty(true);
                          setTsAutoHintInterval(e.target.value);
                        }}
                      />
                    </label>
                  </div>
                </fieldset>

                <div className="actions" style={{ marginTop: 14 }}>
                  <button
                    type="submit"
                    id="fine-settings-save-btn"
                    className="primary win-btn"
                    disabled={savingFine}
                  >
                    保存精细参数
                  </button>
                </div>
              </fieldset>
              <p
                id="fine-settings-state"
                className={fineStateError ? "hint error-text" : "hint"}
                role="status"
              >
                {fineStateText}
              </p>
            </form>
          </article>
        </div>
      </section>

      {/* Sub-tab 3: 礼物与弹幕规则 */}
      <section
        id="interaction-panel-rules"
        role="tabpanel"
        aria-labelledby="interaction-tab-rules"
        hidden={activeTab !== "rules"}
      >
        <article className="win-group-box" id="interactive-rules-card" data-workspace-anchor="">
          <div className="panel-heading">
            <div className="win-group-title" style={{ margin: 0 }}>
              🎁 互动玩法与礼物门槛配置
            </div>
            <span className="hint">自定义点歌与你画我猜的礼物电池与弹幕指令触发门槛</span>
          </div>
          <form
            id="interactive-rules-form"
            style={{ marginTop: "10px" }}
            onSubmit={(e) => void handleSaveRules(e)}
          >
            <fieldset disabled={savingRules} style={{ border: 0, padding: 0, margin: 0 }}>
              <fieldset className={styles.matrixGroup}>
                <legend>一、点歌门槛规则</legend>
                <div className="form-grid">
                  <label>
                    点歌触发方式
                    <select
                      id="rule-song-mode"
                      value={songMode}
                      onChange={(e) => {
                        setRulesDirty(true);
                        setSongMode(e.target.value);
                      }}
                    >
                      <option value="free">免费（弹幕输入“点歌 歌名”即可）</option>
                      <option value="gift_only">仅送礼（必须送出指定礼物/达标电池）</option>
                    </select>
                  </label>
                  <label>
                    最低礼物价值（电池）
                    <input
                      id="rule-song-coin"
                      type="number"
                      min={0}
                      value={songCoin}
                      onChange={(e) => {
                        setRulesDirty(true);
                        setSongCoin(e.target.value);
                      }}
                    />
                  </label>
                  <label>
                    指定礼物名称（逗号分隔，留空则不限）
                    <input
                      id="rule-song-gifts"
                      type="text"
                      placeholder="例如：小心心, 辣条"
                      value={songGifts}
                      onChange={(e) => {
                        setRulesDirty(true);
                        setSongGifts(e.target.value);
                      }}
                    />
                  </label>
                </div>
              </fieldset>

              <fieldset className={styles.matrixGroup} style={{ marginTop: "14px" }}>
                <legend>二、“你画我猜”互动规则</legend>
                <div className="form-grid">
                  <label className="check-field" style={{ gridColumn: "1 / -1" }}>
                    <input
                      id="rule-dg-enabled"
                      type="checkbox"
                      checked={dgEnabled}
                      onChange={(e) => {
                        setRulesDirty(true);
                        setDgEnabled(e.target.checked);
                      }}
                    />{" "}
                    启用直播间“你画我猜”互动游戏
                  </label>
                  <label>
                    启动触发方式
                    <select
                      id="rule-dg-mode"
                      value={dgMode}
                      onChange={(e) => {
                        setRulesDirty(true);
                        setDgMode(e.target.value);
                      }}
                    >
                      <option value="gift_only">仅送礼触发</option>
                      <option value="danmaku_or_gift">弹幕指令（#你画我猜）或送礼</option>
                      <option value="host_only">仅主播/后台手动启动</option>
                    </select>
                  </label>
                  <label>
                    启动最低礼物电池
                    <input
                      id="rule-dg-coin"
                      type="number"
                      min={0}
                      value={dgCoin}
                      onChange={(e) => {
                        setRulesDirty(true);
                        setDgCoin(e.target.value);
                      }}
                    />
                  </label>
                  <label>
                    指定触发礼物名称
                    <input
                      id="rule-dg-gifts"
                      type="text"
                      value={dgGifts}
                      onChange={(e) => {
                        setRulesDirty(true);
                        setDgGifts(e.target.value);
                      }}
                    />
                  </label>
                  <label>
                    每轮竞猜时长（秒）
                    <input
                      id="rule-dg-duration"
                      type="number"
                      min={15}
                      max={300}
                      value={dgDuration}
                      onChange={(e) => {
                        setRulesDirty(true);
                        setDgDuration(e.target.value);
                      }}
                    />
                  </label>
                </div>
              </fieldset>

              <div className="actions" style={{ marginTop: "14px" }}>
                <button type="submit" id="interactive-rules-save-btn" className="primary win-btn">
                  保存互动与礼物规则
                </button>
              </div>
            </fieldset>
            <p
              id="interactive-rules-state"
              className={rulesStateError ? "hint error-text" : "hint"}
              role="status"
            >
              {rulesStateText}
            </p>
          </form>
        </article>
      </section>

      {/* Sub-tab 4: 实机游戏与设备 */}
      <section
        id="interaction-panel-games"
        role="tabpanel"
        aria-labelledby="interaction-tab-games"
        hidden={activeTab !== "games"}
        className={styles.tabPanel}
      >
        <div className={styles.twoColGrid}>
          <article className="win-group-box" id="live-game-panel" data-workspace-anchor="">
            <div className="panel-heading">
              <div className="win-group-title" style={{ margin: 0 }}>
                🎮 游戏联动 · 离线模拟 (星露谷 / 植物大战僵尸)
              </div>
              <span id="live-game-name" className="badge">
                {activeGame?.display_name || (gameStatus ? "未知游戏" : "星露谷物语")}
              </span>
            </div>
            <p id="live-game-summary" className="speech-text" style={{ fontSize: "14px" }}>
              {gameSummaryError || activeGame?.summary || (gameStatus ? "准备中..." : "正在读取模拟状态…")}
            </p>
            <p id="live-game-activity" className="hint">
              {gameStatus ? `当前行动: ${activeGame?.current_activity || "待命"}` : ""}
            </p>
            <div className="actions" style={{ marginTop: "10px" }}>
              <button
                type="button"
                className="win-btn primary"
                data-game-switch="stardew_valley"
                disabled={gameBusy}
                onClick={() => void handleSwitchGame("stardew_valley")}
              >
                切到《星露谷》
              </button>
              <button
                type="button"
                className="win-btn"
                data-game-switch="pvz"
                disabled={gameBusy}
                onClick={() => void handleSwitchGame("pvz")}
              >
                切到《植物大战僵尸》
              </button>
              <button
                type="button"
                className="win-btn"
                id="game-control"
                disabled={!gameStatus || gameBusy}
                onClick={() => void handleTransferControl()}
              >
                {gameStatus?.owner === "operator" ? "交回游戏控制" : "接管游戏"}
              </button>
              <button
                type="button"
                className="win-btn"
                data-game-step=""
                disabled={!gameStatus || gameBusy}
                onClick={() => void handleGameStep()}
              >
                推进一回合
              </button>
            </div>
            <div className="win-subgroup-box" style={{ marginTop: "14px" }}>
              <div className="win-subgroup-title">快捷游戏事件注入</div>
              <div id="live-game-actions" className="actions">
                {gameStatus &&
                  gameActions.map((item) => (
                    <button
                      key={item.action}
                      type="button"
                      className="button quiet small"
                      data-game-action={item.action}
                      disabled={gameBusy}
                      onClick={() => void handleGameAction(item.action)}
                    >
                      {item.label}
                    </button>
                  ))}
              </div>
            </div>
          </article>

          <article className="win-group-box" id="stardew-device-panel">
            <div className="win-group-title">🌾 星露谷物语 · 隔离实机检查器</div>
            <p id="device-state">{deviceStateText}</p>
            <div className="actions">
              {(
                [
                  ["refresh", "刷新状态"],
                  ["takeover", "人工接管"],
                  ["release", "交回工坊 30 秒"],
                  ["turn_right", "向右转身"],
                  ["next_slot", "下一背包格"],
                  ["till_front", "锄面前土地"],
                  ["plant_front", "播种手中种子"],
                  ["water_front", "浇面前土地"],
                ] as const
              ).map(([action, label]) => (
                <button
                  key={action}
                  type="button"
                  className="win-btn"
                  data-device-action={action}
                  disabled={deviceActionDisabled(action)}
                  onClick={() => void handleDeviceAction(action)}
                >
                  {label}
                </button>
              ))}
            </div>
            <p id="device-result" role="status">
              {deviceResultText}
            </p>
          </article>
        </div>
      </section>
    </div>
  );
}
