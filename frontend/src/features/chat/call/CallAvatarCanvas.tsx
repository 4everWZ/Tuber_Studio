import { useEffect, useRef, useState } from "react";
import { apiRequest } from "@/shared/api/client";

export interface CallAvatarAssets {
  still?: string | null;
  speaking?: string | null;
  fallback?: string | null;
}

export class CallAvatarController {
  private readonly canvas: HTMLCanvasElement;
  private readonly onVisibleChange?: (shown: boolean, mouth: "open" | "closed") => void;
  private readonly motion =
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-reduced-motion: reduce)")
      : null;
  private version = 0;
  private still: ImageBitmap | null = null;
  private open: ImageBitmap | null = null;
  private shown: ImageBitmap | null = null;

  constructor(
    canvas: HTMLCanvasElement,
    onVisibleChange?: (shown: boolean, mouth: "open" | "closed") => void
  ) {
    this.canvas = canvas;
    this.onVisibleChange = onVisibleChange;
    this.reset();
  }

  reset(name = "语音通话"): void {
    this.version += 1;
    this.still?.close();
    this.open?.close();
    this.still = null;
    this.open = null;
    this.shown = null;
    this.canvas.hidden = true;
    this.canvas.dataset.mouth = "closed";
    this.canvas.setAttribute("aria-label", name);
    this.onVisibleChange?.(false, "closed");
  }

  async load(
    characterName: string,
    avatar: CallAvatarAssets | null | undefined,
    signal: AbortSignal
  ): Promise<void> {
    this.reset(characterName);
    const version = this.version;
    const decode = async (path: string | null | undefined): Promise<ImageBitmap | null> => {
      if (!path || !path.startsWith("/assets/")) return null;
      try {
        const blob = await apiRequest<Blob>(path, {
          signal,
          mode: "same-origin",
          redirect: "error",
          responseType: "blob",
        });
        return await createImageBitmap(blob);
      } catch {
        return null;
      }
    };

    const [still, open] = await Promise.all([decode(avatar?.still), decode(avatar?.speaking)]);
    const fallback =
      !still && avatar?.fallback !== avatar?.still ? await decode(avatar?.fallback) : null;

    if (signal.aborted || version !== this.version) {
      still?.close();
      open?.close();
      fallback?.close();
      return;
    }

    this.still = still || fallback;
    if (still) {
      this.open = open;
    } else {
      open?.close();
    }
    this.draw(false);
  }

  draw(speaking: boolean): void {
    const image = (speaking && !this.motion?.matches && this.open) || this.still;
    if (!image || image === this.shown) return;
    const ctx = this.canvas.getContext("2d");
    if (!ctx) return;
    const scale = Math.min(this.canvas.width / image.width, this.canvas.height / image.height);
    const width = image.width * scale;
    const height = image.height * scale;
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.drawImage(
      image,
      (this.canvas.width - width) / 2,
      (this.canvas.height - height) / 2,
      width,
      height
    );
    this.shown = image;
    const mouth = image === this.open ? "open" : "closed";
    this.canvas.dataset.mouth = mouth;
    this.canvas.hidden = false;
    this.onVisibleChange?.(true, mouth);
  }
}

export interface CallAvatarCanvasProps {
  characterName: string;
  avatar: CallAvatarAssets | null | undefined;
  speaking: boolean;
  active: boolean;
}

export function CallAvatarCanvas({
  characterName,
  avatar,
  speaking,
  active,
}: CallAvatarCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const controllerRef = useRef<CallAvatarController | null>(null);
  const [hasImage, setHasImage] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !active) return;
    const controller = new CallAvatarController(canvas, (shown) => setHasImage(shown));
    controllerRef.current = controller;
    const abort = new AbortController();
    void controller.load(characterName, avatar, abort.signal);
    return () => {
      abort.abort();
      controller.reset();
      controllerRef.current = null;
      setHasImage(false);
    };
  }, [active, characterName, avatar]);

  useEffect(() => {
    controllerRef.current?.draw(speaking);
  }, [speaking]);

  if (!active || !avatar?.speaking) {
    return null;
  }

  return (
    <div id="private-call-avatar">
      <canvas
        ref={canvasRef}
        width={240}
        height={240}
        hidden={!hasImage}
        aria-label={characterName}
      />
    </div>
  );
}
