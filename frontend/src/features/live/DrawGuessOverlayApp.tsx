import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiRequest } from "@/shared/api/client";
import styles from "./DrawGuessOverlay.module.css";
import { ShowStageLayer } from "./ShowStageLayer";
import type {
  ExtendedLiveShowsStatusData,
  LivePollState,
  RundownRuntimeState,
} from "./consoleTypes";
import type {
  DrawGuessStatusData,
  LiveShowsStatusData,
  RoastRadioStatusData,
  TurtleSoupStatusData,
} from "./types";

function resolveCharacterName(character: LiveShowsStatusData["character"]): string | undefined {
  if (!character) return undefined;
  if (typeof character === "string") return character;
  return character.name;
}

function isPreviewModeEnabled(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("preview") === "1";
}

export function DrawGuessOverlayApp() {
  const [overlayState, setOverlayState] = useState<DrawGuessStatusData | null>(null);
  const [visible, setVisible] = useState(false);
  const [turtleSoup, setTurtleSoup] = useState<TurtleSoupStatusData | null>(null);
  const [roastRadio, setRoastRadio] = useState<RoastRadioStatusData | null>(null);
  const [livePoll, setLivePoll] = useState<LivePollState | null>(null);
  const [rundown, setRundown] = useState<RundownRuntimeState | null>(null);

  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmountedRef = useRef(false);

  const previewMode = isPreviewModeEnabled();

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
      const shows = await apiRequest<ExtendedLiveShowsStatusData | DrawGuessStatusData>(
        "/api/v1/live/shows/status",
        { method: "GET", cache: "no-store" }
      );
      if (unmountedRef.current) return;

      const isCombinedPayload =
        shows &&
        typeof shows === "object" &&
        ("draw_and_guess" in shows ||
          "turtle_soup" in shows ||
          "roast_radio" in shows ||
          "poll" in shows ||
          "rundown" in shows);

      if (isCombinedPayload) {
        const combined = shows as ExtendedLiveShowsStatusData;
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
        setLivePoll(combined.poll ?? null);
        setRundown(combined.rundown ?? null);
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

  return (
    <div
      id="draw-guess-overlay-root"
      className={previewMode ? `${styles.root} ${styles.rootPreview}` : styles.root}
    >
      <ShowStageLayer
        drawGuess={overlayState}
        drawGuessVisible={visible}
        turtleSoup={turtleSoup}
        roastRadio={roastRadio}
        poll={livePoll}
        rundown={rundown}
        previewStandby={previewMode}
      />
    </div>
  );
}
