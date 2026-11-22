import { useEffect, useRef } from "react";
import { studioPath } from "@/shared/api/client";
import type { Live2DManifest, PlaybackSnapshot } from "./types";

interface PixiBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface PixiCoreModel {
  getModel(): { parameters: { ids: string[] } };
  setParameterValueByIndex(index: number, value: number): void;
}

interface PixiInternalModel {
  coreModel: PixiCoreModel;
  motionManager?: {
    expressionManager?: {
      resetExpression?: () => void;
    } | null;
  } | null;
  on(event: "beforeModelUpdate", listener: () => void): void;
}

interface PixiLive2DModel {
  internalModel: PixiInternalModel;
  scale: { set(value: number): void };
  position: { set(x: number, y: number): void };
  getLocalBounds(): PixiBounds;
  motion(group: string, index: number, priority: number): Promise<unknown>;
  expression(name: string): Promise<unknown>;
  destroy(): void;
}

interface PixiApp {
  ticker: { maxFPS: number };
  stage: { addChild(child: PixiLive2DModel): void };
  renderer: { resize(width: number, height: number): void };
  destroy(removeView?: boolean, options?: { children?: boolean }): void;
}

interface PixiGlobal {
  Application: new (options: {
    view: HTMLCanvasElement;
    backgroundAlpha: number;
    autoDensity: boolean;
  }) => PixiApp;
  Ticker: unknown;
  live2d: {
    Live2DModel: {
      registerTicker(ticker: unknown): void;
      from(url: string, options: { autoInteract: boolean }): Promise<PixiLive2DModel>;
    };
  };
}

declare global {
  interface Window {
    PIXI?: PixiGlobal;
  }
}

let scriptsChain: Promise<void> | null = null;
let loadedUrlsSignature = "";

/** Sequentially inject Live2D SDK script tags once per distinct script manifest. */
export function installLive2DScripts(urls: readonly string[]): Promise<void> {
  const resolved = urls.map((url) => studioPath(url));
  const signature = JSON.stringify(resolved);
  if (scriptsChain && loadedUrlsSignature === signature) {
    return scriptsChain;
  }
  loadedUrlsSignature = signature;
  scriptsChain = resolved.reduce(
    (chain, url) =>
      chain.then(
        () =>
          new Promise<void>((resolve, reject) => {
            const script = document.createElement("script");
            script.src = url;
            script.onload = () => resolve();
            script.onerror = () => reject(new Error(`脚本加载失败: ${url}`));
            document.head.append(script);
          })
      ),
    Promise.resolve()
  );
  return scriptsChain;
}

export function resetLive2DScriptCacheForTest(): void {
  scriptsChain = null;
  loadedUrlsSignature = "";
}

export interface Live2DAvatarCanvasProps {
  manifest: Live2DManifest;
  playback?: PlaybackSnapshot | null;
  readMouth?: () => number | undefined;
  onState: (active: boolean) => void;
  className?: string;
}

