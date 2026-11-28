import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPut, apiRequest, studioPath } from "@/shared/api/client";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { errorMessage } from "@/shared/lib/errors";
import type { ObsLayoutId, SceneProfilesResponse } from "./consoleTypes";
import sceneStyles from "./LiveRulesScene.module.css";
import { clampSceneItem, LiveSceneCanvas } from "./LiveSceneCanvas";
import styles from "./LiveWorkbench.module.css";
import type { LiveViewData, SceneBackground, SceneData, SceneItem, SceneItemKind } from "./types";

const ITEM_NAMES: Record<SceneItemKind, string> = {
  avatar: "角色形象",
  title: "直播标题",
  now: "当前歌曲",
  lyrics: "歌词字幕",
  queue: "点歌队列",
  chat: "弹幕互动",
  gifts: "礼物心意",
  decoration: "小装饰",
};

const ITEM_ICONS: Record<SceneItemKind, string> = {
  avatar: "♙",
  title: "T",
  now: "♫",
  lyrics: "≋",
  queue: "☷",
  chat: "☏",
  gifts: "♡",
  decoration: "✧",
};

interface SystemStatusCharacter {
  character?: {
    voice_profile?: string | null;
  } | null;
}

export interface LiveScenePageProps {
  visible?: boolean;
  showWorkbenchHeading?: boolean;
  onFeedback?: (message: string, error?: boolean) => void;
}

