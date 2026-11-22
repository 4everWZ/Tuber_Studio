import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { studioPath } from "@/shared/api/client";
import { Live2DAvatarCanvas } from "./Live2DAvatarCanvas";
import { formatSongQuality, sanitizeCoverUrl } from "./liveControls";
import styles from "./SceneCanvas.module.css";
import type { LiveViewData, SceneData, SceneItem, SceneItemKind } from "./types";

export interface LiveSceneCanvasProps {
  id?: string;
  scene: SceneData | null | undefined;
  view?: LiveViewData | null;
  editable?: boolean;
  selectedId?: string | null;
  onSelect?: (id: string | null) => void;
  onChange?: (updatedScene: SceneData) => void;
  onWarning?: (message: string) => void;
  readMouth?: () => number | undefined;
  onLive2DState?: (active: boolean) => void;
  className?: string;
}

const KIND_STYLE_CLASS: Record<SceneItemKind, string> = {
  avatar: styles.itemAvatar,
  title: styles.itemTitle,
  now: styles.itemNow,
  lyrics: styles.itemLyrics,
  queue: styles.itemQueue,
  chat: styles.itemChat,
  gifts: styles.itemGifts,
  decoration: styles.itemDecoration,
};

const CARD_KINDS = new Set<SceneItemKind>(["now", "queue", "chat"]);

export function clampSceneItem(
  item: SceneItem,
  changes: Partial<SceneItem>,
  sceneWidth: number,
  sceneHeight: number
): SceneItem {
  const next: SceneItem = { ...item, ...changes };
  next.width = Math.max(40, Math.min(next.width, sceneWidth));
  next.height = Math.max(30, Math.min(next.height, sceneHeight));
  next.x = Math.max(0, Math.min(next.x, sceneWidth - next.width));
  next.y = Math.max(0, Math.min(next.y, sceneHeight - next.height));
  return next;
}

