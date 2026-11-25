import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseHashRoute } from "@/shell/navigation";
import { VideoStudioPage } from "./VideoStudioPage";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SAMPLE_OVERVIEW = {
  jobs: [
    {
      id: "rem_demo_01",
      status: "ready_for_review",
      publish_mode: "preview_review",
      title: "一分钟看懂 Remotion 帧对齐",
      planner_source: "llm",
      planner_error: null,
      plan: {
        title: "一分钟看懂 Remotion 帧对齐",
        summary: "用音频真实帧数驱动 React 视频动画。",
        tags: ["Aqua", "Remotion"],
        video_mode: "project_showcase",
        fps: 30,
        width: 1920,
        height: 1080,
        include_avatar: false,
        scenes: [
          {
            scene_id: "s1",
            template: "TitleCard",
            heading: "音频驱动帧对齐",
            visual_props: { subtitle: "零漂移合成", bullets: ["精准时码"] },
            lines: [{ line_id: "l1", text: "大家好，我是 Aqua！", avatar_cue: "cheerful" }],
            avatar_cue: "cheerful",
          },
          {
            scene_id: "s2",
            template: "CodeWalkthrough",
            heading: "核心代码拆解",
            visual_props: {
              language: "typescript",
              code_snippet: "const frames = Math.ceil(duration * fps);",
            },
            lines: [{ line_id: "l2", text: "一行公式就能把音频时长映射到精准帧号。", avatar_cue: "curious" }],
            avatar_cue: "curious",
          },
        ],
        sources: [
          {
            source_id: "video:BV1xx411c7mD",
            kind: "watched_video",
            scope: "public",
            title: "Remotion 实战视频",
            url: "https://www.bilibili.com/video/BV1xx411c7mD",
          },
          {
            source_id: "web:gh1",
            kind: "web_research",
            scope: "public",
            title: "aqua-org/studio README",
            url: "https://github.com/aqua-org/studio",
            fetch_status: "fetched",
          },
          {
            source_id: "web:bad1",
            kind: "web_research",
            scope: "public",
            title: "docs.example.org",
            url: "https://docs.example.org/guide",
            fetch_status: "fallback",
            fetch_error: "TimeoutError: timed out",
          },
        ],
      },
      manifest: {
        job_id: "rem_demo_01",
        fps: 30,
        width: 1920,
        height: 1080,
        total_frames: 90,
        total_duration_seconds: 3.0,
        cues: [],
      },
      qc_report: {
        passed: true,
        issues: [],
        size_bytes: 524288,
        expected_duration_seconds: 3.0,
        probed_duration_seconds: 3.01,
        duration_drift_seconds: 0.01,
        width: 1920,
        height: 1080,
        loudnorm_applied: true,
        cover_generated: true,
      },
      media: {
        video: "/api/v1/operations/videos/rem_demo_01/media/video",
        subtitles: "/api/v1/operations/videos/rem_demo_01/media/subtitles",
        subtitles_vtt: "/api/v1/operations/videos/rem_demo_01/media/subtitles_vtt",
        cover: "/api/v1/operations/videos/rem_demo_01/media/cover",
        props: "/api/v1/operations/videos/rem_demo_01/media/props",
        code: "/api/v1/operations/videos/rem_demo_01/media/code",
        manifest: "/api/v1/operations/videos/rem_demo_01/media/manifest",
      },
      created_at: 1_760_000_000,
      updated_at: 1_760_000_100,
    },
  ],
  policy: {
    publish_mode: "preview_review",
    auto_publish_enabled: false,
    default_fps: 30,
    default_width: 1920,
    default_height: 1080,
    max_render_concurrency: 2,
    render_timeout_seconds: 300,
    upload_timeout_seconds: 600,
  },
  environment: {
    node: { ready: true },
    npx: { ready: true },
    ffmpeg: { ready: true },
    ffprobe: { ready: true },
    remotion_studio: { ready: true, label: "remotion-studio" },
    browser: { ready: true, kind: "chrome" },
    biliup: { ready: false },
    omni_llm: { ready: true },
    omni_tts: { ready: true },
  },
  candidates: [{ bvid: "BV1xx411c7mD", title: "Remotion 实战视频" }],
};