export function LiveScenePage({
  visible = true,
  showWorkbenchHeading = true,
  onFeedback,
}: LiveScenePageProps) {
  const [view, setView] = useState<LiveViewData | null>(null);
  const [voiceProfile, setVoiceProfile] = useState<string>("");
  const [selectedLayout, setSelectedLayout] = useState<ObsLayoutId>("chat");
  const [activeLayout, setActiveLayout] = useState<ObsLayoutId>("chat");
  const [layoutLocked, setLayoutLocked] = useState(false);
  const [profilesSupported, setProfilesSupported] = useState(false);
  const [profilesMap, setProfilesMap] = useState<Partial<Record<ObsLayoutId, SceneData>>>({});

  const [draftScene, setDraftScene] = useState<SceneData | null>(null);
  const [savedScene, setSavedScene] = useState<SceneData | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [editable, setEditable] = useState(true);
  const [busy, setBusy] = useState(false);

  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const selectedLayoutRef = useRef<ObsLayoutId>(selectedLayout);
  selectedLayoutRef.current = selectedLayout;
  const loadedSignatureRef = useRef("");
  const pollingRef = useRef(false);

  useUnsavedGuard("live-scene", dirty);

  const applySavedScene = useCallback((sceneValue: SceneData) => {
    const cloned = structuredClone(sceneValue);
    loadedSignatureRef.current = JSON.stringify(sceneValue);
    setSavedScene(cloned);
    setDraftScene(structuredClone(sceneValue));
    setDirty(false);
  }, []);

  const poll = useCallback(async () => {
    if (pollingRef.current || !visible) return;
    if (typeof document !== "undefined" && document.hidden) return;
    pollingRef.current = true;
    try {
      const [nextView, sysStatus, profilesRes] = await Promise.all([
        apiGet<"/api/v1/live/view", LiveViewData>("/api/v1/live/view"),
        apiGet<"/api/v1/system/status", SystemStatusCharacter>("/api/v1/system/status"),
        apiRequest<Partial<SceneProfilesResponse>>("/api/v1/live/scene/profiles", { method: "GET" }).catch(
          () => null
        ),
      ]);
      setView(nextView);
      setVoiceProfile(sysStatus?.character?.voice_profile || "");

      if (profilesRes && profilesRes.profiles && (profilesRes.profiles.chat || profilesRes.profiles.game)) {
        setProfilesSupported(true);
        setProfilesMap(profilesRes.profiles);
        if (profilesRes.active_layout === "chat" || profilesRes.active_layout === "game") {
          setActiveLayout(profilesRes.active_layout);
        }
        setLayoutLocked(Boolean(profilesRes.layout_locked));

        const targetProfile = profilesRes.profiles[selectedLayoutRef.current];
        if (targetProfile) {
          const nextSig = JSON.stringify(targetProfile);
          if (!dirtyRef.current && nextSig !== loadedSignatureRef.current) {
            applySavedScene(targetProfile);
          }
          return;
        }
      }

      const nextSig = JSON.stringify(nextView.scene);
      if (!dirtyRef.current && nextSig !== loadedSignatureRef.current) {
        applySavedScene(nextView.scene);
      }
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      pollingRef.current = false;
    }
  }, [visible, applySavedScene, onFeedback]);

  useEffect(() => {
    if (!visible) return undefined;
    void poll();
    const timer = setInterval(() => {
      void poll();
    }, 1000);
    return () => clearInterval(timer);
  }, [visible, poll]);

  const markChanged = useCallback((updated: SceneData) => {
    setDraftScene(updated);
    setDirty(true);
  }, []);

  const handleSelectProfile = async (nextLayout: ObsLayoutId) => {
    if (nextLayout === selectedLayout || busy) return;
    if (dirtyRef.current && typeof window !== "undefined") {
      if (!window.confirm("当前布局有未保存的修改，切换布局将放弃这些修改。确定切换？")) {
        return;
      }
    }
    selectedLayoutRef.current = nextLayout;
    setSelectedLayout(nextLayout);
    setSelectedId(null);

    const cached = profilesMap[nextLayout];
    if (cached) {
      applySavedScene(cached);
    } else {
      setDirty(false);
    }

    try {
      const profilesRes = await apiRequest<Partial<SceneProfilesResponse>>("/api/v1/live/scene/profiles", {
        method: "GET",
      });
      if (profilesRes?.profiles) {
        setProfilesSupported(true);
        setProfilesMap(profilesRes.profiles);
        if (profilesRes.active_layout === "chat" || profilesRes.active_layout === "game") {
          setActiveLayout(profilesRes.active_layout);
        }
        setLayoutLocked(Boolean(profilesRes.layout_locked));
        const loaded = profilesRes.profiles[nextLayout];
        if (loaded) {
          applySavedScene(loaded);
        }
      }
    } catch {
      // Keep cached profile if refresh fails
    }
  };

  const selectedItem: SceneItem | null =
    draftScene?.items.find((item) => item.id === selectedId) ?? null;

  const handleSave = async () => {
    if (!draftScene || busy) return;
    setBusy(true);
    try {
      const saved = profilesSupported
        ? await apiRequest<SceneData, SceneData>("/api/v1/live/scene", {
            method: "PUT",
            query: { layout: selectedLayout },
            body: draftScene,
          })
        : await apiPut<"/api/v1/live/scene", SceneData, SceneData>("/api/v1/live/scene", {
            body: draftScene,
          });
      applySavedScene(saved);
      if (profilesSupported) {
        setProfilesMap((prev) => ({ ...prev, [selectedLayout]: saved }));
      }
      onFeedback?.("布局已保存，OBS画面将同步更新。");
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setBusy(false);
    }
  };

  const handleUndo = () => {
    if (!savedScene) return;
    applySavedScene(savedScene);
  };

  const handleReset = async () => {
    if (busy) return;
    if (typeof window !== "undefined" && !window.confirm("将当前布局恢复为默认场景？保存前可撤销。")) {
      return;
    }
    setBusy(true);
    try {
      const defScene = profilesSupported
        ? await apiRequest<SceneData>("/api/v1/live/scene/default", {
            method: "GET",
            query: { layout: selectedLayout },
          })
        : await apiGet<"/api/v1/live/scene/default", SceneData>("/api/v1/live/scene/default");
      markChanged(defScene);
    } catch (error) {
      onFeedback?.(errorMessage(error), true);
    } finally {
      setBusy(false);
    }
  };

  const handleAddDecoration = () => {
    if (!draftScene) return;
    if (draftScene.items.length >= 32) {
      onFeedback?.("最多32个场景组件", true);
      return;
    }
    const idSuffix =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID().slice(0, 8)
        : Math.random().toString(36).slice(2, 10);
    const nextItem: SceneItem = {
      id: `deco_${idSuffix}`,
      kind: "decoration",
      x: 50,
      y: 200,
      width: 100,
      height: 100,
      z: 5,
      text: "🫧",
      visible: true,
      locked: false,
    };
    markChanged({ ...draftScene, items: [...draftScene.items, nextItem] });
    setSelectedId(nextItem.id);
  };

  const handleItemField = (field: keyof SceneItem, rawValue: string | boolean) => {
    if (!draftScene || !selectedItem) return;
    const value =
      typeof rawValue === "boolean"
        ? rawValue
        : field === "text"
          ? rawValue
          : Number(rawValue);
    const items = draftScene.items.map((item) =>
      item.id === selectedItem.id
        ? clampSceneItem(item, { [field]: value }, draftScene.width, draftScene.height)
        : item
    );
    markChanged({ ...draftScene, items });
  };

  const handleDimensionChange = (dimension: "width" | "height", value: number) => {
    if (!draftScene) return;
    if (
      Number.isNaN(value) ||
      value < (dimension === "width" ? 640 : 360) ||
      value > (dimension === "width" ? 3840 : 2160)
    ) {
      return;
    }
    const ratio = value / draftScene[dimension];
    const items = draftScene.items.map((item) => {
      const nextItem = { ...item };
      const posKey = dimension === "width" ? "x" : "y";
      nextItem[dimension] = Math.max(
        dimension === "width" ? 40 : 30,
        Math.min(value, nextItem[dimension] * ratio)
      );
      nextItem[posKey] = Math.max(
        0,
        Math.min(value - nextItem[dimension], nextItem[posKey] * ratio)
      );
      return nextItem;
    });
    markChanged({ ...draftScene, [dimension]: value, items });
  };

  const isTransparent = draftScene?.background === "transparent";

  return (
    <div data-live-page="scene">
      {showWorkbenchHeading ? (
        <div className="page-heading win-heading">
          <h1>演播场景编排</h1>
          <a href={studioPath("/live")} className="win-link-btn" target="_blank" rel="noopener noreferrer">
            打开独立演播窗口 ↗
          </a>
        </div>
      ) : null}

      <div className={styles.studioHeading}>
        <div>
          <h1>场景编排</h1>
        </div>
        <div className="actions">
          <span id="save-state" className={styles.saveState}>
            {dirty ? "布局尚未保存" : "布局已保存"}
          </span>
          <button
            type="button"
            id="undo-layout"
            disabled={!dirty || busy}
            onClick={handleUndo}
          >
            撤销布局修改
          </button>
          <button
            type="button"
            id="save-layout"
            className="primary"
            disabled={!dirty || busy}
            onClick={() => void handleSave()}
          >
            保存布局
          </button>
        </div>
      </div>

      <div id="scene-profile-switcher" className={sceneStyles.profileSwitcherBar}>
        <div className={sceneStyles.profileTabs} role="group" aria-label="OBS 布局方案切换">
          <button
            type="button"
            className={sceneStyles.profileBtn}
            data-scene-profile="chat"
            aria-pressed={selectedLayout === "chat"}
            onClick={() => void handleSelectProfile("chat")}
          >
            <span>💬 闲聊 OBS 布局 (chat)</span>
            {activeLayout === "chat" && (
              <span className={sceneStyles.activeBadge}>当前直播生效</span>
            )}
          </button>
          <button
            type="button"
            className={sceneStyles.profileBtn}
            data-scene-profile="game"
            aria-pressed={selectedLayout === "game"}
            onClick={() => void handleSelectProfile("game")}
          >
            <span>🎮 游戏 OBS 布局 (game)</span>
            {activeLayout === "game" && (
              <span className={sceneStyles.activeBadge}>当前直播生效</span>
            )}
          </button>
        </div>
        <div className={sceneStyles.profileStatusMeta}>
          <span id="scene-active-layout-label">
            直播间当前输出：{activeLayout === "game" ? "🎮 游戏布局 (game)" : "💬 闲聊布局 (chat)"}
          </span>
          {layoutLocked ? (
            <span id="scene-layout-locked-badge" className={sceneStyles.lockedBadge}>
              🔒 已手动锁定
            </span>
          ) : (
            <span className="hint" style={{ margin: 0 }}>
              随节目单自动切换
            </span>
          )}
        </div>
      </div>

      <div className={styles.editorLayout}>
        <aside className={`panel ${styles.layersPanel}`}>
          <div className="panel-heading">
            <h2>场景图层</h2>
            <span className="mini">可拖拽</span>
          </div>
          <div id="layers" className={styles.layersList}>
            {(draftScene?.items ?? []).map((item) => (
              <button
                key={item.id}
                type="button"
                className={styles.layerBtn}
                data-id={item.id}
                aria-pressed={selectedId === item.id}
                onClick={() => setSelectedId(item.id)}
              >
                <span>{ITEM_ICONS[item.kind]}</span>
                <span>{ITEM_NAMES[item.kind]}</span>
                <span>{item.locked ? "锁" : item.visible ? "◉" : "○"}</span>
              </button>
            ))}
          </div>
          <button
            type="button"
            id="add-decoration"
            className={styles.fullBtn}
            onClick={handleAddDecoration}
          >
            ＋ 添加装饰
          </button>
          <button
            type="button"
            id="reset-layout"
            className={`${styles.fullBtn} ${styles.resetLayoutBtn}`}
            disabled={busy}
            onClick={() => void handleReset()}
          >
            恢复默认布局
          </button>
          <div className={styles.layerFooter}>
            <label>
              场景标题
              <input
                id="scene-title"
                maxLength={80}
                value={draftScene?.title ?? ""}
                onChange={(e) => {
                  if (!draftScene) return;
                  markChanged({ ...draftScene, title: e.target.value });
                }}
              />
            </label>
            <label>
              背景
              <select
                id="scene-background"
                value={draftScene?.background ?? "ocean"}
                onChange={(e) => {
                  if (!draftScene) return;
                  markChanged({
                    ...draftScene,
                    background: e.target.value as SceneBackground,
                  });
                }}
              >
                <option value="ocean">深海小窝</option>
                <option value="transparent">透明 · OBS 叠加</option>
              </select>
            </label>
            <div className={styles.fieldsGrid}>
              <label>
                画布宽
                <input
                  id="canvas-width"
                  type="number"
                  min={640}
                  max={3840}
                  value={draftScene?.width ?? 1920}
                  onChange={(e) => handleDimensionChange("width", Number(e.target.value))}
                />
              </label>
              <label>
                画布高
                <input
                  id="canvas-height"
                  type="number"
                  min={360}
                  max={2160}
                  value={draftScene?.height ?? 1080}
                  onChange={(e) => handleDimensionChange("height", Number(e.target.value))}
                />
              </label>
            </div>
          </div>
        </aside>

        <section className={styles.stageArea}>
          <div className={styles.stageToolbar}>
            <span>
              <i className={styles.dot} /> 场景编排（{selectedLayout === "game" ? "游戏 PiP" : "闲聊主舞台"}）{" "}
              <small>预览静音</small>
              {isTransparent && (
                <span className={sceneStyles.transparentTag} data-transparent-preview="true" style={{ marginLeft: 8 }}>
                  透明棋盘预览 · OBS 叠加
                </span>
              )}
            </span>
            <button
              type="button"
              id="toggle-edit"
              aria-pressed={editable}
              onClick={() => setEditable((prev) => !prev)}
            >
              {editable ? "完成编排" : "编辑场景"}
            </button>
          </div>
          <div
            className={`${styles.canvasShell} ${isTransparent ? sceneStyles.transparentCanvasShell : ""}`}
            data-scene-background={draftScene?.background ?? "ocean"}
            data-selected-layout={selectedLayout}
          >
            <LiveSceneCanvas
              id="scene"
              scene={draftScene}
              view={view}
              editable={editable}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onChange={markChanged}
              onWarning={(msg) => onFeedback?.(msg, true)}
            />
          </div>
          <div className={styles.stageCaption}>
            <span>拖动元素调整位置 · 右下角缩放 · 方向键微调</span>
            <span id="canvas-size">
              {draftScene ? `${draftScene.width} × ${draftScene.height}` : "1920 × 1080"}
            </span>
          </div>
        </section>

        <aside className={`panel ${styles.inspectorPanel}`}>
          <div className="panel-heading">
            <h2>组件属性</h2>
            <span id="selected-name" className="mini">
              {selectedItem ? ITEM_NAMES[selectedItem.kind] : "未选择"}
            </span>
          </div>
          <p id="inspector-empty" className="hint" hidden={Boolean(selectedItem)}>
            点击画面或图层，调整组件的位置与显示。
          </p>
          <fieldset id="inspector-fields" disabled={!selectedItem} hidden={!selectedItem}>
            <div className={styles.fieldsGrid}>
              <label>
                X
                <input
                  id="item-x"
                  type="number"
                  value={selectedItem ? Math.round(selectedItem.x) : ""}
                  onChange={(e) => handleItemField("x", e.target.value)}
                />
              </label>
              <label>
                Y
                <input
                  id="item-y"
                  type="number"
                  value={selectedItem ? Math.round(selectedItem.y) : ""}
                  onChange={(e) => handleItemField("y", e.target.value)}
                />
              </label>
              <label>
                宽度
                <input
                  id="item-width"
                  type="number"
                  min={40}
                  value={selectedItem ? Math.round(selectedItem.width) : ""}
                  onChange={(e) => handleItemField("width", e.target.value)}
                />
              </label>
              <label>
                高度
                <input
                  id="item-height"
                  type="number"
                  min={30}
                  value={selectedItem ? Math.round(selectedItem.height) : ""}
                  onChange={(e) => handleItemField("height", e.target.value)}
                />
              </label>
            </div>
            <label>
              图层顺序
              <input
                id="item-z"
                type="number"
                min={0}
                max={100}
                value={selectedItem ? Math.round(selectedItem.z) : ""}
                onChange={(e) => handleItemField("z", e.target.value)}
              />
            </label>
            <label>
              装饰文字／符号
              <input
                id="item-text"
                maxLength={120}
                value={selectedItem?.text ?? ""}
                onChange={(e) => handleItemField("text", e.target.value)}
              />
            </label>
            <div className="checks">
              <label>
                <input
                  id="item-visible"
                  type="checkbox"
                  checked={Boolean(selectedItem?.visible)}
                  onChange={(e) => handleItemField("visible", e.target.checked)}
                />
                显示
              </label>
              <label>
                <input
                  id="item-locked"
                  type="checkbox"
                  checked={Boolean(selectedItem?.locked)}
                  onChange={(e) => handleItemField("locked", e.target.checked)}
                />
                锁定
              </label>
            </div>
          </fieldset>
          <div className={styles.voiceSummary}>
            <h3>当前角色</h3>
            <strong id="character-name">{view?.character?.name || "—"}</strong>
            <p id="voice-state" className="hint">
              {voiceProfile ? `说话音色：${voiceProfile}` : "正在读取声音状态"}
            </p>
            <p id="singing-state" className="hint" />
          </div>
        </aside>
      </div>
    </div>
  );
}
