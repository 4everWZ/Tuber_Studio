import styles from "./DrawGuessOverlay.module.css";
import type { ShowStageLayerProps } from "./consoleTypes";
import type { DrawGuessStroke, LiveShowsStatusData } from "./types";

const VERDICT_LABELS: Record<string, string> = {
  yes: "是",
  no: "不是",
  irrelevant: "无关",
  close: "很接近",
  solved: "猜中啦",
};

const SEGMENT_KIND_LABELS: Record<string, string> = {
  chat: "闲聊互动",
  draw_and_guess: "你画我猜 2.0",
  turtle_soup: "灵魂海龟汤",
  roast_radio: "锐评辩论",
  song_party: "点歌听歌会",
  stardew_valley: "星露谷物语",
  pvz: "植物大战僵尸",
};

function safeColor(value: unknown, fallback?: string): string | undefined {
  if (typeof value !== "string") return fallback;
  const trimmed = value.trim();
  if (trimmed === "none" || trimmed === "transparent") return trimmed;
  if (/^#[0-9a-fA-F]{3,8}$/.test(trimmed)) return trimmed;
  return fallback;
}

function safeNumber(value: unknown, min: number, max: number, fallback: number): number {
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return fallback;
  return Math.max(min, Math.min(max, num));
}

function safePathD(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed || !/^[MmLlHhVvCcSsQqTtAaZz0-9.,\s+-]+$/.test(trimmed)) return null;
  return trimmed;
}

function safePoints(points: DrawGuessStroke["points"]): string | null {
  if (Array.isArray(points)) {
    const pairs: string[] = [];
    for (const pt of points) {
      if (Array.isArray(pt) && pt.length >= 2) {
        const x = safeNumber(pt[0], 0, 400, NaN);
        const y = safeNumber(pt[1], 0, 300, NaN);
        if (Number.isFinite(x) && Number.isFinite(y)) {
          pairs.push(`${x},${y}`);
        }
      }
    }
    return pairs.length >= 3 ? pairs.join(" ") : null;
  }
  if (typeof points !== "string") return null;
  const nums = points
    .trim()
    .split(/[\s,]+/)
    .map((part) => Number(part))
    .filter((n) => Number.isFinite(n));
  if (nums.length < 6) return null;
  const pairs: string[] = [];
  for (let i = 0; i + 1 < nums.length; i += 2) {
    pairs.push(`${safeNumber(nums[i], 0, 400, 0)},${safeNumber(nums[i + 1], 0, 300, 0)}`);
  }
  return pairs.length >= 3 ? pairs.join(" ") : null;
}

export function renderStroke(stroke: DrawGuessStroke, index: number) {
  const strokeColor = safeColor(stroke.stroke);
  const strokeWidthVal = stroke.stroke_width ?? stroke.strokeWidth ?? stroke.width;

  if (stroke.type === "path") {
    const d = safePathD(stroke.d);
    if (!d) return null;
    return (
      <path
        key={index}
        d={d}
        fill={safeColor(stroke.fill, "none")}
        stroke={strokeColor || "#38bdf8"}
        strokeWidth={safeNumber(strokeWidthVal, 0.5, 40, 4)}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    );
  }
  if (stroke.type === "circle") {
    return (
      <circle
        key={index}
        cx={safeNumber(stroke.cx, 0, 400, 0)}
        cy={safeNumber(stroke.cy, 0, 300, 0)}
        r={safeNumber(stroke.r, 0, 300, 1)}
        fill={safeColor(stroke.fill, "#38bdf8")}
        stroke={strokeColor}
        strokeWidth={strokeColor ? safeNumber(strokeWidthVal, 0.5, 40, 2) : undefined}
      />
    );
  }
  if (stroke.type === "ellipse") {
    return (
      <ellipse
        key={index}
        cx={safeNumber(stroke.cx, 0, 400, 0)}
        cy={safeNumber(stroke.cy, 0, 300, 0)}
        rx={safeNumber(stroke.rx, 0, 400, 1)}
        ry={safeNumber(stroke.ry, 0, 300, 1)}
        fill={safeColor(stroke.fill, "#38bdf8")}
        stroke={strokeColor}
        strokeWidth={strokeColor ? safeNumber(strokeWidthVal, 0.5, 40, 2) : undefined}
      />
    );
  }
  if (stroke.type === "rect") {
    const rectStrokeWidth = stroke.stroke_width ?? stroke.strokeWidth;
    return (
      <rect
        key={index}
        x={safeNumber(stroke.x, 0, 400, 0)}
        y={safeNumber(stroke.y, 0, 300, 0)}
        width={safeNumber(stroke.width, 0, 400, 0)}
        height={safeNumber(stroke.height, 0, 300, 0)}
        rx={stroke.rx != null ? safeNumber(stroke.rx, 0, 200, 0) : undefined}
        fill={safeColor(stroke.fill, "#38bdf8")}
        stroke={strokeColor}
        strokeWidth={strokeColor ? safeNumber(rectStrokeWidth, 0.5, 40, 2) : undefined}
      />
    );
  }
  if (stroke.type === "line") {
    return (
      <line
        key={index}
        x1={safeNumber(stroke.x1, 0, 400, 0)}
        y1={safeNumber(stroke.y1, 0, 300, 0)}
        x2={safeNumber(stroke.x2, 0, 400, 0)}
        y2={safeNumber(stroke.y2, 0, 300, 0)}
        stroke={strokeColor || "#38bdf8"}
        strokeWidth={safeNumber(strokeWidthVal, 0.5, 40, 4)}
        strokeLinecap="round"
      />
    );
  }
  if (stroke.type === "polygon") {
    const points = safePoints(stroke.points);
    if (!points) return null;
    return (
      <polygon
        key={index}
        points={points}
        fill={safeColor(stroke.fill, "#38bdf8")}
        stroke={strokeColor}
        strokeWidth={strokeColor ? safeNumber(strokeWidthVal, 0.5, 40, 2) : undefined}
        strokeLinejoin="round"
      />
    );
  }
  return null;
}

