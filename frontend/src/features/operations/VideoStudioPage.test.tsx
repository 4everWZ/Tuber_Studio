import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
      editorial_fit: {
        recommended_channel: "video",
        fit_score: 0.91,
        video_mode: "project_showcase",
        duration_preset: "standard",
        reasoning: "当前选题包含源码与多镜演示，推荐以「project_showcase / standard」成片呈现。",
        dynamic_alternative: "可同步裁出 1 条轻量动态作为预告。",
        key_signals: ["已开启工坊公开文档取材", "包含代码拆解诉求"],
      },
      plan: {
        title: "一分钟看懂 Remotion 帧对齐",
        summary: "用音频真实帧数驱动 React 视频动画。",
        tags: ["Aqua", "Remotion"],
        video_mode: "project_showcase",
        duration_preset: "standard",
        fps: 30,
        width: 1920,
        height: 1080,
        include_avatar: false,
        scenes: [
          {
            scene_id: "s1",
            template: "TitleCard",
            heading: "音频驱动帧对齐",
            chapter_title: "开篇引子",
            theme: "cinema",
            camera_motion: "push_in",
            visual_props: { subtitle: "零漂移合成", bullets: ["精准时码"] },
            lines: [
              {
                line_id: "l1",
                text: "大家好，我是 Aqua！",
                avatar_cue: "cheerful",
                reveal_step: 1,
                visual_focus: "标题主卡片",
                sfx: "pop",
              },
            ],
            avatar_cue: "cheerful",
          },
          {
            scene_id: "s2",
            template: "CodeWalkthrough",
            heading: "核心代码拆解",
            chapter_title: "核心实现",
            theme: "dark_tech",
            camera_motion: "focus_target",
            visual_props: {
              language: "typescript",
              code_snippet: "const frames = Math.ceil(duration * fps);",
            },
            lines: [
              {
                line_id: "l2",
                text: "一行公式就能把音频时长映射到精准帧号。",
                avatar_cue: "curious",
                reveal_step: 2,
                visual_focus: "Math.ceil 公式",
                sfx: "chime",
              },
            ],
            avatar_cue: "curious",
          },
          {
            scene_id: "s3",
            template: "ChapterBumper",
            heading: "架构与界面总览",
            theme: "aqua_light",
            camera_motion: "pull_out",
            visual_props: { chapter_number: 2 },
            lines: [{ line_id: "l3", text: "接下来看工作台全貌。", avatar_cue: "calm", sfx: "chapter_hit" }],
          },
          {
            scene_id: "s4",
            template: "ArchitectureFlow",
            heading: "数据流管道",
            theme: "dark_tech",
            camera_motion: "focus_target",
            visual_props: {
              diagram_nodes: [
                { id: "n1", label: "脚本策划" },
                { id: "n2", label: "TTS 帧对齐" },
              ],
            },
            lines: [{ line_id: "l4", text: "从脚本到帧对齐一气呵成。", avatar_cue: "proud" }],
          },
          {
            scene_id: "s5",
            template: "UIShowcase",
            heading: "工坊界面特写",
            theme: "dark_tech",
            camera_motion: "focus_target",
            visual_props: {
              ui_focus_regions: [{ x: 40, y: 30, zoom: 1.5, label: "审阅面板" }],
            },
            lines: [{ line_id: "l5", text: "成片审阅一目了然。", avatar_cue: "cheerful" }],
          },
          {
            scene_id: "s6",
            template: "KineticStatement",
            heading: "一句话总结",
            theme: "cinema",
            camera_motion: "push_in",
            visual_props: { stat_callout: "100% 本地帧同步" },
            lines: [{ line_id: "l6", text: "本地工作坊也能做出高质感成片。", avatar_cue: "proud" }],
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
        contact_sheet_generated: true,
        scene_frames: 6,
        visual_review: {
          reviewed: true,
          overall_score: 88,
          monotony_risk: "low",
          layout_issues: [],
          suggestions: ["可在第 4 镜补充更明显的流向箭头高亮"],
        },
      },
      uploads: [
        {
          filename: "existing_shot.png",
          rel_path: "uploads/existing_shot.png",
          url: "/api/v1/operations/videos/rem_demo_01/uploads/existing_shot.png",
          size_bytes: 12000,
          mime_type: "image/png",
          kind: "image",
        },
      ],
      media: {
        video: "/api/v1/operations/videos/rem_demo_01/media/video",
        subtitles: "/api/v1/operations/videos/rem_demo_01/media/subtitles",
        subtitles_vtt: "/api/v1/operations/videos/rem_demo_01/media/subtitles_vtt",
        cover: "/api/v1/operations/videos/rem_demo_01/media/cover",
        contact_sheet: "/api/v1/operations/videos/rem_demo_01/media/contact_sheet",
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

  it("opens the linked job even when another job is first in the asynchronously loaded list", async () => {
    const first = SAMPLE_OVERVIEW.jobs[0];
    const target = { ...first, id: "rem_closeout", title: "当日运营复盘视频", plan: { ...first.plan, title: "当日运营复盘视频" } };
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ ...SAMPLE_OVERVIEW, jobs: [first, target] })
    );

    render(<VideoStudioPage route={parseHashRoute("#videos/rem_closeout")} />);

    expect(await screen.findByRole("heading", { name: "当日运营复盘视频" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: first.title })).not.toBeInTheDocument();
  });

  it("renders job queue, contact sheet & visual review, 10-template storyboard metadata, and Remotion code artifacts", async () => {
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
    expect(screen.getAllByText("开源项目 / 作品介绍").length).toBeGreaterThan(0);
    expect(screen.getByText("适合视频 (91%)")).toBeInTheDocument();
    expect(screen.getByText(/推荐以「project_showcase \/ standard」成片呈现/)).toBeInTheDocument();
    expect(screen.getByAltText("一分钟看懂 Remotion 帧对齐 关键帧审阅图")).toBeInTheDocument();
    expect(screen.getByText("视觉评分：88 / 100")).toBeInTheDocument();
    expect(screen.getByText("单调风险：低")).toBeInTheDocument();
    expect(screen.getByText("可在第 4 镜补充更明显的流向箭头高亮")).toBeInTheDocument();
    expect(screen.getByText("已抓取")).toBeInTheDocument();
    expect(screen.getByText("抓取回退")).toBeInTheDocument();
    expect(screen.getByText(/TimeoutError: timed out/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://github.com/aqua-org/studio" })).toHaveAttribute(
      "href",
      "https://github.com/aqua-org/studio"
    );

    await user.click(screen.getByRole("tab", { name: /分镜脚本/ }));
    expect(screen.getByText(/音频驱动帧对齐/)).toBeInTheDocument();
    expect(screen.getByText("章节：开篇引子")).toBeInTheDocument();
    expect(screen.getAllByText("电影过场").length).toBeGreaterThan(0);
    expect(screen.getAllByText("深海科技").length).toBeGreaterThan(0);
    expect(screen.getByText("浅海手账")).toBeInTheDocument();
    expect(screen.getByText("章节转场卡")).toBeInTheDocument();
    expect(screen.getByText("架构数据流图")).toBeInTheDocument();
    expect(screen.getByText("界面特写演示")).toBeInTheDocument();
    expect(screen.getByText("动态金句冲击")).toBeInTheDocument();
    expect(screen.getAllByText("运镜: 镜头推进").length).toBeGreaterThan(0);
    expect(screen.getAllByText("运镜: 焦点放大").length).toBeGreaterThan(0);
    expect(screen.getByText("渐进揭晓：第 1 步")).toBeInTheDocument();
    expect(screen.getByText("画面聚焦：标题主卡片")).toBeInTheDocument();
    expect(screen.getByText("音效：气泡弹出 (pop)")).toBeInTheDocument();
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

  it("binds the confirmed render and distinguishes a submission receipt from verified public visibility", async () => {
    const user = userEvent.setup();
    const contentHash = "a".repeat(64);
    const original = { ...SAMPLE_OVERVIEW.jobs[0], content_hash: contentHash };
    const publicUrl = "https://www.bilibili.com/video/av41002";
    const receipt = { aid: 41002 };
    const submitted = { ...original, status: "published", platform_state: "submitted", receipt };
    const confirmed = vi.spyOn(window, "confirm").mockReturnValue(true);
    const post = vi.fn();
    let checks = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/api/v1/operations/videos")) return jsonResponse({ ...SAMPLE_OVERVIEW, jobs: [original] });
      if (init?.method === "POST") post(url, init.body ? JSON.parse(String(init.body)) : undefined);
      if (url.endsWith("/publish")) return jsonResponse({ ok: true, action: "published", job: submitted });
      if (url.endsWith("/publish/recheck")) {
        checks += 1;
        return checks === 1 ? jsonResponse({
          ok: true, action: "readback", reason: "公开页面暂未返回此投稿",
          job: { ...submitted, platform_state: "unconfirmed", public_url: publicUrl },
        }) : jsonResponse({
          ok: true, action: "readback", job: { ...submitted, platform_state: "public_visible", public_url: publicUrl },
        });
      }
      return jsonResponse({ ok: true });
    });

    render(<VideoStudioPage route={parseHashRoute("#videos")} />);
    await user.click(await screen.findByRole("button", { name: "确认投稿到 B 站" }));
    expect(confirmed).toHaveBeenCalledOnce();
    expect(post).toHaveBeenCalledExactlyOnceWith("/api/v1/operations/videos/rem_demo_01/publish", {
      approved: true, publish_mode: "auto_publish", expected_content_hash: contentHash,
    });
    expect(await screen.findByText("待公开")).toBeInTheDocument();
    expect(screen.getByText("投稿编号 av41002")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "打开 B 站公开视频" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认投稿到 B 站" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "核对公开状态" }));
    expect(await screen.findByText("公开状态未知")).toBeInTheDocument();
    expect(screen.getByText("公开页面暂未返回此投稿")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "打开 B 站公开视频" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "核对公开状态" }));
    expect(await screen.findByText("公开可见已核实")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开 B 站公开视频" })).toHaveAttribute("href", publicUrl);
    expect(post.mock.calls.slice(1)).toEqual([
      ["/api/v1/operations/videos/rem_demo_01/publish/recheck", undefined],
      ["/api/v1/operations/videos/rem_demo_01/publish/recheck", undefined],
    ]);
  });

  it("offers readback for an unknown upload receipt without enabling resubmission", async () => {
    const user = userEvent.setup();
    const uncertain = { ...SAMPLE_OVERVIEW.jobs[0], status: "publish_uncertain", platform_state: "unconfirmed" };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/v1/operations/videos")) return jsonResponse({ ...SAMPLE_OVERVIEW, jobs: [uncertain] });
      return jsonResponse({ ok: false, action: "readback", reason: "尚无有效稿件编号", job: uncertain });
    });

    render(<VideoStudioPage route={parseHashRoute("#videos")} />);
    expect(await screen.findByText("公开状态未知")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认投稿到 B 站" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "核对公开状态" }));
    expect(await screen.findByText("尚无有效稿件编号")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toEqual([
      ["/api/v1/operations/videos/rem_demo_01/publish/recheck", expect.objectContaining({ method: "POST" })],
    ]);
  });

  it("supports 6 video modes, duration_preset auto-sync & override, 1-20 scenes, include_workspace_docs, 30000-char brief, and up to 6 URLs", async () => {
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
          editorial_fit: {
            recommended_channel: "video",
            fit_score: 0.94,
            video_mode: "daily_vlog",
            duration_preset: "deep_dive",
            reasoning: "多轮实验对照与 6 条参考来源，适合深度长片。",
            key_signals: ["已开启工坊公开文档取材"],
          },
        });
      }
      return jsonResponse({ ok: true });
    });

    render(<VideoStudioPage route={parseHashRoute("#videos")} />);
    await waitFor(() => {
      expect(screen.getByLabelText("视频选题")).toBeInTheDocument();
    });

    const modeSelect = screen.getByLabelText("视频类型") as HTMLSelectElement;
    expect(modeSelect.querySelectorAll("option").length).toBe(6);
    expect(screen.getByRole("option", { name: /日常实验 \/ LoRA 微调日志/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /看片串烧 \/ 近期盘点/ })).toBeInTheDocument();

    const durationSelect = screen.getByLabelText("时长档位") as HTMLSelectElement;
    const sceneSelect = screen.getByLabelText("分镜数量") as HTMLSelectElement;
    expect(sceneSelect.querySelectorAll("option").length).toBe(20);
    expect(screen.getByRole("option", { name: "20 镜" })).toBeInTheDocument();

    // Default commentary -> short -> 5 scenes
    expect(durationSelect.value).toBe("short");
    expect(sceneSelect.value).toBe("5");

    // Switching mode auto-updates duration_preset and max_scenes
    await user.selectOptions(modeSelect, "daily_vlog");
    expect(durationSelect.value).toBe("standard");
    expect(sceneSelect.value).toBe("9");

    // User overrides duration_preset -> syncs max_scenes unless overridden; mode change won't clobber override
    await user.selectOptions(durationSelect, "deep_dive");
    expect(sceneSelect.value).toBe("14");
    await user.selectOptions(modeSelect, "commentary");
    expect(durationSelect.value).toBe("deep_dive");
    await user.selectOptions(modeSelect, "daily_vlog");

    await user.selectOptions(sceneSelect, "16");
    await user.click(screen.getByLabelText("纳入本仓库公开文档与架构"));
    await user.type(screen.getByLabelText("视频选题"), "LoRA 微调第 3 轮对比实验");
    await user.type(screen.getByLabelText("参考资料或原始大纲"), "核心特性：本地离线工作坊");
    expect(screen.getByText("12 / 30000")).toBeInTheDocument();

    // Rejects 7 URLs, accepts 6 URLs
    const urlInput = screen.getByLabelText("公开参考链接");
    await user.type(
      urlInput,
      [
        "https://github.com/aqua-org/studio",
        "https://docs.example.org/guide",
        "https://example.org/3",
        "https://example.org/4",
        "https://example.org/5",
        "https://example.org/6",
        "https://example.org/7",
      ].join("\n")
    );
    await user.click(screen.getByRole("button", { name: "策划脚本" }));
    expect(screen.getByText("操作提示：公开参考链接最多填写 6 条。")).toBeInTheDocument();

    await user.clear(urlInput);
    await user.type(
      urlInput,
      [
        "https://github.com/aqua-org/studio",
        "https://docs.example.org/guide",
        "https://example.org/3",
        "https://example.org/4",
        "https://example.org/5",
        "https://example.org/6",
      ].join("\n")
    );
    await user.click(screen.getByRole("button", { name: "策划脚本" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/api/v1/operations/videos/plan") && c.method === "POST")).toBe(true);
    });
    const planCall = calls.find((c) => c.url.endsWith("/api/v1/operations/videos/plan"));
    expect(planCall?.body).toMatchObject({
      topic: "LoRA 微调第 3 轮对比实验",
      video_mode: "daily_vlog",
      duration_preset: "deep_dive",
      include_workspace_docs: true,
      brief_text: "核心特性：本地离线工作坊",
      reference_urls: [
        "https://github.com/aqua-org/studio",
        "https://docs.example.org/guide",
        "https://example.org/3",
        "https://example.org/4",
        "https://example.org/5",
        "https://example.org/6",
      ],
      max_scenes: 16,
    });
  }, 20_000);

  it("triggers script planning from the composer bar with default commentary short settings", async () => {
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
      video_mode: "commentary",
      duration_preset: "short",
      include_workspace_docs: false,
      fps: 30,
      include_avatar: true,
      bgm_enabled: true,
      max_scenes: 5,
    });
  });

  it("pitches ideas across channels and fills composer from a video idea", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method || "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/api/v1/operations/videos") && method === "GET") {
        return jsonResponse(SAMPLE_OVERVIEW);
      }
      if (url.endsWith("/api/v1/operations/videos/pitch-ideas") && method === "POST") {
        return jsonResponse({
          ok: true,
          channel_guidance: "评论适合短平快接话；动态适合单点日常；视频适合多镜对比与演示。",
          ideas: [
            {
              idea_id: "pitch_roundup",
              title: "Aqua 近期看片盘点",
              topic: "盘点近期看过的 Remotion 与 AI 工作流视频",
              channel: "video",
              video_mode: "watched_roundup",
              duration_preset: "standard",
              hook: "连刷几部技术向视频后发现了三个共同亮点！",
              visual_angle: "QuoteSpotlight + ChartCompare 评分对比",
              source_refs: ["BV1xx411c7mD", "workspace_docs"],
              why_this_channel: "多部视频具备对比脉络，适合串联盘点。",
            },
            {
              idea_id: "pitch_dynamic",
              title: "轻量动态冒泡",
              topic: "刚把测试跑通，先干一碗热白饭保鲜",
              channel: "dynamic",
              video_mode: "commentary",
              duration_preset: "short",
              hook: "一句话分享当下状态。",
              visual_angle: "单图动态卡片",
              source_refs: ["public_memory"],
              why_this_channel: "单点随想发动态更自然。",
            },
          ],
        });
      }
      return jsonResponse({ ok: true });
    });

    render(<VideoStudioPage route={parseHashRoute("#videos")} />);
    await waitFor(() => {
      expect(screen.getByLabelText("灵感方向提示")).toBeInTheDocument();
    });

    await user.type(screen.getByLabelText("灵感方向提示"), "看片盘点");
    await user.click(screen.getByRole("button", { name: "获取创作灵感" }));

    await screen.findByText("Aqua 近期看片盘点");
    expect(screen.getByText("轻量动态冒泡")).toBeInTheDocument();
    expect(screen.getByText("非视频优先（不阻塞手填）")).toBeInTheDocument();

    const applyButtons = screen.getAllByRole("button", { name: "填入制片面板" });
    expect(applyButtons).toHaveLength(1);
    await user.click(applyButtons[0]);

    expect((screen.getByLabelText("视频选题") as HTMLInputElement).value).toBe(
      "盘点近期看过的 Remotion 与 AI 工作流视频"
    );
    expect((screen.getByLabelText("视频类型") as HTMLSelectElement).value).toBe("watched_roundup");
    expect((screen.getByLabelText("时长档位") as HTMLSelectElement).value).toBe("standard");
    expect((screen.getByLabelText("分镜数量") as HTMLSelectElement).value).toBe("9");
    expect((screen.getByLabelText("纳入本仓库公开文档与架构") as HTMLInputElement).checked).toBe(true);
  });

  it("pins all_channels memes, uploads up to 12 images/MP4 clips, captures UI screenshots, binds clips to scenes, and runs VLM visual review", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    const overviewWithClip = {
      ...SAMPLE_OVERVIEW,
      jobs: [
        {
          ...SAMPLE_OVERVIEW.jobs[0],
          uploads: [
            ...SAMPLE_OVERVIEW.jobs[0].uploads,
            {
              filename: "demo_clip.mp4",
              rel_path: "uploads/demo_clip.mp4",
              url: "/api/v1/operations/videos/rem_demo_01/uploads/demo_clip.mp4",
              size_bytes: 2048,
              mime_type: "video/mp4",
              kind: "video",
            },
          ],
        },
      ],
    };
    let overviewCalls = 0;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method || "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/api/v1/operations/videos") && method === "GET") {
        overviewCalls += 1;
        return jsonResponse(overviewCalls >= 3 ? overviewWithClip : SAMPLE_OVERVIEW);
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
      if (url.endsWith("/api/v1/operations/videos/rem_demo_01/uploads") && method === "POST") {
        return jsonResponse({ ok: true });
      }
      if (url.endsWith("/api/v1/operations/videos/rem_demo_01/capture-ui") && method === "POST") {
        return jsonResponse({
          ok: true,
          captured: [{ route: "/", filename: "ui_01_home.png", rel_path: "uploads/ui_01_home.png" }],
        });
      }
      if (url.endsWith("/api/v1/operations/videos/rem_demo_01/visual-review") && method === "POST") {
        return jsonResponse({
          ok: true,
          reviewed: true,
          overall_score: 93,
          monotony_risk: "low",
          layout_issues: ["第 2 镜代码字号可再放大"],
          suggestions: ["保持当前节奏与主题交替"],
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

    // Composer upload supports up to 12 files including .mp4 clips (rejecting >30MB)
    const composerUploadInput = screen.getByLabelText("本次专属公开配图/截图") as HTMLInputElement;
    const oversizeMp4 = new File(["tiny"], "huge.mp4", { type: "video/mp4" });
    Object.defineProperty(oversizeMp4, "size", { value: 31 * 1024 * 1024 });
    fireEvent.change(composerUploadInput, { target: { files: [oversizeMp4] } });
    await screen.findByText(/不能超过 30 MB/);

    const batchFiles = [
      ...Array.from({ length: 11 }, (_, idx) => new File(["pngbytes"], `shot_${idx + 1}.png`, { type: "image/png" })),
      new File(["ftypmp4"], "clip.mp4", { type: "video/mp4" }),
    ];
    await user.upload(composerUploadInput, batchFiles);
    await screen.findByText("12 / 12");
    expect(screen.getByText("clip.mp4 (视频)")).toBeInTheDocument();

    await user.type(screen.getByLabelText("视频选题"), "带 12 份素材与置顶表情的演示");
    await user.click(screen.getByRole("button", { name: "策划脚本" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/api/v1/operations/videos/plan") && c.method === "POST")).toBe(true);
    });
    const planCall = calls.find((c) => c.url.endsWith("/api/v1/operations/videos/plan"));
    expect((planCall?.body as { uploaded_images: unknown[] }).uploaded_images).toHaveLength(12);
    expect(planCall?.body).toMatchObject({
      topic: "带 12 份素材与置顶表情的演示",
      pinned_meme_ids: ["meme_public_1"],
      uploaded_images: expect.arrayContaining([
        expect.objectContaining({ filename: "shot_1.png", mime_type: "image/png" }),
        expect.objectContaining({ filename: "clip.mp4", mime_type: "video/mp4" }),
      ]),
    });

    // Job inspector: capture-ui button + MP4 upload + bind video clip to scene + visual-review button
    await user.click(screen.getByRole("button", { name: "自拍工坊界面" }));
    await waitFor(() => {
      expect(
        calls.some((c) => c.url.endsWith("/api/v1/operations/videos/rem_demo_01/capture-ui") && c.method === "POST")
      ).toBe(true);
    });

    const jobUploadInput = screen.getByLabelText("上传公开参考图片") as HTMLInputElement;
    await user.upload(jobUploadInput, new File(["ftypmp4"], "demo_clip.mp4", { type: "video/mp4" }));
    await waitFor(() => {
      expect(
        calls.some((c) => c.url.endsWith("/api/v1/operations/videos/rem_demo_01/uploads") && c.method === "POST")
      ).toBe(true);
    });
    await screen.findByText("demo_clip.mp4");

    await user.selectOptions(screen.getByLabelText("绑定图片 demo_clip.mp4 到分镜"), "s5");
    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.url.endsWith("/api/v1/operations/videos/rem_demo_01/plan") &&
            c.method === "PATCH" &&
            JSON.stringify(c.body).includes("uploaded_video_file")
        )
      ).toBe(true);
    });

    await user.click(screen.getByRole("button", { name: "运行 VLM 视觉审阅" }));
    await screen.findByText("视觉评分：93 / 100");
    expect(screen.getByText("第 2 镜代码字号可再放大")).toBeInTheDocument();
    expect(screen.getByText("保持当前节奏与主题交替")).toBeInTheDocument();

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
            c.url.endsWith("/api/v1/operations/videos/rem_demo_01/plan") &&
            c.method === "PATCH" &&
            JSON.stringify(c.body).includes("meme_public_1")
        )
      ).toBe(true);
    });
  }, 20_000);
});
