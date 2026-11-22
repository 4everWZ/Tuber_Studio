import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/shared/api/client";
import styles from "./DrawGuessOverlay.module.css";
import type { DrawGuessStatusData, DrawGuessStroke } from "./types";

function renderStroke(stroke: DrawGuessStroke, index: number) {
  if (stroke.type === "path") {
    return (
      <path
        key={index}
        d={stroke.d}
        fill={stroke.fill || "none"}
        stroke={stroke.stroke || "#38bdf8"}
        strokeWidth={stroke.width || 4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    );
  }
  if (stroke.type === "circle") {
    return (
      <circle
        key={index}
        cx={stroke.cx}
        cy={stroke.cy}
        r={stroke.r}
        fill={stroke.fill || "#38bdf8"}
        stroke={stroke.stroke}
        strokeWidth={stroke.stroke ? stroke.width || 2 : undefined}
      />
    );
  }
  if (stroke.type === "ellipse") {
    return (
      <ellipse
        key={index}
        cx={stroke.cx}
        cy={stroke.cy}
        rx={stroke.rx}
        ry={stroke.ry}
        fill={stroke.fill || "#38bdf8"}
      />
    );
  }
  return null;
}

export function DrawGuessOverlayApp() {
  const [overlayState, setOverlayState] = useState<DrawGuessStatusData | null>(null);
  const [visible, setVisible] = useState(false);

  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmountedRef = useRef(false);

  const poll = useCallback(async () => {
    try {
      const data = await apiGet<"/api/v1/live/draw-and-guess/overlay", DrawGuessStatusData>(
        "/api/v1/live/draw-and-guess/overlay",
        { cache: "no-store" }
      );
      if (unmountedRef.current) return;

      if (data.active || (data.state === "finished" && data.word)) {
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
    } catch {
      // Transient network error ignored in OBS overlay
    }
  }, []);

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

  return (
    <div className={styles.root}>
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
            {overlayState?.hint ? `提示: ${overlayState.hint}` : ""}
          </span>
        </div>
      </div>
    </div>
  );
}
