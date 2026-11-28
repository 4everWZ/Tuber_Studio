import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiRequest } from "@/shared/api/client";
import styles from "./DrawGuessOverlay.module.css";
import type {
  DrawGuessStatusData,
  DrawGuessStroke,
  LiveShowsStatusData,
  RoastRadioStatusData,
  TurtleSoupStatusData,
} from "./types";

const VERDICT_LABELS: Record<string, string> = {
  yes: "是",
  no: "不是",
  irrelevant: "无关",
  close: "很接近",
  solved: "猜中啦",
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

function renderStroke(stroke: DrawGuessStroke, index: number) {
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

export function DrawGuessOverlayApp() {
  const [overlayState, setOverlayState] = useState<DrawGuessStatusData | null>(null);
  const [visible, setVisible] = useState(false);
  const [turtleSoup, setTurtleSoup] = useState<TurtleSoupStatusData | null>(null);
  const [roastRadio, setRoastRadio] = useState<RoastRadioStatusData | null>(null);

  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmountedRef = useRef(false);

  const applyDrawGuess = useCallback((data: DrawGuessStatusData | null | undefined) => {
    if (data && (data.active || (data.state === "finished" && data.word))) {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
      setVisible(true);
      setOverlayState(data);
    } else if (visibleRef.current && !hideTimerRef.current) {
      hideTimerRef.current = setTimeout(() => {
        hideTimerRef.current = null;
        if (!unmountedRef.current) {
          setVisible(false);
          setOverlayState(null);
        }
      }, 4000);
    }
  }, []);

  const poll = useCallback(async () => {
    try {
      const shows = await apiRequest<LiveShowsStatusData | DrawGuessStatusData>(
        "/api/v1/live/shows/status",
        { method: "GET", cache: "no-store" }
      );
      if (unmountedRef.current) return;

      const isCombinedPayload =
        shows &&
        typeof shows === "object" &&
        ("draw_and_guess" in shows || "turtle_soup" in shows || "roast_radio" in shows);

      if (isCombinedPayload) {
        const combined = shows as LiveShowsStatusData;
        const charName = resolveCharacterName(combined.character);
        const dg = combined.draw_and_guess
          ? {
              ...combined.draw_and_guess,
              character: combined.draw_and_guess.character ?? charName,
            }
          : null;
        applyDrawGuess(dg);
        setTurtleSoup(combined.turtle_soup ?? null);
        setRoastRadio(combined.roast_radio ?? null);
        return;
      }

      if (shows && typeof shows === "object" && ("active" in shows || "state" in shows)) {
        applyDrawGuess(shows as DrawGuessStatusData);
        return;
      }
    } catch {
      // Fallback to legacy draw-and-guess overlay endpoint
    }

    try {
      const data = await apiGet<"/api/v1/live/draw-and-guess/overlay", DrawGuessStatusData>(
        "/api/v1/live/draw-and-guess/overlay",
        { cache: "no-store" }
      );
      if (unmountedRef.current) return;
      applyDrawGuess(data);
    } catch {
      // Transient network error ignored in OBS overlay
    }
  }, [applyDrawGuess]);

  useEffect(() => {
    unmountedRef.current = false;
    void poll();
    const interval = setInterval(() => {
      void poll();
    }, 800);
    return () => {
      unmountedRef.current = true;
      clearInterval(interval);
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
    };
  }, [poll]);

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
    turtleSoup?.active || (turtleSoup?.state === "finished" && turtleSoup?.answer)
  );
  const showRoastRadio = Boolean(roastRadio?.active);

  const votesA = Math.max(0, roastRadio?.votes_a ?? 0);
  const votesB = Math.max(0, roastRadio?.votes_b ?? 0);
  const totalVotes = votesA + votesB;
  const pctA = totalVotes > 0 ? Math.round((votesA / totalVotes) * 100) : 50;
  const pctB = 100 - pctA;

  return (
    <div className={styles.root}>
      <div className={styles.overlayStack}>
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
            <svg id="drawing-board" className={`drawing-board ${styles.drawingBoard}`} viewBox="0 0 400 300">
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

        {showTurtleSoup && turtleSoup && (
          <section id="turtle-soup-card" className={styles.showCard} aria-label="海龟汤互动卡片">
            <div className={styles.header}>
              <div className={styles.titleRow}>
                <span>🐢</span>
                <h3 className={styles.showTitle}>海龟汤推理</h3>
                <span className={styles.badge}>{turtleSoup.genre || "悬疑故事"}</span>
              </div>
              <span className={styles.badge}>
                {turtleSoup.state === "finished" ? "已揭晓" : "弹幕提问中"}
              </span>
            </div>
            <p id="ts-surface" className={styles.surfaceBox}>
              {turtleSoup.surface || "正在出题…"}
            </p>
            {(turtleSoup.clues_found?.length ?? 0) > 0 && (
              <div id="ts-clues" className={styles.cluesList}>
                {turtleSoup.clues_found!.map((clue, idx) => (
                  <span key={`${clue}-${idx}`} className={styles.cluePill}>
                    🔍 {clue}
                  </span>
                ))}
              </div>
            )}
            {(turtleSoup.qa_history?.length ?? 0) > 0 && (
              <ul id="ts-qa-list" className={styles.qaList}>
                {turtleSoup.qa_history!.slice(-5).map((qa, idx) => (
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
            {turtleSoup.state === "finished" && turtleSoup.answer && (
              <div id="ts-answer-box" className={styles.roastQuote}>
                {turtleSoup.winner ? `🎉 ${turtleSoup.winner} 还原真相！` : "📖 汤底揭晓："}
                {turtleSoup.answer}
              </div>
            )}
          </section>
        )}

        {showRoastRadio && roastRadio && (
          <section
            id="roast-radio-card"
            className={`${styles.showCard} ${styles.showCardRoast}`}
            aria-label="犀利电台与辩论卡片"
          >
            <div className={styles.header}>
              <div className={styles.titleRow}>
                <span>🎙️</span>
                <h3 className={`${styles.showTitle} ${styles.showTitleRoast}`}>
                  {roastRadio.mode === "debate" ? "锐评辩论台" : "犀利锐评电台"}
                </h3>
                {roastRadio.source_bvid && (
                  <span className={styles.badge}>{roastRadio.source_bvid}</span>
                )}
              </div>
              <span className={styles.badge}>
                {roastRadio.mode === "debate" ? "弹幕站队中" : "锐评中"}
              </span>
            </div>
            <p id="rr-topic" className={styles.surfaceBox}>
              {roastRadio.topic || "今日公开槽点话题"}
            </p>
            {(roastRadio.mode === "debate" || roastRadio.side_a || roastRadio.side_b) && (
              <div id="rr-vote-bar" className={styles.debateWrap}>
                <div className={styles.debateLabels}>
                  <span className={styles.debateSideA}>
                    1 · {roastRadio.side_a || "正方"} ({votesA}票 · {pctA}%)
                  </span>
                  <span className={styles.debateSideB}>
                    2 · {roastRadio.side_b || "反方"} ({votesB}票 · {pctB}%)
                  </span>
                </div>
                <div className={styles.debateBar}>
                  <div className={styles.debateFillA} style={{ width: `${pctA}%` }} />
                  <div className={styles.debateFillB} style={{ width: `${pctB}%` }} />
                </div>
              </div>
            )}
            {roastRadio.last_roast && (
              <p id="rr-last-roast" className={styles.roastQuote}>
                “{roastRadio.last_roast}”
              </p>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