describe("VideoStudioPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders job queue, video preview with QC, storyboard scenes, and Remotion code artifacts", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/v1/operations/videos")) {
        return jsonResponse(SAMPLE_OVERVIEW);
      }
      if (url.endsWith("/media/props")) {
        return new Response('{"title":"一分钟看懂 Remotion 帧对齐","totalFrames":90}', {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return jsonResponse({ ok: true });
    });

    render(<VideoStudioPage route={parseHashRoute("#videos")} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "一分钟看懂 Remotion 帧对齐" })).toBeInTheDocument();
    });
    expect(screen.getByText("质检通过")).toBeInTheDocument();
    expect(screen.getByText("LLM 策划")).toBeInTheDocument();
    expect(screen.getByText("开源项目 / 作品介绍")).toBeInTheDocument();
    expect(screen.getByText("已抓取")).toBeInTheDocument();
    expect(screen.getByText("抓取回退")).toBeInTheDocument();
    expect(screen.getByText(/TimeoutError: timed out/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://github.com/aqua-org/studio" })).toHaveAttribute(
      "href",
      "https://github.com/aqua-org/studio"
    );

    await user.click(screen.getByRole("tab", { name: /分镜脚本/ }));
    expect(screen.getByText(/音频驱动帧对齐/)).toBeInTheDocument();
    expect(screen.getByText(/const frames = Math\.ceil/)).toBeInTheDocument();
    const sourceLinks = screen.getAllByRole("link", { name: "打开原来源" });
    expect(sourceLinks[0]).toHaveAttribute("href", "https://www.bilibili.com/video/BV1xx411c7mD");
    expect(sourceLinks[1]).toHaveAttribute("href", "https://github.com/aqua-org/studio");

    await user.click(screen.getByRole("tab", { name: "Remotion 代码与 Props" }));
    await waitFor(() => {
      expect(screen.getByText(/"totalFrames":90/)).toBeInTheDocument();
    });
    expect(fetchMock).toHaveBeenCalled();
  });

  it("supports video_mode, 2-10 scenes, brief_text with counter, and reference_urls in the composer", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method || "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/api/v1/operations/videos") && method === "GET") {
        return jsonResponse(SAMPLE_OVERVIEW);
      }
      if (url.endsWith("/api/v1/operations/videos/plan") && method === "POST") {
        return jsonResponse({
          ok: true,
          job_id: "rem_rich_01",
          status: "script_planned",
          planner_source: "llm",
          reference_diagnostics: [],
        });
      }
      return jsonResponse({ ok: true });
    });

    render(<VideoStudioPage route={parseHashRoute("#videos")} />);
    await waitFor(() => {
      expect(screen.getByLabelText("视频选题")).toBeInTheDocument();
    });

    const sceneSelect = screen.getByLabelText("分镜数量");
    expect(sceneSelect.querySelectorAll("option").length).toBe(9);
    expect(screen.getByRole("option", { name: "10 镜" })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("视频类型"), "project_showcase");
    await user.selectOptions(sceneSelect, "10");
    await user.type(screen.getByLabelText("视频选题"), "Aqua Studio 开源发布");
    await user.type(screen.getByLabelText("参考资料或原始大纲"), "核心特性：本地离线工作坊");
    expect(screen.getByText("12 / 8000")).toBeInTheDocument();
    await user.type(
      screen.getByLabelText("公开参考链接"),
      "https://github.com/aqua-org/studio\n\nhttps://docs.example.org/guide"
    );
    await user.click(screen.getByRole("button", { name: "策划脚本" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/api/v1/operations/videos/plan") && c.method === "POST")).toBe(true);
    });
    const planCall = calls.find((c) => c.url.endsWith("/api/v1/operations/videos/plan"));
    expect(planCall?.body).toMatchObject({
      topic: "Aqua Studio 开源发布",
      video_mode: "project_showcase",
      brief_text: "核心特性：本地离线工作坊",
      reference_urls: ["https://github.com/aqua-org/studio", "https://docs.example.org/guide"],
      max_scenes: 10,
    });
  }, 15_000);

  it("triggers script planning from the composer bar", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method || "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/api/v1/operations/videos") && method === "GET") {
        return jsonResponse(SAMPLE_OVERVIEW);
      }
      if (url.endsWith("/api/v1/operations/videos/plan") && method === "POST") {
        return jsonResponse({
          ok: true,
          job_id: "rem_new_99",
          status: "script_planned",
          planner_source: "llm",
        });
      }
      return jsonResponse({ ok: true });
    });

    render(<VideoStudioPage route={parseHashRoute("#videos")} />);
    await waitFor(() => {
      expect(screen.getByLabelText("视频选题")).toBeInTheDocument();
    });

    await user.type(screen.getByLabelText("视频选题"), "Python 异步并发图解");
    await user.click(screen.getByRole("button", { name: "策划脚本" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/api/v1/operations/videos/plan") && c.method === "POST")).toBe(true);
    });
    const planCall = calls.find((c) => c.url.endsWith("/api/v1/operations/videos/plan"));
    expect(planCall?.body).toMatchObject({
      topic: "Python 异步并发图解",
      fps: 30,
      include_avatar: true,
      bgm_enabled: true,
      max_scenes: 4,
    });
  });

  it("pins all_channels memes (excluding interaction_only), attaches exclusive screenshots, and swaps scene memes in script tab", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method || "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/api/v1/operations/videos") && method === "GET") {
        return jsonResponse(SAMPLE_OVERVIEW);
      }
      if (url.endsWith("/api/v1/persona/memes") && method === "GET") {
        return jsonResponse({
          items: [
            {
              asset_id: "meme_public_1",
              title: "全渠道欢呼",
              enabled: true,
              missing: false,
              usage_scope: "all_channels",
              license: { name: "CC-BY-4.0" },
            },
            {
              asset_id: "meme_private_1",
              title: "仅互动黑话",
              enabled: true,
              missing: false,
              usage_scope: "interaction_only",
              license: { name: "CC-BY-4.0" },
            },
          ],
        });
      }
      if (url.endsWith("/api/v1/operations/videos/plan") && method === "POST") {
        return jsonResponse({
          ok: true,
          job_id: "rem_pinned_01",
          status: "script_planned",
          planner_source: "llm",
        });
      }
      if (url.endsWith("/api/v1/operations/videos/rem_demo_01/plan") && method === "PATCH") {
        return jsonResponse({ ok: true });
      }
      return jsonResponse({ ok: true });
    });

    render(<VideoStudioPage route={parseHashRoute("#videos")} />);
    await waitFor(() => {
      expect(screen.getByLabelText("视频选题")).toBeInTheDocument();
    });

    // Open composer pinned meme picker -> only all_channels meme is offered
    await user.click(screen.getByRole("button", { name: /置顶图库表情/ }));
    await screen.findByText("全渠道欢呼");
    expect(screen.queryByText("仅互动黑话")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "选用全渠道欢呼" }));

    // Attach exclusive screenshot
    const uploadInput = screen.getByLabelText("本次专属公开配图/截图") as HTMLInputElement;
    const shotFile = new File(["pngbytes"], "shot.png", { type: "image/png" });
    await user.upload(uploadInput, shotFile);
    await screen.findByText("shot.png");

    await user.type(screen.getByLabelText("视频选题"), "带置顶表情的演示");
    await user.click(screen.getByRole("button", { name: "策划脚本" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/api/v1/operations/videos/plan") && c.method === "POST")).toBe(true);
    });
    const planCall = calls.find((c) => c.url.endsWith("/api/v1/operations/videos/plan"));
    expect(planCall?.body).toMatchObject({
      topic: "带置顶表情的演示",
      pinned_meme_ids: ["meme_public_1"],
      uploaded_images: [
        expect.objectContaining({
          filename: "shot.png",
          mime_type: "image/png",
        }),
      ],
    });

    // Switch to Script tab and swap scene meme
    await user.click(screen.getByRole("tab", { name: /分镜脚本/ }));
    const swapButtons = screen.getAllByRole("button", { name: "更换表情" });
    await user.click(swapButtons[0]);
    await screen.findByText("全渠道欢呼");
    await user.click(screen.getByRole("button", { name: "选用全渠道欢呼" }));

    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.url.endsWith("/api/v1/operations/videos/rem_demo_01/plan") && c.method === "PATCH"
        )
      ).toBe(true);
    });
    const patchCall = calls.find(
      (c) => c.url.endsWith("/api/v1/operations/videos/rem_demo_01/plan") && c.method === "PATCH"
    );
    expect(patchCall?.body).toMatchObject({
      scenes: [
        expect.objectContaining({
          scene_id: "s1",
          visual_props: expect.objectContaining({ meme_asset_id: "meme_public_1" }),
        }),
        expect.objectContaining({ scene_id: "s2" }),
      ],
    });
  });
});
