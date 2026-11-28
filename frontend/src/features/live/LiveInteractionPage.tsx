import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut, apiRequest, studioPath } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./LiveWorkbench.module.css";
import type {
  ColdDirectorStatusData,
  DrawGuessStatusData,
  GameStatusData,
  InteractiveRulesData,
  LiveShowsStatusData,
  RoastRadioStatusData,
  StardewActionReceipt,
  StardewDeviceState,
  StardewDeviceStatus,
  TurtleSoupStatusData,
} from "./types";

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

export interface LiveInteractionPageProps {
  visible?: boolean;
  onFeedback?: (message: string, error?: boolean) => void;
}

export function LiveInteractionPage({
  visible = true,
  onFeedback,
}: LiveInteractionPageProps) {
  const [gameStatus, setGameStatus] = useState<GameStatusData | null>(null);
  const [gameSummaryError, setGameSummaryError] = useState("");
  const [gameBusy, setGameBusy] = useState(false);

  const [device, setDevice] = useState<StardewDeviceState | null>(null);
  const [deviceStateText, setDeviceStateText] = useState("正在读取游戏状态…");
  const [deviceResultText, setDeviceResultText] = useState("");
  const [deviceBusyId, setDeviceBusyId] = useState<string | null>(null);

  const [drawGuessStatus, setDrawGuessStatus] = useState<DrawGuessStatusData | null>(null);
  const [drawGuessError, setDrawGuessError] = useState("");
  const [drawGuessBusy, setDrawGuessBusy] = useState(false);
  const [dgTheme, setDgTheme] = useState("");
  const [dgUseLlm, setDgUseLlm] = useState(true);
  const [dgRoundMode, setDgRoundMode] = useState("aqua_draws");

  const [turtleSoupStatus, setTurtleSoupStatus] = useState<TurtleSoupStatusData | null>(null);
  const [tsGenre, setTsGenre] = useState("");
  const [tsUseLlm, setTsUseLlm] = useState(true);
  const [tsBusy, setTsBusy] = useState(false);

  const [roastRadioStatus, setRoastRadioStatus] = useState<RoastRadioStatusData | null>(null);
  const [rrTopic, setRrTopic] = useState("");
  const [rrSideA, setRrSideA] = useState("正方");
  const [rrSideB, setRrSideB] = useState("反方");
  const [rrBusy, setRrBusy] = useState(false);

  const [coldDirectorStatus, setColdDirectorStatus] = useState<ColdDirectorStatusData | null>(null);
  const [coldBusy, setColdBusy] = useState(false);

  const [rules, setRules] = useState<InteractiveRulesData | null>(null);
  const [rulesDirty, setRulesDirty] = useState(false);
  const [savingRules, setSavingRules] = useState(false);
  const [rulesStateText, setRulesStateText] = useState("");
  const [rulesStateError, setRulesStateError] = useState(false);

  // Rules form state (preserving numeric 0 accurately!)
  const [songMode, setSongMode] = useState("free");
  const [songCoin, setSongCoin] = useState("0");
  const [songGifts, setSongGifts] = useState("");
  const [dgEnabled, setDgEnabled] = useState(true);
  const [dgMode, setDgMode] = useState("gift_only");
  const [dgCoin, setDgCoin] = useState("10");
  const [dgGifts, setDgGifts] = useState("画笔, 小心心, 辣条");
  const [dgDuration, setDgDuration] = useState("60");

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

  useUnsavedGuard("live-interaction-rules", rulesDirty);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
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
    setDgDuration(String(dg?.round_duration_sec || 60));
  }, []);

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

  const fetchDrawGuessStatus = useCallback(async () => {
    try {
      const data = await apiGet<"/api/v1/live/draw-and-guess/status", DrawGuessStatusData>(
        "/api/v1/live/draw-and-guess/status"
      );
      if (!mountedRef.current) return;
      setDrawGuessStatus(data);
      setDrawGuessError("");
    } catch (error) {
      if (!mountedRef.current) return;
      setDrawGuessError(`读取失败：${errorMessage(error)}`);
    }
  }, []);

  const fetchShowsStatus = useCallback(async () => {
    try {
      const [showsRes, coldRes] = await Promise.allSettled([
        apiRequest<LiveShowsStatusData>("/api/v1/live/shows/status", { method: "GET" }),
        apiRequest<ColdDirectorStatusData>("/api/v1/live/cold-director/status", { method: "GET" }),
      ]);
      if (!mountedRef.current) return;
      if (showsRes.status === "fulfilled" && showsRes.value && typeof showsRes.value === "object") {
        const shows = showsRes.value;
        if (shows.turtle_soup && typeof shows.turtle_soup === "object") {
          setTurtleSoupStatus(shows.turtle_soup);
        }
        if (shows.roast_radio && typeof shows.roast_radio === "object") {
          setRoastRadioStatus(shows.roast_radio);
        }
        if (shows.cold_director && typeof shows.cold_director === "object") {
          setColdDirectorStatus(shows.cold_director);
        }
        if (
          shows.draw_and_guess &&
          typeof shows.draw_and_guess === "object" &&
          ("active" in shows.draw_and_guess || "state" in shows.draw_and_guess)
        ) {
          setDrawGuessStatus(shows.draw_and_guess);
        }
      }
      if (
        coldRes.status === "fulfilled" &&
        coldRes.value &&
        typeof coldRes.value === "object" &&
        "tier" in coldRes.value
      ) {
        setColdDirectorStatus(coldRes.value);
      }
    } catch {
      // Optional Phase 2 endpoints fail silently if backend isn't mounted yet
    }
  }, []);

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
      fetchGameStatus(),
      fetchDevice(),
      fetchDrawGuessStatus(),
      fetchShowsStatus(),
      fetchRules(),
    ]);
    return () => {
      if (deviceTimerRef.current) {
        clearTimeout(deviceTimerRef.current);
        deviceTimerRef.current = null;
      }
    };
  }, [visible, fetchGameStatus, fetchDevice, fetchDrawGuessStatus, fetchShowsStatus, fetchRules]);

  const handleSwitchGame = async (gameId: string) => {
    if (gameBusy) return;
    setGameBusy(true);
    try {
      const data = await apiPost<
        "/api/v1/live/games/switch",
        { spoken_text?: string }
      >("/api/v1/live/games/switch", {
        body: { game_id: gameId },
      });
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
      const data = await apiPost<
        "/api/v1/live/games/action",
        { message?: string }
      >("/api/v1/live/games/action", {
        body: { action },
      });
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
      let receipt = await apiPost<
        "/api/v1/live/games/device/stardew/actions",
        StardewActionReceipt
      >("/api/v1/live/games/device/stardew/actions", {
        body: command as never,
      });
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

  const handleStartDrawGuess = async () => {
    if (drawGuessBusy) return;
    setDrawGuessBusy(true);
    try {
      const body: Record<string, unknown> = {
        trigger_user: "主播",
        duration_sec: rules?.draw_and_guess?.round_duration_sec ?? 60,
        use_llm: dgUseLlm,
        mode: dgRoundMode,
      };
      if (dgTheme.trim()) {
        body.theme = dgTheme.trim();
      }
      const data = await apiPost<
        "/api/v1/live/draw-and-guess/start",
        { spoken_text?: string }
      >("/api/v1/live/draw-and-guess/start", {
        body: body as never,
      });
      onFeedback?.(data.spoken_text || "已开启你画我猜！");
      await Promise.all([fetchDrawGuessStatus(), fetchShowsStatus()]);
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setDrawGuessBusy(false);
    }
  };

  const handleStopDrawGuess = async () => {
    if (drawGuessBusy) return;
    setDrawGuessBusy(true);
    try {
      const data = await apiPost<
        "/api/v1/live/draw-and-guess/stop",
        { word?: string }
      >("/api/v1/live/draw-and-guess/stop");
      onFeedback?.(`已结束本轮你画我猜，答案是：【${data.word ?? ""}】`);
      await Promise.all([fetchDrawGuessStatus(), fetchShowsStatus()]);
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setDrawGuessBusy(false);
    }
  };

  const handleStartTurtleSoup = async () => {
    if (tsBusy) return;
    setTsBusy(true);
    try {
      const body: Record<string, unknown> = {
        trigger_user: "主播",
        use_llm: tsUseLlm,
      };
      if (tsGenre.trim()) body.genre = tsGenre.trim();
      const data = await apiRequest<{ spoken_text?: string; surface?: string }>(
        "/api/v1/live/shows/turtle-soup/start",
        { method: "POST", body }
      );
      onFeedback?.(data.spoken_text || "海龟汤已开汤！");
      await fetchShowsStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setTsBusy(false);
    }
  };

  const handleTurtleSoupHint = async () => {
    if (tsBusy) return;
    setTsBusy(true);
    try {
      const data = await apiRequest<{ spoken_text?: string; hint?: string }>(
        "/api/v1/live/shows/turtle-soup/hint",
        { method: "POST" }
      );
      onFeedback?.(data.spoken_text || data.hint || "已给出一条海龟汤提示");
      await fetchShowsStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setTsBusy(false);
    }
  };

  const handleTurtleSoupReveal = async () => {
    if (tsBusy) return;
    setTsBusy(true);
    try {
      const data = await apiRequest<{ spoken_text?: string; answer?: string }>(
        "/api/v1/live/shows/turtle-soup/reveal",
        { method: "POST" }
      );
      onFeedback?.(data.spoken_text || (data.answer ? `汤底揭晓：${data.answer}` : "海龟汤已揭晓"));
      await fetchShowsStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setTsBusy(false);
    }
  };

  const handleStartDebate = async () => {
    if (rrBusy) return;
    const topic = rrTopic.trim() || "今晚宵夜吃白饭还是泡面";
    setRrBusy(true);
    try {
      const data = await apiRequest<{ spoken_text?: string }>(
        "/api/v1/live/shows/roast-radio/start-debate",
        {
          method: "POST",
          body: {
            topic,
            side_a: rrSideA.trim() || "正方",
            side_b: rrSideB.trim() || "反方",
          },
        }
      );
      onFeedback?.(data.spoken_text || `已开启辩论：${topic}`);
      await fetchShowsStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setRrBusy(false);
    }
  };

  const handleTriggerRoast = async () => {
    if (rrBusy) return;
    setRrBusy(true);
    try {
      const body: Record<string, unknown> = {};
      if (rrTopic.trim()) body.topic = rrTopic.trim();
      const data = await apiRequest<{ spoken_text?: string }>(
        "/api/v1/live/shows/roast-radio/roast",
        { method: "POST", body }
      );
      onFeedback?.(data.spoken_text || "已触发一段犀利锐评");
      await fetchShowsStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setRrBusy(false);
    }
  };

  const handleStopRoastRadio = async () => {
    if (rrBusy) return;
    setRrBusy(true);
    try {
      await apiRequest("/api/v1/live/shows/roast-radio/stop", { method: "POST" });
      onFeedback?.("已结束犀利电台 / 辩论台");
      await fetchShowsStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setRrBusy(false);
    }
  };

  const handleTriggerColdDirector = async () => {
    if (coldBusy) return;
    setColdBusy(true);
    try {
      const data = await apiRequest<{ spoken_text?: string; kind?: string }>(
        "/api/v1/live/cold-director/trigger",
        { method: "POST" }
      );
      onFeedback?.(data.spoken_text || "已触发冷场导演暖场片段");
      await fetchShowsStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setColdBusy(false);
    }
  };

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

  const activeGame = gameStatus?.active_status;
  const gameActions =
    gameStatus?.active_game_id === "pvz" ? PVZ_ACTIONS : STARDEW_ACTIONS;

  const dgActive = Boolean(drawGuessStatus?.active);
  const dgBadgeText = drawGuessError
    ? drawGuessError
    : !drawGuessStatus
      ? "空闲中"
      : dgActive
        ? `作画中 (${Math.ceil(drawGuessStatus.remaining ?? 0)}s)`
        : drawGuessStatus.state === "finished"
          ? "上一局结束"
          : "空闲中";

  const dgWordText = !drawGuessStatus
    ? "等待送礼触发或手动开启"
    : dgActive
      ? `分类: ${drawGuessStatus.category || ""} | 提示: ${drawGuessStatus.hint || "逐步揭晓中"}`
      : drawGuessStatus.word
        ? `上一题: 【${drawGuessStatus.word}】 ${drawGuessStatus.winner ? `胜者: ${drawGuessStatus.winner}` : "(无人猜出)"}`
        : "等待送礼触发或手动开启";

  const deviceActionDisabled = (action: string) => {
    if (action === "refresh") return false;
    if (action === "takeover") return !device;
    if (action === "release") return !device || Boolean(deviceBusyId);
    return Boolean(deviceBusyId) || !device?.can_act || device.owner !== "studio";
  };

  return (
    <div data-live-page="live-interaction">
      <div className="page-heading win-heading">
        <h1>直播小游戏与礼物互动规则</h1>
      </div>

      <div className="live-grid win-split-pane">
        <article className="win-group-box" id="live-game-panel" data-workspace-anchor="">
          <div className="panel-heading">
            <div className="win-group-title" style={{ margin: 0 }}>
              🎮 游戏联动 · 离线模拟
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

        <article className="win-group-box">
          <div className="win-group-title">星露谷物语 · 隔离实机</div>
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

        <article className="win-group-box" id="live-draw-guess-panel">
          <div className="panel-heading">
            <div className="win-group-title" style={{ margin: 0 }}>
              🎨 你画我猜 2.0 互动玩法
            </div>
            <span id="dg-live-status" className={dgActive ? "badge amber" : "badge"}>
              {dgBadgeText}
            </span>
          </div>
          <p id="dg-live-word" className="speech-text" style={{ fontSize: "14px" }}>
            {dgWordText}
          </p>
          {(drawGuessStatus?.progressive_hint || drawGuessStatus?.hint_level) && (
            <p id="dg-progressive-hint" className="hint">
              {drawGuessStatus.hint_level ? `提示 L${drawGuessStatus.hint_level}: ` : "渐进提示: "}
              {drawGuessStatus.progressive_hint || drawGuessStatus.hint || ""}
            </p>
          )}
          {drawGuessStatus?.scoreboard && Object.keys(drawGuessStatus.scoreboard).length > 0 && (
            <p id="dg-live-scoreboard" className="hint">
              积分榜：
              {Object.entries(drawGuessStatus.scoreboard)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 5)
                .map(([name, score]) => `${name}(${score})`)
                .join(" · ")}
            </p>
          )}
          <div className={styles.compactFormGrid}>
            <label>
              主题词（可选）
              <input
                id="dg-theme-input"
                type="text"
                placeholder="例如：美食、二次元、科技"
                value={dgTheme}
                onChange={(e) => setDgTheme(e.target.value)}
              />
            </label>
            <label>
              玩法模式
              <select
                id="dg-mode-select"
                value={dgRoundMode}
                onChange={(e) => setDgRoundMode(e.target.value)}
              >
                <option value="aqua_draws">Aqua 作画观众猜</option>
                <option value="viewer_draws">指定主题出题</option>
              </select>
            </label>
            <label className="check-field" style={{ alignSelf: "end" }}>
              <input
                id="dg-use-llm-toggle"
                type="checkbox"
                checked={dgUseLlm}
                onChange={(e) => setDgUseLlm(e.target.checked)}
              />{" "}
              启用 LLM 现画现猜
            </label>
          </div>
          <div className="actions" style={{ marginTop: "10px" }}>
            <button
              type="button"
              id="dg-start-btn"
              className="primary win-btn"
              disabled={dgActive || !rules || drawGuessBusy}
              onClick={() => void handleStartDrawGuess()}
            >
              开启一轮竞猜
            </button>
            <button
              type="button"
              id="dg-stop-btn"
              className="win-btn danger"
              disabled={!dgActive || drawGuessBusy}
              onClick={() => void handleStopDrawGuess()}
            >
              提前结束
            </button>
            <a
              href={studioPath("/overlay/draw-guess")}
              target="_blank"
              rel="noopener noreferrer"
              className="win-link-btn"
            >
              打开 OBS 透明画板 ↗
            </a>
          </div>
          <p className="hint" style={{ marginTop: "12px" }}>
            观众送出指定礼物（如画笔/小心心）或弹幕抢答即可自动参与；猜中后由当前角色接话，结果记入每日趣事。
          </p>
        </article>
      </div>

      <div className={styles.phase2Grid}>
        <article className="win-group-box" id="live-turtle-soup-panel">
          <div className="panel-heading">
            <div className="win-group-title" style={{ margin: 0 }}>
              🐢 海龟汤推理剧场
            </div>
            <span
              id="ts-live-status"
              className={turtleSoupStatus?.active ? "badge amber" : "badge"}
            >
              {turtleSoupStatus?.active
                ? "推理进行中"
                : turtleSoupStatus?.state === "finished"
                  ? "已揭晓"
                  : "空闲中"}
            </span>
          </div>
          <div className={styles.showSubBox}>
            <p id="ts-panel-surface">
              {turtleSoupStatus?.surface
                ? `汤面：${turtleSoupStatus.surface}`
                : "点击开汤或让弹幕输入 #海龟汤 开启一轮推理。"}
            </p>
            {(turtleSoupStatus?.clues_found?.length ?? 0) > 0 && (
              <p id="ts-panel-clues">
                已解锁线索：{turtleSoupStatus!.clues_found!.join(" / ")}
              </p>
            )}
            {turtleSoupStatus?.answer && turtleSoupStatus.state === "finished" && (
              <p id="ts-panel-answer">汤底：{turtleSoupStatus.answer}</p>
            )}
          </div>
          <div className={styles.compactFormGrid}>
            <label>
              题材偏好（可选）
              <input
                id="ts-genre-input"
                type="text"
                placeholder="悬疑 / 科幻 / 日常"
                value={tsGenre}
                onChange={(e) => setTsGenre(e.target.value)}
              />
            </label>
            <label className="check-field" style={{ alignSelf: "end" }}>
              <input
                id="ts-use-llm-toggle"
                type="checkbox"
                checked={tsUseLlm}
                onChange={(e) => setTsUseLlm(e.target.checked)}
              />{" "}
              启用 LLM 现编与判题
            </label>
          </div>
          <div className="actions" style={{ marginTop: "10px" }}>
            <button
              type="button"
              id="ts-start-btn"
              className="primary win-btn"
              disabled={Boolean(turtleSoupStatus?.active) || tsBusy}
              onClick={() => void handleStartTurtleSoup()}
            >
              开汤出题
            </button>
            <button
              type="button"
              id="ts-hint-btn"
              className="win-btn"
              disabled={!turtleSoupStatus?.active || tsBusy}
              onClick={() => void handleTurtleSoupHint()}
            >
              给一条提示
            </button>
            <button
              type="button"
              id="ts-reveal-btn"
              className="win-btn danger"
              disabled={!turtleSoupStatus?.active || tsBusy}
              onClick={() => void handleTurtleSoupReveal()}
            >
              揭晓汤底并结束
            </button>
          </div>
        </article>

        <article className="win-group-box" id="live-roast-radio-panel">
          <div className="panel-heading">
            <div className="win-group-title" style={{ margin: 0 }}>
              🎙️ 犀利锐评电台 & 辩论台
            </div>
            <span
              id="rr-live-status"
              className={roastRadioStatus?.active ? "badge amber" : "badge"}
            >
              {roastRadioStatus?.active
                ? roastRadioStatus.mode === "debate"
                  ? "辩论站队中"
                  : "锐评中"
                : "空闲中"}
            </span>
          </div>
          <div className={styles.showSubBox}>
            <p id="rr-panel-topic">
              {roastRadioStatus?.topic
                ? `当前话题：${roastRadioStatus.topic}`
                : "仅读取公开看过的视频与公开记忆进行安全锐评，或开启正反方辩论。"}
            </p>
            {(roastRadioStatus?.mode === "debate" || roastRadioStatus?.side_a || roastRadioStatus?.side_b) && (
              <div>
                <div className={styles.overtimeMeterRow}>
                  <span>
                    {roastRadioStatus?.side_a || "正方"} ({roastRadioStatus?.votes_a ?? 0})
                  </span>
                  <span>
                    {roastRadioStatus?.side_b || "反方"} ({roastRadioStatus?.votes_b ?? 0})
                  </span>
                </div>
                <div className={styles.voteMiniBar}>
                  <div
                    className={styles.voteMiniA}
                    style={{
                      width: `${
                        (roastRadioStatus?.votes_a ?? 0) + (roastRadioStatus?.votes_b ?? 0) > 0
                          ? Math.round(
                              ((roastRadioStatus?.votes_a ?? 0) /
                                ((roastRadioStatus?.votes_a ?? 0) +
                                  (roastRadioStatus?.votes_b ?? 0))) *
                                100
                            )
                          : 50
                      }%`,
                    }}
                  />
                  <div
                    className={styles.voteMiniB}
                    style={{
                      width: `${
                        (roastRadioStatus?.votes_a ?? 0) + (roastRadioStatus?.votes_b ?? 0) > 0
                          ? 100 -
                            Math.round(
                              ((roastRadioStatus?.votes_a ?? 0) /
                                ((roastRadioStatus?.votes_a ?? 0) +
                                  (roastRadioStatus?.votes_b ?? 0))) *
                                100
                            )
                          : 50
                      }%`,
                    }}
                  />
                </div>
              </div>
            )}
            {roastRadioStatus?.last_roast && (
              <p id="rr-panel-roast">最新锐评：{roastRadioStatus.last_roast}</p>
            )}
          </div>
          <div className={styles.compactFormGrid}>
            <label style={{ gridColumn: "1 / -1" }}>
              辩论 / 锐评话题
              <input
                id="rr-topic-input"
                type="text"
                placeholder="例如：熬夜写代码该配白饭还是咖啡"
                value={rrTopic}
                onChange={(e) => setRrTopic(e.target.value)}
              />
            </label>
            <label>
              正方标签
              <input
                id="rr-side-a-input"
                type="text"
                value={rrSideA}
                onChange={(e) => setRrSideA(e.target.value)}
              />
            </label>
            <label>
              反方标签
              <input
                id="rr-side-b-input"
                type="text"
                value={rrSideB}
                onChange={(e) => setRrSideB(e.target.value)}
              />
            </label>
          </div>
          <div className="actions" style={{ marginTop: "10px" }}>
            <button
              type="button"
              id="rr-start-debate-btn"
              className="primary win-btn"
              disabled={rrBusy}
              onClick={() => void handleStartDebate()}
            >
              开启辩论台
            </button>
            <button
              type="button"
              id="rr-roast-btn"
              className="win-btn"
              disabled={rrBusy}
              onClick={() => void handleTriggerRoast()}
            >
              触发一次锐评
            </button>
            <button
              type="button"
              id="rr-stop-btn"
              className="win-btn danger"
              disabled={!roastRadioStatus?.active || rrBusy}
              onClick={() => void handleStopRoastRadio()}
            >
              结束电台
            </button>
          </div>
        </article>

        <article className="win-group-box" id="live-cold-director-panel">
          <div className="panel-heading">
            <div className="win-group-title" style={{ margin: 0 }}>
              🌙 冷场导演状态（60 分钟防冷场）
            </div>
            <span id="cold-director-tier" className="badge">
              Tier {coldDirectorStatus?.tier ?? 0}
            </span>
          </div>
          <div className={styles.showSubBox}>
            <p id="cold-director-idle">
              当前冷场时长：{Math.round(coldDirectorStatus?.elapsed_seconds ?? coldDirectorStatus?.idle_seconds ?? 0)}s
              {coldDirectorStatus?.tier_label ? ` (${coldDirectorStatus.tier_label})` : ""}
              {coldDirectorStatus?.last_segment_kind
                ? ` · 最近片段：${coldDirectorStatus.last_segment_kind}`
                : ""}
            </p>
            {coldDirectorStatus?.last_spoken_text && (
              <p id="cold-director-spoken">最近台词：{coldDirectorStatus.last_spoken_text}</p>
            )}
          </div>
          <div className="actions" style={{ marginTop: "10px" }}>
            <button
              type="button"
              id="cold-trigger-btn"
              className="primary win-btn"
              disabled={coldBusy}
              onClick={() => void handleTriggerColdDirector()}
            >
              手动触发冷场暖场
            </button>
            <button
              type="button"
              id="cold-refresh-btn"
              className="win-btn"
              disabled={coldBusy}
              onClick={() => void fetchShowsStatus()}
            >
              刷新演播状态
            </button>
          </div>
          <p className="hint" style={{ marginTop: "10px" }}>
            Tier 0 活跃 · Tier 1 暖场闲聊 (90s+) · Tier 2 自发你画我猜/锐评/海龟汤 (5m+) · Tier 3 深夜伴播电台 (15m+)。
          </p>
        </article>
      </div>

      <article
        className="win-group-box"
        id="interactive-rules-card"
        data-workspace-anchor=""
        style={{ marginTop: "16px" }}
      >
        <div className="panel-heading">
          <div className="win-group-title" style={{ margin: 0 }}>
            🎁 互动玩法与礼物门槛配置
          </div>
          <span className="hint">自定义点歌、你画我猜与游戏直播的触发门槛</span>
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
              <button type="submit" className="primary win-btn">
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
    </div>
  );
}
