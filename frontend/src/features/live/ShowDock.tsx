import { useEffect, useRef, useState } from "react";
import type {
  ExtendedLiveShowsStatusData,
  LivePollState,
  RundownStatusResponse,
} from "./consoleTypes";
import { formatElapsed } from "./liveControls";
import styles from "./LiveWorkbench.module.css";
import type { GameStatusData } from "./types";

function clampIntInput(raw: string, fallback: number, min: number, max: number): number {
  const n = Number.parseInt(raw, 10);
  if (Number.isNaN(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

export type DockTabId =
  | "rundown"
  | "draw_guess"
  | "turtle_soup"
  | "roast_radio"
  | "cold_director"
  | "games";

const SEGMENT_KIND_LABELS: Record<string, string> = {
  chat: "闲聊",
  draw_and_guess: "你画我猜 2.0",
  turtle_soup: "灵魂海龟汤",
  roast_radio: "锐评辩论",
  song_party: "点歌听歌会",
  stardew_valley: "星露谷物语",
  pvz: "植物大战僵尸",
};

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

export interface ShowDockProps {
  activeTab: DockTabId;
  onTabChange: (tab: DockTabId) => void;
  shows: ExtendedLiveShowsStatusData | null;
  poll: LivePollState | null;
  rundownStatus: RundownStatusResponse | null;
  gameStatus: GameStatusData | null;
  busy: boolean;
  canAct: boolean;
  onPostAction: (path: string, body?: Record<string, unknown>, successMsg?: string) => Promise<void>;
}

export function ShowDock({
  activeTab,
  onTabChange,
  shows,
  poll,
  rundownStatus,
  gameStatus,
  busy,
  canAct,
  onPostAction,
}: ShowDockProps) {
  const [dgTheme, setDgTheme] = useState("");
  const [dgMode, setDgMode] = useState("aqua_draws");
  const [dgUseLlm, setDgUseLlm] = useState(true);
  const [dgRevealSecret, setDgRevealSecret] = useState(false);

  const [tsGenre, setTsGenre] = useState("");
  const [tsUseLlm, setTsUseLlm] = useState(true);
  const [tsRevealSpoiler, setTsRevealSpoiler] = useState(false);

  const [rrTopic, setRrTopic] = useState("");
  const [rrSideA, setRrSideA] = useState("正方");
  const [rrSideB, setRrSideB] = useState("反方");

  const rundownState = rundownStatus?.state ?? shows?.rundown ?? null;
  const rundownPlan = rundownStatus?.plan ?? null;

  const [extendMinutes, setExtendMinutes] = useState("15");
  const extendEditedRef = useRef(false);
  const [voteDurationSec, setVoteDurationSec] = useState("90");
  const voteEditedRef = useRef(false);
  const [targetRemainingMin, setTargetRemainingMin] = useState("15");

  const planExtendMins = rundownPlan?.chat_entry?.extend_chat_minutes;
  const planPollSec = rundownPlan?.chat_entry?.poll_duration_seconds;

  useEffect(() => {
    if (!extendEditedRef.current && typeof planExtendMins === "number") {
      setExtendMinutes(String(planExtendMins));
    }
  }, [planExtendMins]);

  useEffect(() => {
    if (!voteEditedRef.current && typeof planPollSec === "number") {
      setVoteDurationSec(String(planPollSec));
    }
  }, [planPollSec]);

  const parsedExtendMin = clampIntInput(extendMinutes, planExtendMins ?? 15, 1, 360);
  const parsedVoteSec = clampIntInput(voteDurationSec, planPollSec ?? 90, 10, 600);
  const parsedTargetRemMin = clampIntInput(targetRemainingMin, 15, 1, 360);

  const drawGuess = shows?.draw_and_guess ?? null;
  const turtleSoup = shows?.turtle_soup ?? null;
  const roastRadio = shows?.roast_radio ?? null;
  const coldDirector = shows?.cold_director ?? null;

  const tabs: Array<{ id: DockTabId; label: string; live: boolean }> = [
    {
      id: "rundown",
      label: "节目单与投票",
      live: Boolean(rundownState?.active || poll?.phase === "started"),
    },
    { id: "draw_guess", label: "你画我猜 2.0", live: Boolean(drawGuess?.active) },
    { id: "turtle_soup", label: "灵魂海龟汤", live: Boolean(turtleSoup?.active) },
    { id: "roast_radio", label: "锐评辩论", live: Boolean(roastRadio?.active) },
    { id: "cold_director", label: "冷场导演", live: Boolean((coldDirector?.tier ?? 0) >= 1) },
    { id: "games", label: "实机游戏", live: Boolean(gameStatus?.active_game_id) },
  ];

  const actionDisabled = busy || !canAct;
  const gameActions = gameStatus?.active_game_id === "pvz" ? PVZ_ACTIONS : STARDEW_ACTIONS;
  const votesA = Math.max(0, roastRadio?.votes_a ?? 0);
  const votesB = Math.max(0, roastRadio?.votes_b ?? 0);
  const totalVotes = votesA + votesB;
  const pctA = totalVotes > 0 ? Math.round((votesA / totalVotes) * 100) : 50;

  return (
    <section className={`panel ${styles.deskPanel} ${styles.cockpitPanel}`} aria-label="节目控制台">
      <div className={styles.dockTabs} role="tablist" aria-label="节目子控制台">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={`dock-tab-${tab.id}`}
            data-dock-tab={tab.id}
            aria-selected={activeTab === tab.id}
            className={styles.dockTabBtn}
            onClick={() => onTabChange(tab.id)}
          >
            {tab.live && <span className={styles.liveDot} aria-hidden="true" />}
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "rundown" && (
        <div
          className={`${styles.dockBody} ${styles.dockBodyScroll}`}
          role="tabpanel"
          aria-labelledby="dock-tab-rundown"
        >
          <div className="actions">
            {!rundownState?.active ? (
              <button
                type="button"
                className={`primary ${styles.compactBtn}`}
                data-rundown-start
                disabled={actionDisabled}
                onClick={() => void onPostAction("/api/v1/live/rundown/start", {}, "节目单已启动。")}
              >
                启动节目单
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className={styles.compactBtn}
                  data-rundown-pause-resume
                  disabled={actionDisabled}
                  onClick={() =>
                    void onPostAction(
                      rundownState.paused
                        ? "/api/v1/live/rundown/resume"
                        : "/api/v1/live/rundown/pause",
                      {},
                      rundownState.paused ? "节目单已继续。" : "节目单已暂停。"
                    )
                  }
                >
                  {rundownState.paused ? "继续节目单" : "暂停节目单"}
                </button>
                <button
                  type="button"
                  className={styles.compactBtn}
                  data-rundown-stop
                  disabled={actionDisabled}
                  onClick={() => void onPostAction("/api/v1/live/rundown/stop", {}, "节目单已停止。")}
                >
                  停止节目单
                </button>
              </>
            )}
            <div className={styles.inlineInputGroup}>
              <input
                type="number"
                className={styles.inlineNumInput}
                data-rundown-vote-duration
                aria-label="投票秒数"
                title="自定义投票时长（秒）"
                min={10}
                max={600}
                value={voteDurationSec}
                onChange={(e) => {
                  voteEditedRef.current = true;
                  setVoteDurationSec(e.target.value);
                }}
              />
              <span className={styles.deskHint}>s</span>
              <button
                type="button"
                className={styles.compactBtn}
                data-rundown-vote-now
                disabled={actionDisabled}
                onClick={() =>
                  void onPostAction(
                    "/api/v1/live/rundown/vote-now",
                    { duration_seconds: parsedVoteSec },
                    `已发起 ${parsedVoteSec} 秒选游戏投票。`
                  )
                }
              >
                发起选游戏投票
              </button>
            </div>
            <button
              type="button"
              className={styles.compactBtn}
              data-rundown-settle-vote
              disabled={actionDisabled || poll?.phase !== "started"}
              onClick={() =>
                void onPostAction("/api/v1/live/rundown/settle-vote", {}, "已结算当前选游戏投票。")
              }
            >
              立即结算投票
            </button>
            <div className={styles.inlineInputGroup}>
              <input
                type="number"
                className={styles.inlineNumInput}
                data-rundown-extend-minutes
                aria-label="延长闲聊分钟数"
                title="自定义再聊分钟数"
                min={1}
                max={360}
                value={extendMinutes}
                onChange={(e) => {
                  extendEditedRef.current = true;
                  setExtendMinutes(e.target.value);
                }}
              />
              <span className={styles.deskHint}>m</span>
              <button
                type="button"
                className={styles.compactBtn}
                data-rundown-extend-chat
                disabled={actionDisabled}
                onClick={() =>
                  void onPostAction(
                    "/api/v1/live/rundown/extend-chat",
                    { minutes: parsedExtendMin },
                    `已延长 ${parsedExtendMin} 分钟闲聊时间。`
                  )
                }
              >
                再聊 {parsedExtendMin}m
              </button>
            </div>
          </div>

          <div className={styles.timeAdjustBar} data-rundown-time-controls>
            <span className={styles.deskChip}>
              ⏱ 剩余：
              {rundownState?.active
                ? rundownState.paused
                  ? `${formatElapsed(rundownState.remaining_seconds)}（暂停）`
                  : formatElapsed(rundownState.remaining_seconds)
                : "未启动"}
            </span>
            <button
              type="button"
              className={styles.compactBtn}
              data-rundown-delta="-5"
              disabled={actionDisabled || !rundownState?.active}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/rundown/adjust-time",
                  { delta_minutes: -5 },
                  "当前环节已减少 5 分钟。"
                )
              }
            >
              -5m
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-rundown-delta="5"
              disabled={actionDisabled || !rundownState?.active}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/rundown/adjust-time",
                  { delta_minutes: 5 },
                  "当前环节已延长 5 分钟。"
                )
              }
            >
              +5m
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-rundown-delta="15"
              disabled={actionDisabled || !rundownState?.active}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/rundown/adjust-time",
                  { delta_minutes: 15 },
                  "当前环节已延长 15 分钟。"
                )
              }
            >
              +15m
            </button>
            <div className={styles.inlineInputGroup}>
              <input
                type="number"
                className={styles.inlineNumInput}
                data-rundown-set-remaining-input
                aria-label="设定当前环节剩余分钟数"
                min={1}
                max={360}
                value={targetRemainingMin}
                onChange={(e) => setTargetRemainingMin(e.target.value)}
              />
              <span className={styles.deskHint}>m</span>
              <button
                type="button"
                className={styles.compactBtn}
                data-rundown-set-remaining-btn
                disabled={actionDisabled || !rundownState?.active}
                onClick={() =>
                  void onPostAction(
                    "/api/v1/live/rundown/adjust-time",
                    { set_remaining_minutes: parsedTargetRemMin },
                    `已将当前环节剩余时间设为 ${parsedTargetRemMin} 分钟。`
                  )
                }
              >
                设剩余
              </button>
            </div>
          </div>

          {poll?.phase === "started" && (
            <div className={styles.showSubBox} data-dock-active-poll>
              <strong>
                🗳️ {poll.topic} · 剩余 {Math.max(0, Math.ceil(poll.remaining_seconds ?? 0))}s · 共{" "}
                {poll.total_votes ?? 0} 票
              </strong>
              <p className={styles.deskHint}>
                {(poll.options || [])
                  .map((opt, idx) => `${idx + 1}.${opt} (${poll.counts?.[opt] ?? 0})`)
                  .join(" ｜ ")}
              </p>
            </div>
          )}

          {rundownPlan?.enabled_games && rundownPlan.enabled_games.length > 0 && (
            <div className={styles.badgeWrap} aria-label="可投票游戏池">
              <span className={styles.deskHint}>已启用游戏池：</span>
              {rundownPlan.enabled_games.map((kind) => {
                const catalogItem = rundownStatus?.game_catalog?.find((g) => g.kind === kind);
                return (
                  <span key={kind} className={styles.deskChip}>
                    {catalogItem?.label || SEGMENT_KIND_LABELS[kind] || kind}
                  </span>
                );
              })}
            </div>
          )}

          <ol className={styles.segmentList} data-dock-segment-list>
            {(rundownPlan?.segments ?? []).map((seg, idx) => {
              const isCurrent =
                Boolean(rundownState?.active) &&
                (rundownState?.current_segment?.id === seg.id || rundownState?.current_index === idx);
              return (
                <li
                  key={seg.id}
                  className={`${styles.segmentRow} ${isCurrent ? styles.segmentRowActive : ""}`}
                  data-segment-id={seg.id}
                >
                  <div>
                    <strong>
                      {idx + 1}. {seg.title}
                    </strong>{" "}
                    <span className={styles.deskHint}>
                      {SEGMENT_KIND_LABELS[seg.kind] || seg.kind} · {seg.duration_minutes}m ·{" "}
                      {seg.obs_layout === "game" ? "🎮 game" : "💬 chat"}
                    </span>
                  </div>
                  <button
                    type="button"
                    className={styles.compactBtn}
                    data-rundown-jump={seg.id}
                    disabled={actionDisabled}
                    onClick={() =>
                      void onPostAction(
                        "/api/v1/live/rundown/jump",
                        { index: idx, segment_id: seg.id },
                        `已跳转到环节：${seg.title}`
                      )
                    }
                  >
                    跳到此段
                  </button>
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {activeTab === "draw_guess" && (
        <div className={styles.dockBody} role="tabpanel" aria-labelledby="dock-tab-draw_guess">
          <div className={styles.compactFormGrid}>
            <label>
              主题词（可选）
              <input
                type="text"
                placeholder="如：二次元、美食"
                value={dgTheme}
                onChange={(e) => setDgTheme(e.target.value)}
              />
            </label>
            <label>
              模式
              <select value={dgMode} onChange={(e) => setDgMode(e.target.value)}>
                <option value="aqua_draws">Aqua 作画观众猜</option>
                <option value="viewer_draws">指定主题出题</option>
              </select>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6, alignSelf: "end" }}>
              <input
                type="checkbox"
                checked={dgUseLlm}
                onChange={(e) => setDgUseLlm(e.target.checked)}
              />
              LLM 现画现猜
            </label>
          </div>
          <div className="actions">
            <button
              type="button"
              className={`primary ${styles.compactBtn}`}
              data-dock-dg-start
              disabled={actionDisabled || Boolean(drawGuess?.active)}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/draw-and-guess/start",
                  {
                    trigger_user: "主播",
                    duration_sec: 60,
                    use_llm: dgUseLlm,
                    mode: dgMode,
                    ...(dgTheme.trim() ? { theme: dgTheme.trim() } : {}),
                  },
                  "已开启你画我猜 2.0！"
                )
              }
            >
              开始一局
            </button>
            <button
              type="button"
              className={`danger ${styles.compactBtn}`}
              data-dock-dg-stop
              disabled={actionDisabled || !drawGuess?.active}
              onClick={() =>
                void onPostAction("/api/v1/live/draw-and-guess/stop", {}, "已结束本轮你画我猜。")
              }
            >
              停止本局
            </button>
          </div>
          <div className={styles.spoilerBox}>
            <span>
              <strong>主播私密词底：</strong>
              {dgRevealSecret ? drawGuess?.word || "当前无题目" : "••••••（防走光已遮挡）"}
            </span>
            <button
              type="button"
              className={styles.compactBtn}
              data-dg-toggle-secret
              onClick={() => setDgRevealSecret((prev) => !prev)}
            >
              {dgRevealSecret ? "隐藏词底" : "偷看词底"}
            </button>
          </div>
          <div className={styles.showSubBox}>
            <p>
              <strong>状态：</strong>
              {drawGuess?.active
                ? `进行中 (${Math.ceil(drawGuess.remaining ?? 0)}s) · 提示 L${drawGuess.hint_level ?? 1}: ${drawGuess.progressive_hint || drawGuess.hint || ""}`
                : "空闲"}
            </p>
            {drawGuess?.scoreboard && Object.keys(drawGuess.scoreboard).length > 0 && (
              <p>
                <strong>积分榜：</strong>
                {Object.entries(drawGuess.scoreboard)
                  .sort((a, b) => b[1] - a[1])
                  .slice(0, 5)
                  .map(([name, score]) => `${name}(${score})`)
                  .join(" · ")}
              </p>
            )}
          </div>
        </div>
      )}

      {activeTab === "turtle_soup" && (
        <div className={styles.dockBody} role="tabpanel" aria-labelledby="dock-tab-turtle_soup">
          <div className={styles.compactFormGrid}>
            <label>
              题材偏好
              <input
                type="text"
                placeholder="如：科幻、日常推理"
                value={tsGenre}
                onChange={(e) => setTsGenre(e.target.value)}
              />
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6, alignSelf: "end" }}>
              <input
                type="checkbox"
                checked={tsUseLlm}
                onChange={(e) => setTsUseLlm(e.target.checked)}
              />
              LLM 出题与裁判
            </label>
          </div>
          <div className="actions">
            <button
              type="button"
              className={`primary ${styles.compactBtn}`}
              data-dock-ts-start
              disabled={actionDisabled || Boolean(turtleSoup?.active)}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/shows/turtle-soup/start",
                  {
                    trigger_user: "主播",
                    use_llm: tsUseLlm,
                    ...(tsGenre.trim() ? { genre: tsGenre.trim() } : {}),
                  },
                  "灵魂海龟汤已开汤！"
                )
              }
            >
              开始海龟汤
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-dock-ts-hint
              disabled={actionDisabled || !turtleSoup?.active}
              onClick={() =>
                void onPostAction("/api/v1/live/shows/turtle-soup/hint", {}, "已给出海龟汤提示。")
              }
            >
              提示
            </button>
            <button
              type="button"
              className={`danger ${styles.compactBtn}`}
              data-dock-ts-reveal
              disabled={actionDisabled || !turtleSoup?.active}
              onClick={() =>
                void onPostAction("/api/v1/live/shows/turtle-soup/reveal", {}, "已揭晓海龟汤汤底。")
              }
            >
              揭晓汤底
            </button>
          </div>
          <div className={styles.showSubBox}>
            <p>
              <strong>汤面：</strong>
              {turtleSoup?.surface || "当前未开汤"}
            </p>
            <p>
              <strong>已解锁线索：</strong>
              {turtleSoup?.clues_found?.length ? turtleSoup.clues_found.join(" · ") : "暂无"}
            </p>
          </div>
          <div className={styles.spoilerBox}>
            <span>
              <strong>主播汤底参考：</strong>
              {tsRevealSpoiler ? turtleSoup?.answer || "开汤后或揭晓时可用" : "••••••（防剧透）"}
            </span>
            <button
              type="button"
              className={styles.compactBtn}
              data-ts-toggle-spoiler
              onClick={() => setTsRevealSpoiler((prev) => !prev)}
            >
              {tsRevealSpoiler ? "隐藏汤底" : "查看汤底"}
            </button>
          </div>
        </div>
      )}

      {activeTab === "roast_radio" && (
        <div className={styles.dockBody} role="tabpanel" aria-labelledby="dock-tab-roast_radio">
          <div className={styles.compactFormGrid}>
            <label>
              辩论 / 锐评话题
              <input
                type="text"
                placeholder="今晚宵夜吃白饭还是泡面"
                value={rrTopic}
                onChange={(e) => setRrTopic(e.target.value)}
              />
            </label>
            <label>
              正方 (1)
              <input type="text" value={rrSideA} onChange={(e) => setRrSideA(e.target.value)} />
            </label>
            <label>
              反方 (2)
              <input type="text" value={rrSideB} onChange={(e) => setRrSideB(e.target.value)} />
            </label>
          </div>
          <div className="actions">
            <button
              type="button"
              className={`primary ${styles.compactBtn}`}
              data-dock-rr-debate
              disabled={actionDisabled}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/shows/roast-radio/start-debate",
                  {
                    topic: rrTopic.trim() || "今晚宵夜吃白饭还是泡面",
                    side_a: rrSideA.trim() || "正方",
                    side_b: rrSideB.trim() || "反方",
                  },
                  "锐评辩论已开启！"
                )
              }
            >
              开启辩论
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-dock-rr-roast
              disabled={actionDisabled}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/shows/roast-radio/roast",
                  rrTopic.trim() ? { topic: rrTopic.trim() } : {},
                  "已生成即兴公开锐评。"
                )
              }
            >
              即兴公开锐评
            </button>
            <button
              type="button"
              className={`danger ${styles.compactBtn}`}
              data-dock-rr-stop
              disabled={actionDisabled || !roastRadio?.active}
              onClick={() =>
                void onPostAction("/api/v1/live/shows/roast-radio/stop", {}, "已结束锐评辩论。")
              }
            >
              停止
            </button>
          </div>
          <div className={styles.showSubBox}>
            <p>
              <strong>{roastRadio?.topic || "暂无进行中的辩论"}</strong>
            </p>
            <div className={styles.overtimeMeterRow}>
              <span>
                {roastRadio?.side_a || rrSideA}：{votesA} 票
              </span>
              <span>
                {roastRadio?.side_b || rrSideB}：{votesB} 票
              </span>
            </div>
            <div className={styles.voteMiniBar}>
              <div className={styles.voteMiniA} style={{ width: `${pctA}%` }} />
              <div className={styles.voteMiniB} style={{ width: `${100 - pctA}%` }} />
            </div>
            {roastRadio?.last_roast && <p className={styles.deskHint}>“{roastRadio.last_roast}”</p>}
          </div>
        </div>
      )}

      {activeTab === "cold_director" && (
        <div className={styles.dockBody} role="tabpanel" aria-labelledby="dock-tab-cold_director">
          <div className={styles.showSubBox}>
            <p>
              <strong>当前档位：</strong>Tier {coldDirector?.tier ?? 0} ·{" "}
              {coldDirector?.tier_label || "活跃互动"}
            </p>
            <p>
              <strong>空闲时长：</strong>
              {formatElapsed(coldDirector?.elapsed_seconds ?? coldDirector?.idle_seconds)} ·{" "}
              <strong>距离下次动作：</strong>
              {coldDirector?.next_action_in_seconds != null
                ? `${Math.ceil(coldDirector.next_action_in_seconds)}s`
                : "待命中"}
            </p>
            {coldDirector?.last_spoken_text && (
              <p className={styles.deskHint}>最近暖场台词：{coldDirector.last_spoken_text}</p>
            )}
          </div>
          <div className="actions">
            <button
              type="button"
              className={`primary ${styles.compactBtn}`}
              id="desk-cold-trigger-btn"
              data-dock-cold-trigger
              disabled={actionDisabled}
              onClick={() =>
                void onPostAction("/api/v1/live/cold-director/trigger", {}, "已触发冷场导演暖场节目。")
              }
            >
              立即触发下一档冷场节目
            </button>
          </div>
        </div>
      )}

      {activeTab === "games" && (
        <div className={styles.dockBody} role="tabpanel" aria-labelledby="dock-tab-games">
          <div className={styles.showSubBox}>
            <p>
              <strong>当前游戏：</strong>
              {gameStatus?.active_status?.display_name || gameStatus?.active_game_id || "星露谷物语"} ·{" "}
              <strong>控制权：</strong>
              {gameStatus?.owner === "operator" ? "主播手动" : "AI 自动托管"}
            </p>
            <p className={styles.deskHint}>
              {gameStatus?.active_status?.summary || "支持一键切换游戏与模拟推进。"}
            </p>
          </div>
          <div className="actions">
            <button
              type="button"
              className={styles.compactBtn}
              data-dock-game-switch="stardew_valley"
              disabled={actionDisabled}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/games/switch",
                  { game_id: "stardew_valley" },
                  "已切换到星露谷物语。"
                )
              }
            >
              星露谷物语
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-dock-game-switch="pvz"
              disabled={actionDisabled}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/games/switch",
                  { game_id: "pvz" },
                  "已切换到植物大战僵尸。"
                )
              }
            >
              植物大战僵尸
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-dock-game-control
              disabled={actionDisabled || !gameStatus}
              onClick={() =>
                void onPostAction(
                  "/api/v1/live/games/control",
                  { owner: gameStatus?.owner === "operator" ? "studio" : "operator" },
                  "已切换游戏控制权。"
                )
              }
            >
              {gameStatus?.owner === "operator" ? "切为 AI 托管" : "切为手动接管"}
            </button>
            <button
              type="button"
              className={styles.compactBtn}
              data-dock-game-step
              disabled={actionDisabled || !gameStatus}
              onClick={() => void onPostAction("/api/v1/live/games/step", {}, "已推进一步游戏。")}
            >
              推进一回合
            </button>
          </div>
          <div className={styles.quickActions}>
            {gameActions.map((item) => (
              <button
                key={item.action}
                type="button"
                className={styles.compactBtn}
                data-dock-game-action={item.action}
                disabled={actionDisabled}
                onClick={() =>
                  void onPostAction("/api/v1/live/games/action", { action: item.action })
                }
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
