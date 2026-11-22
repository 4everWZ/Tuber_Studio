import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { characterDisplayStore } from "@/shared/hooks/useCharacterDisplay";
import { clearDirtyRegistry, hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import { ProvidersPanel } from "./ProvidersPanel";
import { SettingsPage } from "./SettingsPage";
import { buildOmniSettingsPayload } from "./types";

function makeSettingsPayload(overrides: Record<string, unknown> = {}) {
  return {
    omni: {
      base_url: "http://127.0.0.1:8080/v1",
      model: "omni-default",
      responses_model: "llm-default",
      operations_max_tokens: 2048,
      enable_thinking: true,
      video_options: { max_frames: 5, frame_strategy: "per_frame" },
      responses_options: { parallel_reads: 2, max_output_tokens: 4096 },
    },
    provider_credentials: { omni: true },
    ...overrides,
  } as unknown as NonNullable<AppSessionValue["settings"]>;
}

function makeStatusPayload() {
  return {
    character: {
      id: "fish",
      display_name: "大肥鱼",
    },
  } as unknown as NonNullable<AppSessionValue["status"]>;
}

function makeSession(overrides: Partial<AppSessionValue> = {}): AppSessionValue {
  const settings = overrides.settings ?? makeSettingsPayload();
  return {
    authenticated: true,
    localAccess: true,
    wsConnected: true,
    status: makeStatusPayload(),
    settings,
    refreshStatus: vi.fn().mockResolvedValue(makeStatusPayload()),
    refreshSettings: vi.fn().mockResolvedValue(settings),
    navigate: vi.fn(),
    logout: vi.fn().mockResolvedValue(undefined),
    onUnauthorized: vi.fn(),
    ...overrides,
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ProvidersPanel & SettingsPage", () => {
  afterEach(() => {
    characterDisplayStore.reset();
    clearDirtyRegistry();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("buildOmniSettingsPayload enforces single gateway and credential exclusivity", () => {
    const base = {
      baseUrl: " http://127.0.0.1:8080/v1 ",
      model: "omni-default",
      responsesModel: "llm-default",
      operationsMaxTokens: 2048,
      enableThinking: true,
      maxFrames: 5,
      parallelReads: 2,
      maxOutputTokens: 4096,
    };

    expect(buildOmniSettingsPayload(base)).toEqual({
      omni: {
        base_url: "http://127.0.0.1:8080/v1",
        model: "omni-default",
        responses_model: "llm-default",
        operations_max_tokens: 2048,
        enable_thinking: true,
        video_options: { max_frames: 5 },
        responses_options: { parallel_reads: 2, max_output_tokens: 4096 },
      },
    });

    expect(buildOmniSettingsPayload({ ...base, key: "sk-omni-1" })).toMatchObject({
      credentials: { omni: "sk-omni-1" },
    });

    expect(buildOmniSettingsPayload({ ...base, clearKey: true })).toMatchObject({
      clear_credentials: ["omni"],
    });

    expect(() =>
      buildOmniSettingsPayload({ ...base, key: "sk-omni-1", clearKey: true })
    ).toThrow(/填写新凭据或清除已有凭据/);
  });

  it("exposes only the single workstation Omni gateway with zero Ollama or extra engine fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({}))
    );

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <ProvidersPanel />
      </AppSessionProvider>
    );

    await waitFor(() => {
      expect(container.querySelector<HTMLInputElement>("#omni-model")?.value).toBe("omni-default");
    });

    // Zero Ollama or removed multi-engine selectors
    for (const forbiddenId of [
      "#llm-kind",
      "#tts-kind",
      "#background-base",
      "#tts-form",
      "#llm-model-select",
      "#inference-node-panel",
      "#ollama-base-url",
    ]) {
      expect(container.querySelector(forbiddenId)).toBeNull();
    }
    expect(container.textContent).not.toMatch(/ollama|InferenceNode|tts_kind/i);

    // Gateway connection & key section starts collapsed inside <details> with single password input
    const details = container.querySelector<HTMLDetailsElement>("#providers-form details")!;
    expect(details.open).toBe(false);
    expect(container.querySelectorAll('#providers-form input[type="password"]').length).toBe(1);
    expect(container.querySelector<HTMLInputElement>("#omni-base-url")?.value).toBe(
      "http://127.0.0.1:8080/v1"
    );
    expect(container.querySelector<HTMLInputElement>("#omni-responses-model")?.value).toBe(
      "llm-default"
    );
    expect(container.querySelector<HTMLInputElement>("#llm-operations-tokens")?.value).toBe("2048");
    expect(container.querySelector<HTMLInputElement>("#llm-enable-thinking")?.checked).toBe(true);
    expect(container.querySelector<HTMLInputElement>("#video-max-frames")?.value).toBe("5");
    expect(container.querySelector<HTMLInputElement>("#background-parallel")?.value).toBe("2");
    expect(container.querySelector<HTMLInputElement>("#background-budget")?.value).toBe("4096");
  });

  it("saves Omni settings, runs health check & model discovery, and surfaces conflicts", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/system/settings/providers" && method === "PUT") {
        return jsonResponse({ ok: true });
      }
      if (url === "/api/v1/system/providers/check" && method === "POST") {
        return jsonResponse({
          foreground: { available: true, context_tokens: 32768 },
          background: { available: true, context_tokens: 131072 },
        });
      }
      if (url === "/api/v1/system/providers/models" && method === "GET") {
        return jsonResponse({
          models: ["omni-default", "qwen3-omni-realtime", "llm-default"],
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const updatedSettings = makeSettingsPayload({
      omni: {
        base_url: "http://127.0.0.1:8080/v1",
        model: "omni-default",
        responses_model: "llm-9b",
        operations_max_tokens: 3000,
        enable_thinking: false,
        video_options: { max_frames: 6, frame_strategy: "per_frame" },
        responses_options: { parallel_reads: 3, max_output_tokens: 6000 },
      },
    });
    const refreshSettings = vi.fn().mockResolvedValue(updatedSettings);
    const refreshStatus = vi.fn().mockResolvedValue(makeStatusPayload());

    const { container } = render(
      <AppSessionProvider value={makeSession({ refreshSettings, refreshStatus })}>
        <ProvidersPanel />
      </AppSessionProvider>
    );

    const saveBtn = container.querySelector<HTMLButtonElement>("#save-providers")!;
    expect(saveBtn.disabled).toBe(true);

    // Edit fields
    fireEvent.change(container.querySelector("#omni-responses-model")!, {
      target: { value: "llm-9b" },
    });
    fireEvent.change(container.querySelector("#llm-operations-tokens")!, {
      target: { value: "3000" },
    });
    fireEvent.click(container.querySelector("#llm-enable-thinking")!);
    fireEvent.change(container.querySelector("#video-max-frames")!, {
      target: { value: "6" },
    });
    fireEvent.change(container.querySelector("#background-parallel")!, {
      target: { value: "3" },
    });
    fireEvent.change(container.querySelector("#background-budget")!, {
      target: { value: "6000" },
    });

    expect(saveBtn.disabled).toBe(false);
    expect(hasAnyUnsavedChanges()).toBe(true);

    // Conflicting key + clear shows validation error without hitting PUT
    fireEvent.change(container.querySelector("#omni-key")!, {
      target: { value: "sk-new-secret" },
    });
    fireEvent.click(container.querySelector("#omni-clear-key")!);
    fireEvent.submit(container.querySelector("#providers-form")!);

    await waitFor(() => {
      expect(container.querySelector("#providers-save-state")?.textContent).toContain(
        "填写新凭据或清除已有凭据"
      );
    });
    expect(
      fetchSpy.mock.calls.filter(
        ([u, init]) => String(u) === "/api/v1/system/settings/providers" && init?.method === "PUT"
      ).length
    ).toBe(0);

    // Uncheck clearKey so credential update proceeds cleanly
    fireEvent.click(container.querySelector("#omni-clear-key")!);
    fireEvent.submit(container.querySelector("#providers-form")!);

    await waitFor(() => {
      expect(container.querySelector("#providers-save-state")?.textContent).toBe("推理设置已保存。");
    });
    expect(hasAnyUnsavedChanges()).toBe(false);
    expect(refreshStatus).toHaveBeenCalled();

    const putCall = fetchSpy.mock.calls.find(
      ([u, init]) => String(u) === "/api/v1/system/settings/providers" && init?.method === "PUT"
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      omni: {
        base_url: "http://127.0.0.1:8080/v1",
        model: "omni-default",
        responses_model: "llm-9b",
        operations_max_tokens: 3000,
        enable_thinking: false,
        video_options: { max_frames: 6 },
        responses_options: { parallel_reads: 3, max_output_tokens: 6000 },
      },
      credentials: { omni: "sk-new-secret" },
    });

    // Check saved connections (foreground + background cards)
    fireEvent.click(container.querySelector("#check-providers-settings")!);
    await waitFor(() => {
      expect(container.querySelector("#providers-check-result")?.textContent).toContain(
        "前台模型卡：已读取（32768 tokens） · 后台模型卡：已读取（131072 tokens）"
      );
    });

    // Discover foreground model aliases
    fireEvent.click(container.querySelector("#omni-discover-models")!);
    await waitFor(() => {
      expect(container.querySelector("#omni-discovery-state")?.textContent).toBe(
        "前台报告了 3 个别名。"
      );
    });
    const options = Array.from(
      container.querySelectorAll<HTMLOptionElement>("#omni-model-options option")
    ).map((opt) => opt.value);
    expect(options).toEqual(["omni-default", "qwen3-omni-realtime", "llm-default"]);
  });

  it("wires CharacterDisplayPanel, IllustrationPolicyPanel, and ArtworkReferencesPanel on SettingsPage", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let petRequests = 0;

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/system/settings/performance" && method === "GET") {
        return jsonResponse({ pets_enabled: false, avatar_enabled: false });
      }
      if (url === "/api/v1/system/settings/performance" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        return jsonResponse({
          pets_enabled: Boolean(body.pets_enabled),
          avatar_enabled: Boolean(body.avatar_enabled),
        });
      }
      if (url === "/api/v1/persona/pets/fish" && method === "GET") {
        petRequests += 1;
        return jsonResponse({
          character_id: "fish",
          selected_asset_id: null,
          items: [
            {
              asset_id: "pet-1",
              title: "摸鱼待机",
              enabled: true,
              manifest_owned: true,
              preview_url: "/api/v1/persona/memes/pet-1/preview",
            },
          ],
        });
      }
      if (url === "/api/v1/persona/pets/fish" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        return jsonResponse({
          character_id: "fish",
          selected_asset_id: body.asset_id,
          items: [],
        });
      }
      if (url === "/api/v1/persona/illustrations/policy" && method === "GET") {
        return jsonResponse({ generate_images: false, use_memes: true });
      }
      if (url === "/api/v1/persona/illustrations/policy" && method === "PATCH") {
        return jsonResponse({ generate_images: true, use_memes: true });
      }
      if (url === "/api/v1/persona/artwork-references/fish" && method === "GET") {
        return jsonResponse({
          character_id: "fish",
          variants: {
            q: { available: true, source: "default", preview_url: "/ref-q.png" },
            mature: { available: false, source: "none" },
          },
        });
      }
      if (url === "/api/v1/persona/artwork-references/fish/q" && method === "PUT") {
        return jsonResponse({
          character_id: "fish",
          variants: {
            q: { available: true, source: "upload", preview_url: "/ref-q-custom.png" },
            mature: { available: false, source: "none" },
          },
        });
      }
      if (url === "/api/v1/persona/artwork-references/fish/q" && method === "DELETE") {
        return jsonResponse({
          character_id: "fish",
          variants: {
            q: { available: true, source: "default", preview_url: "/ref-q.png" },
            mature: { available: false, source: "none" },
          },
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(
      <AppSessionProvider value={makeSession()}>
        <SettingsPage />
      </AppSessionProvider>
    );

    // Live avatar toggle stays disabled TBD
    const liveAvatarToggle = screen.getByLabelText(/直播 Avatar（TBD／暂不可用）/) as HTMLInputElement;
    expect(liveAvatarToggle.disabled).toBe(true);
    expect(liveAvatarToggle.checked).toBe(false);

    // Wait for character display + illustration policy + artwork references initial reads
    const petsToggle = container.querySelector<HTMLInputElement>('[data-display-setting="pets_enabled"]')!;
    await waitFor(() => {
      expect(petsToggle.disabled).toBe(false);
    });
    expect(petRequests).toBe(0);

    // Enable pets -> shows pet picker button, then opens MemePickerModal lazily
    fireEvent.click(petsToggle);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "选择宠物形象" })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: "选择宠物形象" }));
    await waitFor(() => {
      expect(petRequests).toBe(1);
    });
    await screen.findByText("摸鱼待机");

    // Illustration policy PATCH
    const genImgToggle = container.querySelector<HTMLInputElement>(
      '#illustration-policy input[name="generate_images"]'
    )!;
    await waitFor(() => {
      expect(genImgToggle.disabled).toBe(false);
    });
    fireEvent.click(genImgToggle);
    await waitFor(() => {
      expect(container.querySelector("#illustration-policy-result")?.textContent).toBe("已保存");
    });

    // Artwork reference client validation & upload + reset
    await waitFor(() => {
      expect(container.querySelector("#artwork-reference-scope")?.textContent).toContain("大肥鱼");
    });
    const qFieldset = Array.from(
      container.querySelectorAll<HTMLFieldSetElement>("#artwork-reference-variants fieldset")
    )[0];
    const fileInput = qFieldset.querySelector<HTMLInputElement>('input[type="file"]')!;

    const badFile = new File(["not-image"], "bad.txt", { type: "text/plain" });
    fireEvent.change(fileInput, { target: { files: [badFile] } });
    expect(container.querySelector("#artwork-reference-result")?.textContent).toBe(
      "请选择不超过 4 MiB 的 PNG 或 JPEG 图片。"
    );

    const pngFile = new File([new Uint8Array([137, 80, 78, 71])], "q.png", { type: "image/png" });
    fireEvent.change(fileInput, { target: { files: [pngFile] } });
    await waitFor(() => {
      expect(container.querySelector("#artwork-reference-result")?.textContent).toBe("参考图已更新。");
    });
    expect(qFieldset.querySelector("p")?.textContent).toBe("已上传");

    const resetRefBtn = qFieldset.querySelector<HTMLButtonElement>("button")!;
    expect(resetRefBtn.disabled).toBe(false);
    fireEvent.click(resetRefBtn);
    await waitFor(() => {
      expect(container.querySelector("#artwork-reference-result")?.textContent).toBe("已恢复默认设置。");
    });
    expect(qFieldset.querySelector("p")?.textContent).toBe("默认图");
  });
});