export function Live2DAvatarCanvas({
  manifest,
  playback,
  readMouth,
  onState,
  className,
}: Live2DAvatarCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const modelRef = useRef<PixiLive2DModel | null>(null);
  const appRef = useRef<PixiApp | null>(null);
  const playbackRef = useRef<PlaybackSnapshot | null | undefined>(playback);
  const readMouthRef = useRef<(() => number | undefined) | undefined>(readMouth);
  const onStateRef = useRef(onState);
  const manifestRef = useRef(manifest);
  const animationRef = useRef<string | null>(null);
  const emotionRef = useRef<string | null>(null);

  playbackRef.current = playback;
  readMouthRef.current = readMouth;
  onStateRef.current = onState;
  manifestRef.current = manifest;

  useEffect(() => {
    const canvas = canvasRef.current;
    const parent = canvas?.parentElement;
    if (!canvas || !parent) return undefined;

    let disposed = false;
    let observer: ResizeObserver | null = null;
    let model: PixiLive2DModel | null = null;
    let app: PixiApp | null = null;

    const destroyInstance = () => {
      disposed = true;
      observer?.disconnect();
      observer = null;
      app?.destroy(false, { children: true });
      app = null;
      appRef.current = null;
      model = null;
      modelRef.current = null;
    };

    const fallback = () => {
      if (disposed) return;
      destroyInstance();
      onStateRef.current(false);
    };

    const fit = () => {
      const width = parent.clientWidth;
      const height = parent.clientHeight;
      if (!width || !height || !model || !app) return;
      app.renderer.resize(width, height);
      const bounds = model.getLocalBounds();
      const scale = Math.min(width / bounds.width, height / bounds.height);
      if (!Number.isFinite(scale) || scale <= 0) return;
      model.scale.set(scale);
      model.position.set(
        (width - bounds.width * scale) / 2 - bounds.x * scale,
        (height - bounds.height * scale) / 2 - bounds.y * scale
      );
    };

    const applyPlayback = (currentPlayback: PlaybackSnapshot | null | undefined) => {
      if (!model || disposed) return;
      const animation = currentPlayback?.animation_id || "idle";
      const emotion = currentPlayback?.emotion || "idle";
      if (animation !== animationRef.current) {
        const motion = manifest.motions?.[animation];
        if (motion) {
          model.motion(motion.group, motion.index, 3).catch(() => fallback());
        }
        animationRef.current = animation;
      }
      if (emotion !== emotionRef.current) {
        const expression = manifest.expressions?.[emotion];
        if (expression) {
          model.expression(expression).catch(() => fallback());
        } else {
          model.internalModel.motionManager?.expressionManager?.resetExpression?.();
        }
        emotionRef.current = emotion;
      }
    };

    const onContextLost = () => fallback();
    canvas.addEventListener("webglcontextlost", onContextLost);

    void (async () => {
      try {
        await installLive2DScripts(manifest.scripts);
        if (disposed || !window.PIXI) {
          if (!disposed) fallback();
          return;
        }
        const { Application, Ticker, live2d } = window.PIXI;
        const { Live2DModel } = live2d;
        Live2DModel.registerTicker(Ticker);
        app = new Application({ view: canvas, backgroundAlpha: 0, autoDensity: true });
        app.ticker.maxFPS = 30;
        appRef.current = app;

        const loadedModel = await Live2DModel.from(studioPath(manifest.model), {
          autoInteract: false,
        });
        if (disposed) {
          loadedModel.destroy();
          return;
        }
        model = loadedModel;
        modelRef.current = loadedModel;
        app.stage.addChild(loadedModel);

        const internal = loadedModel.internalModel;
        const core = internal.coreModel;
        const index = core.getModel().parameters.ids.indexOf(manifest.mouth_parameter);
        if (index < 0) {
          throw new Error("缺少嘴部参数");
        }
        internal.on("beforeModelUpdate", () => {
          const level = readMouthRef.current?.() ?? playbackRef.current?.mouth ?? 0;
          const opening = Math.min(1, Math.max(0, level) * (manifest.mouth_gain ?? 5));
          core.setParameterValueByIndex(index, opening);
        });

        if (typeof ResizeObserver !== "undefined") {
          observer = new ResizeObserver(() => fit());
          observer.observe(parent);
        }
        fit();
        onStateRef.current(true);
        applyPlayback(playbackRef.current);
      } catch {
        fallback();
      }
    })();

    return () => {
      canvas.removeEventListener("webglcontextlost", onContextLost);
      destroyInstance();
    };
  }, [manifest]);

  useEffect(() => {
    const model = modelRef.current;
    if (!model) return;
    const animation = playback?.animation_id || "idle";
    const emotion = playback?.emotion || "idle";
    const currentManifest = manifestRef.current;

    const triggerFallback = () => {
      modelRef.current = null;
      appRef.current?.destroy(false, { children: true });
      appRef.current = null;
      onStateRef.current(false);
    };

    if (animation !== animationRef.current) {
      const motion = currentManifest.motions?.[animation];
      if (motion) {
        model.motion(motion.group, motion.index, 3).catch(() => triggerFallback());
      }
      animationRef.current = animation;
    }
    if (emotion !== emotionRef.current) {
      const expression = currentManifest.expressions?.[emotion];
      if (expression) {
        model.expression(expression).catch(() => triggerFallback());
      } else {
        model.internalModel.motionManager?.expressionManager?.resetExpression?.();
      }
      emotionRef.current = emotion;
    }
  }, [playback]);

  return (
    <canvas
      ref={canvasRef}
      className={className ? `live2d-avatar ${className}` : "live2d-avatar"}
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        pointerEvents: "none",
      }}
    />
  );
}
