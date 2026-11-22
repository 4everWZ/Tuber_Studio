import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, apiPut, studioPath } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import styles from "./LiveWorkbench.module.css";
import type {
  DrawGuessStatusData,
  GameStatusData,
  InteractiveRulesData,
  StardewActionReceipt,
  StardewDeviceState,
  StardewDeviceStatus,
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
      fetchRules(),
    ]);
    return () => {
      if (deviceTimerRef.current) {
        clearTimeout(deviceTimerRef.current);
        deviceTimerRef.current = null;
      }
    };
  }, [visible, fetchGameStatus, fetchDevice, fetchDrawGuessStatus, fetchRules]);

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
      const data = await apiPost<
        "/api/v1/live/draw-and-guess/start",
        { spoken_text?: string }
      >("/api/v1/live/draw-and-guess/start", {
        body: {
          trigger_user: "主播",
          duration_sec: rules?.draw_and_guess?.round_duration_sec ?? 60,
        },
      });
      onFeedback?.(data.spoken_text || "已开启你画我猜！");
      await fetchDrawGuessStatus();
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
      await fetchDrawGuessStatus();
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setDrawGuessBusy(false);
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
              🎨 你画我猜互动玩法
            </div>
            <span id="dg-live-status" className={dgActive ? "badge amber" : "badge"}>
              {dgBadgeText}
            </span>
          </div>
          <p id="dg-live-word" className="speech-text" style={{ fontSize: "14px" }}>
            {dgWordText}
          </p>
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