export function LiveSceneCanvas({
  id,
  scene,
  view,
  editable = false,
  selectedId = null,
  onSelect,
  onChange,
  onWarning,
  readMouth,
  onLive2DState,
  className,
}: LiveSceneCanvasProps) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [scaleState, setScaleState] = useState<{ scale: number; left: number; top: number }>({
    scale: 1,
    left: 0,
    top: 0,
  });
  const scaleRef = useRef(1);
  const [failedAssets, setFailedAssets] = useState<Set<string>>(() => new Set());
  const [live2dActive, setLive2dActive] = useState(false);
  const [live2dFailed, setLive2dFailed] = useState(false);

  const [reducedMotion, setReducedMotion] = useState<boolean>(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReducedMotion(mq.matches);
    const onMqChange = () => setReducedMotion(mq.matches);
    mq.addEventListener?.("change", onMqChange);
    return () => mq.removeEventListener?.("change", onMqChange);
  }, []);

  const live2dManifest = view?.live2d?.status === "available" ? view.live2d : null;
  const live2dModelKey = live2dManifest?.model ?? "";

  useEffect(() => {
    setLive2dActive(false);
    setLive2dFailed(false);
  }, [live2dModelKey]);

  const fit = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport || !scene) return;
    const vw = viewport.clientWidth;
    const vh = viewport.clientHeight;
    if (!vw || !vh) {
      scaleRef.current = 1;
      setScaleState({ scale: 1, left: 0, top: 0 });
      return;
    }
    const nextScale = Math.min(vw / scene.width, vh / scene.height);
    scaleRef.current = nextScale > 0 ? nextScale : 1;
    setScaleState({
      scale: nextScale,
      left: (vw - scene.width * nextScale) / 2,
      top: (vh - scene.height * nextScale) / 2,
    });
  }, [scene]);

  useEffect(() => {
    fit();
    const viewport = viewportRef.current;
    if (!viewport || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => fit());
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [fit]);

  const applyItemEdit = useCallback(
    (itemId: string, changes: Partial<SceneItem>) => {
      if (!scene || !onChange) return;
      const items = scene.items.map((item) =>
        item.id === itemId ? clampSceneItem(item, changes, scene.width, scene.height) : item
      );
      onChange({ ...scene, items });
    },
    [scene, onChange]
  );

  const handleCanvasPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!editable || event.button !== 0 || !scene) return;
    const target = event.target as HTMLElement;
    const element = target.closest<HTMLElement>("[data-id]");
    if (!element) {
      onSelect?.(null);
      return;
    }
    const itemId = element.dataset.id;
    const item = scene.items.find((entry) => entry.id === itemId);
    if (!item) return;
    onSelect?.(item.id);
    if (item.locked) return;

    event.preventDefault();
    element.focus();
    try {
      element.setPointerCapture?.(event.pointerId);
    } catch {
      // Synthetic pointer events in jsdom may not register an active pointerId
    }

    const originX = event.clientX;
    const originY = event.clientY;
    const originItem = { ...item };
    const isResize = target.classList.contains("scene-resize");

    const handleMove = (moveEvent: PointerEvent) => {
      const currentScale = scaleRef.current || 1;
      const dx = (moveEvent.clientX - originX) / currentScale;
      const dy = (moveEvent.clientY - originY) / currentScale;
      applyItemEdit(
        item.id,
        isResize
          ? { width: originItem.width + dx, height: originItem.height + dy }
          : { x: originItem.x + dx, y: originItem.y + dy }
      );
    };

    const handleEnd = () => {
      element.removeEventListener("pointermove", handleMove);
      element.removeEventListener("pointerup", handleEnd);
      element.removeEventListener("pointercancel", handleEnd);
    };

    element.addEventListener("pointermove", handleMove);
    element.addEventListener("pointerup", handleEnd);
    element.addEventListener("pointercancel", handleEnd);
  };

  const handleCanvasKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!editable || !scene) return;
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    const item = scene.items.find((entry) => entry.id === selectedId);
    if (!item || item.locked) return;

    event.preventDefault();
    const step = event.shiftKey ? 10 : 1;
    const dx = event.key === "ArrowRight" ? step : event.key === "ArrowLeft" ? -step : 0;
    const dy = event.key === "ArrowDown" ? step : event.key === "ArrowUp" ? -step : 0;
    applyItemEdit(item.id, { x: item.x + dx, y: item.y + dy });
  };

  const characterSlotId = useMemo(
    () => scene?.items.find((item) => item.kind === "avatar" && item.visible)?.id ?? null,
    [scene]
  );

  const usableAssets = useMemo(() => {
    const raw = view?.assets ?? [];
    return raw.filter((asset) => {
      const rawUrl = asset.url || (asset.path ? `/${asset.path}` : "");
      if (!rawUrl) return false;
      const resolved = studioPath(rawUrl);
      return !failedAssets.has(resolved);
    });
  }, [view?.assets, failedAssets]);

  const activeAsset = useMemo(
    () => usableAssets.find((asset) => asset.id === "idle") || usableAssets[0] || null,
    [usableAssets]
  );

  const markAssetFailed = useCallback(
    (src: string) => {
      setFailedAssets((prev) => {
        if (prev.has(src)) return prev;
        const next = new Set(prev);
        next.add(src);
        return next;
      });
      onWarning?.("角色素材加载失败，画面已降级为可用素材");
    },
    [onWarning]
  );

  const renderAvatarBody = (item: SceneItem) => {
    if (item.id !== characterSlotId || !view?.performance?.pets_enabled) {
      return null;
    }

    const showLive2D = Boolean(live2dManifest && !live2dFailed);

    let fallbackContent: React.ReactNode = null;
    if (!showLive2D || !live2dActive) {
      if (activeAsset) {
        const rawUrl = activeAsset.url || (activeAsset.path ? `/${activeAsset.path}` : "");
        const url = studioPath(rawUrl);
        const still = activeAsset.still_url ? studioPath(activeAsset.still_url) : "";
        const stillUsable = Boolean(still && !failedAssets.has(still));

        if (reducedMotion && !stillUsable && url.toLowerCase().endsWith(".gif")) {
          fallbackContent = (
            <span className={`${styles.avatarEmpty} avatar-empty`}>
              {view.character?.name || "角色"}
            </span>
          );
        } else {
          const source = reducedMotion && stillUsable ? still : url;
          fallbackContent = (
            <img
              src={source}
              alt={view.character?.name || "角色"}
              draggable={false}
              onError={() => markAssetFailed(source)}
            />
          );
        }
      } else {
        fallbackContent = <span className={`${styles.avatarEmpty} avatar-empty`}>等待角色素材</span>;
      }
    }

    return (
      <>
        {fallbackContent}
        {showLive2D && live2dManifest && (
          <Live2DAvatarCanvas
            manifest={live2dManifest}
            playback={view.playback}
            readMouth={readMouth}
            onState={(active) => {
              if (active) {
                setLive2dActive(true);
              } else {
                setLive2dActive(false);
                setLive2dFailed(true);
              }
              onLive2DState?.(active);
            }}
          />
        )}
      </>
    );
  };

  const playback = view?.playback;
  const media = playback?.song || playback?.media;
  const song = media?.song;
  const position = playback?.position || 0;

  const renderItemBody = (item: SceneItem) => {
    switch (item.kind) {
      case "avatar":
        return renderAvatarBody(item);
      case "title": {
        const sub = [view?.character?.name, view?.muted ? "已静音" : null]
          .filter(Boolean)
          .join(" · ");
        return (
          <>
            <strong>{scene?.title ?? ""}</strong>
            {sub ? <small>{sub}</small> : null}
          </>
        );
      }
      case "now": {
        const baseDetail = song
          ? `${media?.mode === "sing" ? "角色演唱" : `直接播放 · ${song.artist || "曲库音源"}`} · ${media?.requester ?? ""}`
          : "从曲库点一首喜欢的歌";
        const qualityText = formatSongQuality(song?.quality);
        const detail = qualityText
          ? `${baseDetail} · ${song?.source === "bodian" ? "波点" : "咪咕"} ${qualityText}`
          : baseDetail;
        const coverUrl = sanitizeCoverUrl(song?.cover);
        const pct = Math.min(100, (position / (playback?.duration || 1)) * 100);
        return (
          <>
            <h3>♫ 这一首，唱给你</h3>
            <div className={styles.record}>
              {coverUrl && (
                <img
                  className={styles.recordImg}
                  src={coverUrl}
                  alt=""
                  referrerPolicy="no-referrer"
                />
              )}
            </div>
            <p className={styles.songName}>{song?.title || "等待下一首歌"}</p>
            <p className={styles.songDetail} title={detail}>
              {detail}
            </p>
            <div className={styles.progress}>
              <span style={{ width: `${pct}%` }} />
            </div>
          </>
        );
      }
      case "lyrics": {
        const lyrics = song?.lyrics || [];
        const line = lyrics.filter((entry) => entry.time <= position).at(-1);
        return song
          ? line?.text || (lyrics.length ? "♪" : "暂无同步歌词")
          : playback?.started
            ? playback.text
            : "等一首心动的歌 ♡";
      }
      case "queue": {
        const queuedSongs = (view?.queue || []).filter((q) => q.media);
        return (
          <>
            <h3>♫ 点歌队列</h3>
            {queuedSongs.length ? (
              <ol>
                {queuedSongs.slice(0, 7).map((q, idx) => (
                  <li key={q.id}>
                    <b>{String(idx + 1).padStart(2, "0")}</b>
                    <span>
                      {q.media?.song?.title ?? ""}
                      <small>
                        {q.requester} · {q.media?.mode === "sing" ? "角色演唱" : "直接播放"}
                      </small>
                    </span>
                  </li>
                ))}
              </ol>
            ) : (
              <p>还没有排队的歌，下一首留给你。</p>
            )}
          </>
        );
      }
      case "chat": {
        const chats = (view?.events || []).filter((e) => e.type === "chat").slice(-3);
        return chats.length ? (
          chats.map((e, idx) => (
            <p key={e.id ?? `${e.display_name ?? ""}-${idx}`}>
              <b>{e.display_name}</b>
              <span>{e.text}</span>
            </p>
          ))
        ) : (
          <p>等待弹幕</p>
        );
      }
      case "gifts": {
        const gift = (view?.events || []).filter((e) => e.type === "gift").at(-1);
        return gift ? `${gift.display_name} · ${gift.text}` : "本场心意 · 等待礼物";
      }
      case "decoration":
      default:
        return item.text;
    }
  };

  return (
    <div
      id={id}
      ref={viewportRef}
      className={[styles.viewport, "scene-viewport", className].filter(Boolean).join(" ")}
      style={scene ? { aspectRatio: `${scene.width}/${scene.height}` } : undefined}
    >
      {scene && (
        <div
          className={[
            styles.canvas,
            "scene-canvas",
            scene.background === "ocean" ? `${styles.ocean} ocean` : scene.background,
            editable ? `${styles.editing} editing` : "",
          ]
            .filter(Boolean)
            .join(" ")}
          style={{
            width: `${scene.width}px`,
            height: `${scene.height}px`,
            transform: `scale(${scaleState.scale})`,
            left: `${scaleState.left}px`,
            top: `${scaleState.top}px`,
          }}
          onPointerDown={handleCanvasPointerDown}
          onKeyDown={handleCanvasKeyDown}
        >
          <div className={`${styles.bubbles} scene-bubbles`} />
          {scene.items.map((item) => {
            const isSelected = selectedId === item.id;
            const kindClass = KIND_STYLE_CLASS[item.kind] || "";
            return (
              <section
                key={item.id}
                className={[
                  styles.item,
                  kindClass,
                  "scene-item",
                  `scene-${item.kind}`,
                  isSelected ? `${styles.selected} selected` : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                data-id={item.id}
                data-kind={item.kind}
                data-visible={String(item.visible)}
                data-locked={String(item.locked)}
                tabIndex={editable ? 0 : -1}
                aria-label={item.kind}
                style={{
                  left: `${item.x}px`,
                  top: `${item.y}px`,
                  width: `${item.width}px`,
                  height: `${item.height}px`,
                  zIndex: item.z,
                }}
              >
                <div
                  className={[
                    styles.body,
                    "scene-body",
                    CARD_KINDS.has(item.kind) ? `${styles.card} scene-card` : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  style={
                    item.kind === "decoration"
                      ? { fontSize: `${Math.min(item.width, item.height) * 0.9}px` }
                      : undefined
                  }
                >
                  {renderItemBody(item)}
                </div>
                <span className={`${styles.resize} scene-resize`} />
              </section>
            );
          })}
          <span className={`${styles.aiLabel} scene-ai-label`}>
            虚构 AI 同人角色 · 非官方{view?.weather_attribution ? " · 天气：Open-Meteo" : ""}
          </span>
        </div>
      )}
    </div>
  );
}