function resolveCharacterName(character: LiveShowsStatusData["character"]): string | undefined {
  if (!character) return undefined;
  if (typeof character === "string") return character;
  return character.name;
}

export function ShowStageLayer({
  shows,
  drawGuess,
  drawGuessVisible,
  turtleSoup,
  roastRadio,
  poll,
  rundown,
  character,
  previewStandby = false,
  compact = false,
  className,
}: ShowStageLayerProps) {
  const charName = resolveCharacterName(character ?? shows?.character);
  const rawDg = drawGuess !== undefined ? drawGuess : shows?.draw_and_guess ?? null;
  const overlayState = rawDg
    ? {
        ...rawDg,
        character: rawDg.character ?? charName,
      }
    : null;
  const activeTurtleSoup = turtleSoup !== undefined ? turtleSoup : shows?.turtle_soup ?? null;
  const activeRoastRadio = roastRadio !== undefined ? roastRadio : shows?.roast_radio ?? null;
  const activePoll = poll !== undefined ? poll : shows?.poll ?? null;
  const activeRundown = rundown !== undefined ? rundown : shows?.rundown ?? null;

  const isDgDefaultVisible = Boolean(
    overlayState && (overlayState.active || (overlayState.state === "finished" && overlayState.word))
  );
  const visible = drawGuessVisible !== undefined ? drawGuessVisible : isDgDefaultVisible;

  const finished = overlayState?.state === "finished";
  const strokes = overlayState?.revealed_strokes ?? [];
  const clueValue = overlayState?.progressive_hint || overlayState?.hint || "";
  const clueLabel = clueValue
    ? overlayState?.hint_level
      ? `提示 L${overlayState.hint_level}: ${clueValue}`
      : `提示: ${clueValue}`
    : "";

  const scoreEntries = Object.entries(overlayState?.scoreboard ?? {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4);

  const showTurtleSoup = Boolean(
    activeTurtleSoup?.active || (activeTurtleSoup?.state === "finished" && activeTurtleSoup?.answer)
  );
  const showRoastRadio = Boolean(activeRoastRadio?.active);
  const showPoll = Boolean(activePoll && activePoll.phase === "started");

  const votesA = Math.max(0, activeRoastRadio?.votes_a ?? 0);
  const votesB = Math.max(0, activeRoastRadio?.votes_b ?? 0);
  const totalRoastVotes = votesA + votesB;
  const pctA = totalRoastVotes > 0 ? Math.round((votesA / totalRoastVotes) * 100) : 50;
  const pctB = 100 - pctA;

  const pollOptions = activePoll?.options ?? [];
  const pollCounts = activePoll?.counts ?? {};
  const computedPollTotal = pollOptions.reduce(
    (sum, opt) => sum + Math.max(0, pollCounts[opt] ?? 0),
    0
  );
  const pollTotalVotes =
    typeof activePoll?.total_votes === "number"
      ? Math.max(activePoll.total_votes, computedPollTotal)
      : computedPollTotal;

  const hasActiveContent = visible || showTurtleSoup || showRoastRadio || showPoll;
  const stackClass = [
    compact ? styles.stageLayerCompact : styles.overlayStack,
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  const rundownSegmentLabel = activeRundown?.active
    ? activeRundown.current_segment?.title ||
      (activeRundown.current_segment?.kind
        ? SEGMENT_KIND_LABELS[activeRundown.current_segment.kind] || activeRundown.current_segment.kind
        : "节目单运行中")
    : "当前空闲（暂无进行中的互动节目）";

  return (
    <div className={stackClass}>
      <div id="draw-guess-container" className={styles.container} hidden={!visible}>
        <div className={styles.header}>
          <div className={styles.titleRow}>
            <span>🎨</span>
            <h3 id="draw-guess-title">
              {overlayState?.character ? `${overlayState.character} · 你画我猜` : "你画我猜"}
            </h3>
            <span id="category-badge" className={styles.badge}>
              分类: {overlayState?.category || "趣味猜猜看"}
            </span>
            {(overlayState?.theme || overlayState?.llm_generated) && (
              <span id="theme-llm-badge" className={styles.llmBadge}>
                {overlayState?.theme ? `主题: ${overlayState.theme}` : "AI现画"}
                {overlayState?.theme && overlayState?.llm_generated ? " · AI" : ""}
              </span>
            )}
          </div>
          <div id="timer-badge" className={styles.timerBadge}>
            {overlayState?.remaining != null ? `${Math.ceil(overlayState.remaining)}s` : "60s"}
          </div>
        </div>
        <div className={styles.canvasWrap}>
          <svg
            id="drawing-board"
            className={`drawing-board ${styles.drawingBoard}`}
            viewBox="0 0 400 300"
          >
            {strokes.map((stroke, idx) => renderStroke(stroke, idx))}
          </svg>
          <div
            id="winner-banner"
            className={
              finished
                ? `winner-banner ${styles.winnerBanner}`
                : `winner-banner hidden ${styles.winnerBanner} ${styles.winnerBannerHidden}`
            }
          >
            <h4 id="banner-title">{overlayState?.winner ? "🎉 抢答成功！" : "⏰ 时间到！"}</h4>
            <p id="banner-winner">
              {overlayState?.winner ? (
                <>
                  恭喜 <strong>{overlayState.winner}</strong> 猜对！
                </>
              ) : (
                "没有人猜出来哦~"
              )}
            </p>
            <div id="banner-answer" className={styles.answerTag}>
              {overlayState?.winner
                ? `答案：【${overlayState?.word ?? ""}】`
                : `答案其实是：【${overlayState?.word ?? ""}】`}
            </div>
          </div>
        </div>
        <div className={styles.hintBar}>
          <span>
            发起：<span id="trigger-user">{overlayState?.trigger_user || "观众"}</span>
          </span>
          <span id="clue-text" className={styles.clue}>
            {clueLabel}
          </span>
        </div>
        {scoreEntries.length > 0 && (
          <div id="dg-scoreboard" className={styles.scoreboard}>
            <span>积分榜：</span>
            {scoreEntries.map(([name, score]) => (
              <span key={name} className={styles.scoreChip}>
                {name} {score}分
              </span>
            ))}
          </div>
        )}
      </div>

      {showPoll && activePoll && (
        <section
          id="live-poll-card"
          className={`${styles.showCard} ${styles.showCardPoll}`}
          aria-label="直播间实时选游戏投票"
        >
          <div className={styles.header}>
            <div className={styles.titleRow}>
              <span>🗳️</span>
              <h3 className={`${styles.showTitle} ${styles.showTitlePoll}`}>弹幕投票选下一轮</h3>
              <span className={styles.badge}>共 {pollTotalVotes} 票</span>
            </div>
            <span id="live-poll-countdown" className={styles.timerBadge}>
              {activePoll.remaining_seconds != null
                ? `${Math.max(0, Math.ceil(activePoll.remaining_seconds))}s`
                : `${activePoll.duration ?? 90}s`}
            </span>
          </div>
          <p id="live-poll-topic" className={styles.surfaceBox}>
            {activePoll.topic}
          </p>
          <ul id="live-poll-options" className={styles.pollOptions}>
            {pollOptions.map((option, idx) => {
              const count = Math.max(0, pollCounts[option] ?? 0);
              const pct = pollTotalVotes > 0 ? Math.round((count / pollTotalVotes) * 100) : 0;
              return (
                <li key={`${option}-${idx}`} className={styles.pollOptionRow}>
                  <div className={styles.pollOptionTop}>
                    <span className={styles.pollOptionLabel}>
                      {idx + 1}. {option}
                    </span>
                    <span className={styles.pollOptionMeta}>
                      {count} 票 · {pct}%
                    </span>
                  </div>
                  <div className={styles.pollTrack}>
                    <div className={styles.pollFill} style={{ width: `${pct}%` }} />
                  </div>
                </li>
              );
            })}
          </ul>
          <p id="live-poll-fallback" className={styles.pollFallbackNote}>
            无人投票将按默认节目单推进
            {activePoll.fallback_label ? `（默认：${activePoll.fallback_label}）` : ""}
          </p>
        </section>
      )}

      {showTurtleSoup && activeTurtleSoup && (
        <section id="turtle-soup-card" className={styles.showCard} aria-label="海龟汤互动卡片">
          <div className={styles.header}>
            <div className={styles.titleRow}>
              <span>🐢</span>
              <h3 className={styles.showTitle}>海龟汤推理</h3>
              <span className={styles.badge}>{activeTurtleSoup.genre || "悬疑故事"}</span>
            </div>
            <span className={styles.badge}>
              {activeTurtleSoup.state === "finished" ? "已揭晓" : "弹幕提问中"}
            </span>
          </div>
          <p id="ts-surface" className={styles.surfaceBox}>
            {activeTurtleSoup.surface || "正在出题…"}
          </p>
          {(activeTurtleSoup.clues_found?.length ?? 0) > 0 && (
            <div id="ts-clues" className={styles.cluesList}>
              {activeTurtleSoup.clues_found!.map((clue, idx) => (
                <span key={`${clue}-${idx}`} className={styles.cluePill}>
                  🔍 {clue}
                </span>
              ))}
            </div>
          )}
          {(activeTurtleSoup.qa_history?.length ?? 0) > 0 && (
            <ul id="ts-qa-list" className={styles.qaList}>
              {activeTurtleSoup.qa_history!.slice(-5).map((qa, idx) => (
                <li key={idx} className={styles.qaRow}>
                  <span>
                    <strong>{qa.display_name || qa.user || "观众"}</strong>：
                    {qa.question || qa.text || ""}
                  </span>
                  <span className={styles.verdictTag}>
                    {(qa.verdict && VERDICT_LABELS[qa.verdict]) || qa.verdict || "已回应"}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {activeTurtleSoup.state === "finished" && activeTurtleSoup.answer && (
            <div id="ts-answer-box" className={styles.roastQuote}>
              {activeTurtleSoup.winner ? `🎉 ${activeTurtleSoup.winner} 还原真相！` : "📖 汤底揭晓："}
              {activeTurtleSoup.answer}
            </div>
          )}
        </section>
      )}

      {showRoastRadio && activeRoastRadio && (
        <section
          id="roast-radio-card"
          className={`${styles.showCard} ${styles.showCardRoast}`}
          aria-label="犀利电台与辩论卡片"
        >
          <div className={styles.header}>
            <div className={styles.titleRow}>
              <span>🎙️</span>
              <h3 className={`${styles.showTitle} ${styles.showTitleRoast}`}>
                {activeRoastRadio.mode === "debate" ? "锐评辩论台" : "犀利锐评电台"}
              </h3>
              {activeRoastRadio.source_bvid && (
                <span className={styles.badge}>{activeRoastRadio.source_bvid}</span>
              )}
            </div>
            <span className={styles.badge}>
              {activeRoastRadio.mode === "debate" ? "弹幕站队中" : "锐评中"}
            </span>
          </div>
          <p id="rr-topic" className={styles.surfaceBox}>
            {activeRoastRadio.topic || "今日公开槽点话题"}
          </p>
          {(activeRoastRadio.mode === "debate" || activeRoastRadio.side_a || activeRoastRadio.side_b) && (
            <div id="rr-vote-bar" className={styles.debateWrap}>
              <div className={styles.debateLabels}>
                <span className={styles.debateSideA}>
                  1 · {activeRoastRadio.side_a || "正方"} ({votesA}票 · {pctA}%)
                </span>
                <span className={styles.debateSideB}>
                  2 · {activeRoastRadio.side_b || "反方"} ({votesB}票 · {pctB}%)
                </span>
              </div>
              <div className={styles.debateBar}>
                <div className={styles.debateFillA} style={{ width: `${pctA}%` }} />
                <div className={styles.debateFillB} style={{ width: `${pctB}%` }} />
              </div>
            </div>
          )}
          {activeRoastRadio.last_roast && (
            <p id="rr-last-roast" className={styles.roastQuote}>
              “{activeRoastRadio.last_roast}”
            </p>
          )}
        </section>
      )}

      {previewStandby && !hasActiveContent && (
        <section id="show-standby-placeholder" className={styles.standbyCard} aria-label="节目互动层待机预览">
          <div className={styles.header}>
            <div className={styles.titleRow}>
              <span>🎭</span>
              <h3 className={styles.showTitle}>节目互动层待机预览</h3>
            </div>
            <span className={styles.badge}>OBS 透明层待机中</span>
          </div>
          <p className={styles.standbyDesc}>
            OBS 正常采集时在无节目状态下会自动保持全透明隐藏；当前仅在预览模式下显示此占位框。
          </p>
          <div className={styles.standbyStatusBox}>
            <span>当前节目单状态：{rundownSegmentLabel}</span>
            <span>触发「选游戏投票 / 你画我猜 / 海龟汤 / 锐评辩论」后卡片将在此处实时浮现。</span>
          </div>
        </section>
      )}
    </div>
  );
}
