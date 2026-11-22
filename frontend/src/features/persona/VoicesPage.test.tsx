import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { voiceAvailability, VoicesPage } from "./VoicesPage";

function makeSession(voiceProfile = "fish-omni"): AppSessionValue {
  return {
    authenticated: true,
    localAccess: true,
    wsConnected: true,
    status: {
      character: { id: "fish", display_name: "大肥鱼", voice_profile: voiceProfile },
    } as unknown as NonNullable<AppSessionValue["status"]>,
    settings: { voice_id: voiceProfile } as unknown as NonNullable<AppSessionValue["settings"]>,
    refreshStatus: vi.fn().mockResolvedValue(null),
    refreshSettings: vi.fn().mockResolvedValue(null),
    navigate: vi.fn(),
    logout: vi.fn().mockResolvedValue(undefined),
    onUnauthorized: vi.fn(),
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("VoicesPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("voiceAvailability checks prerecorded, external_api, and omni_reference states", () => {
    expect(voiceAvailability(undefined)).toBe("请选择可用的音色。");
    expect(voiceAvailability({ id: "pre-1", provider: "prerecorded" })).toBe("");
    expect(voiceAvailability({ id: "external_api", provider: "external" })).toContain("外部 API 音色选择");
    expect(
      voiceAvailability({ id: "omni-1", provider: "omni_reference", reference_asset: "ref.wav" })
    ).toBe("");
    expect(voiceAvailability({ id: "omni-2", provider: "omni_reference" })).toBe(
      "该音色缺少可用的参考录音。"
    );
  });

  it("auditions omni_reference and prerecorded voices and discards stale preview results on voice switch", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    let resolveDelayedPreview: ((res: Response) => void) | null = null;

    const fetchSpy = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/persona/voices" && method === "GET") {
        return Promise.resolve(
          jsonResponse([
            {
              id: "fish-omni",
              name: "鱼塘参考音色",
              provider: "omni_reference",
              reference_asset: "voices/fish.wav",
              license_status: "原创授权",
            },
            {
              id: "fish-fixed",
              name: "固定开场白",
              provider: "prerecorded",
              sample_text: "欢迎来到鱼塘！",
              license_status: "官方内置",
            },
            {
              id: "external_api",
              name: "外部 API",
              provider: "external_api",
              license_status: "外部服务",
            },
          ])
        );
      }
      if (url === "/api/v1/persona/voices/preview" && method === "POST") {
        const body = JSON.parse(String(init?.body || "{}"));
        if (body.voice_id === "fish-omni" && body.text === "触发切走") {
          return new Promise<Response>((resolve) => {
            resolveDelayedPreview = resolve;
          });
        }
        if (body.voice_id === "fish-fixed") {
          return Promise.resolve(
            jsonResponse({
              audio_ref: "/api/v1/persona/voices/preview/fixed.wav",
              duration: 2.5,
              text: "欢迎来到鱼塘！",
            })
          );
        }
        return Promise.resolve(
          jsonResponse({
            audio_ref: "/api/v1/persona/voices/preview/synth.wav",
            duration: 1.25,
          })
        );
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(
      <AppSessionProvider value={makeSession("fish-omni")}>
        <VoicesPage />
      </AppSessionProvider>
    );

    const voiceSelect = container.querySelector<HTMLSelectElement>("#preview-voice")!;
    await waitFor(() => {
      expect(voiceSelect.value).toBe("fish-omni");
    });
    expect(container.querySelector("#voices")?.textContent).toContain("原创授权");

    // 1. Synthesize custom omni_reference text
    fireEvent.change(container.querySelector("#preview-text")!, {
      target: { value: "晚安，今天也辛苦了。" },
    });
    fireEvent.submit(container.querySelector("#preview-form")!);

    await waitFor(() => {
      expect(container.querySelector("#preview-state")?.textContent).toContain("合成完成 · 1.25 秒");
    });
    const audio = container.querySelector<HTMLAudioElement>("#preview-audio")!;
    expect(audio.hidden).toBe(false);
    expect(audio.getAttribute("src")).toBe("/api/v1/persona/voices/preview/synth.wav");

    // 2. Switch while an omni request is still in-flight -> stale response discarded
    fireEvent.change(container.querySelector("#preview-text")!, {
      target: { value: "触发切走" },
    });
    fireEvent.submit(container.querySelector("#preview-form")!);
    await waitFor(() => {
      expect(resolveDelayedPreview).not.toBeNull();
    });

    fireEvent.change(voiceSelect, { target: { value: "fish-fixed" } });
    expect(container.querySelector("#preview-submit")?.textContent).toBe("试听固定样本");
    expect(container.querySelector("#voice-preview-help")?.textContent).toContain("固定录音：欢迎来到鱼塘！");

    // Now resolve the stale request -> must NOT replace audioSrc or previewState
    await act(async () => {
      resolveDelayedPreview!(
        jsonResponse({ audio_ref: "/api/v1/persona/voices/preview/stale.wav", duration: 9.9 })
      );
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(container.querySelector("#preview-state")?.textContent).toBe("");

    // 3. Submit fixed prerecorded sample
    fireEvent.submit(container.querySelector("#preview-form")!);
    await waitFor(() => {
      expect(container.querySelector("#preview-state")?.textContent).toContain(
        "预录已准备 · 2.50 秒，点击播放试听。 实际录音台词：欢迎来到鱼塘！"
      );
    });

    // 4. Selecting external_api disables submit with help message
    fireEvent.change(voiceSelect, { target: { value: "external_api" } });
    expect((container.querySelector<HTMLButtonElement>("#preview-submit"))?.disabled).toBe(true);
    expect(screen.getByText(/该角色保留外部 API 音色选择/)).toBeTruthy();
  });
});
