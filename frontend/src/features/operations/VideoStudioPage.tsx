import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MemePickerModal, StatusPill, type MemeAsset, type StatusPillTone } from "@/shared";
import { studioPath } from "@/shared/api/client";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import { formatRelativeTime } from "@/shared/lib/formatters";
import { defaultMemePreviewUrl } from "@/shared/ui/MemePickerModal";
import type { ParsedRoute } from "@/shell/navigation";
import { VideoPitchIdeasCard } from "./VideoPitchIdeasCard";
import { VideoStoryboardTab } from "./VideoStoryboardTab";
import {
  CHANNEL_META,
  DEFAULT_DURATION_BY_MODE,
  DEFAULT_SCENES_BY_DURATION,
  DURATION_PRESET_LABELS,
  DURATION_PRESET_OPTIONS,
  FETCH_STATUS_META,
  MAX_BRIEF_LENGTH,
  MAX_REFERENCE_URLS,
  MAX_UPLOAD_FILES,
  MAX_VIDEO_UPLOAD_BYTES,
  STATUS_META,
  UPLOAD_ACCEPT_ATTR,
  VIDEO_MODE_LABELS,
  VIDEO_MODE_OPTIONS,
  formatBytes,
  inferUploadMimeType,
  isVideoMime,
  isVideoUploadItem,
  readMediaDataUrl,
  safeHttpsUrl,
  videoRequest,
  type CodeArtifactTab,
  type ComposerUploadItem,
  type DurationPreset,
  type InspectorTab,
  type VideoCandidate,
  type VideoEditorialFit,
  type VideoEnvironment,
  type VideoIdeaPitch,
  type VideoJobRecord,
  type VideoListResponse,
  type VideoMode,
  type VideoPolicy,
  type VideoPublicationResponse,
  type VideoUploadItem,
  type VideoVisualReview,
} from "./videoStudioTypes";
import styles from "./operations.module.css";

const ACCEPT_ALL_CHANNELS_MEMES = (asset: MemeAsset) =>
  (asset.usage_scope || "all_channels") === "all_channels";

const MONOTONY_RISK_META: Record<string, { label: string; tone: StatusPillTone }> = {
  low: { label: "单调风险：低", tone: "good" },
  medium: { label: "单调风险：中", tone: "amber" },
  high: { label: "单调风险：高", tone: "bad" },
  unknown: { label: "单调风险：未知", tone: "neutral" },
};

type MemePickerTarget =
  | { mode: "composer" }
  | { mode: "scene"; job: VideoJobRecord; sceneId: string; currentAssetId?: string | null };

export function VideoStudioPage({ route }: { route: ParsedRoute }) {
  const [jobs, setJobs] = useState<VideoJobRecord[]>([]);
  const [policy, setPolicy] = useState<VideoPolicy | null>(null);
  const [environment, setEnvironment] = useState<VideoEnvironment | null>(null);
  const [candidates, setCandidates] = useState<VideoCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  const [statusFilter, setStatusFilter] = useState("all");
  const [selectedJobId, setSelectedJobId] = useState<string | null>(route.routeKind || null);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("preview");
  const [codeTab, setCodeTab] = useState<CodeArtifactTab>("props");
  const [codeArtifacts, setCodeArtifacts] = useState<Record<string, string>>({});
  const [codeLoading, setCodeLoading] = useState(false);

  // Composer inputs
  const [topic, setTopic] = useState("");
  const [styleNote, setStyleNote] = useState("");
  const [videoMode, setVideoMode] = useState<VideoMode>("commentary");
  const [durationPreset, setDurationPreset] = useState<DurationPreset>("short");
  const [durationOverridden, setDurationOverridden] = useState(false);
  const [maxScenes, setMaxScenes] = useState<number>(DEFAULT_SCENES_BY_DURATION.short);
  const [maxScenesOverridden, setMaxScenesOverridden] = useState(false);
  const [includeWorkspaceDocs, setIncludeWorkspaceDocs] = useState(false);
  const [briefText, setBriefText] = useState("");
  const [referenceUrlsText, setReferenceUrlsText] = useState("");
  const [selectedBvids, setSelectedBvids] = useState<string[]>([]);
  const [fps, setFps] = useState<30 | 60>(30);
  const [resolution, setResolution] = useState<"1920x1080" | "1280x720">("1920x1080");
  const [ttsMode, setTtsMode] = useState<"omni_speech" | "dry_run">("omni_speech");
  const [includeAvatar, setIncludeAvatar] = useState(true);
  const [bgmEnabled, setBgmEnabled] = useState(true);
  const [pinnedMemes, setPinnedMemes] = useState<MemeAsset[]>([]);
  const [composerUploads, setComposerUploads] = useState<ComposerUploadItem[]>([]);
  const [memePickerTarget, setMemePickerTarget] = useState<MemePickerTarget | null>(null);

  // Per-job transient advisory / visual review results returned from POST endpoints
  const [editorialFitsByJobId, setEditorialFitsByJobId] = useState<Record<string, VideoEditorialFit>>({});
  const [visualReviewsByJobId, setVisualReviewsByJobId] = useState<Record<string, VideoVisualReview>>({});

  // Action busy flags
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState("");
  const [publicationReadback, setPublicationReadback] = useState<{ jobId: string; reason: string | null } | null>(null);

  // Policy draft
  const [policyDraft, setPolicyDraft] = useState<VideoPolicy | null>(null);

  // Metadata editor for selected job
  const [editTitle, setEditTitle] = useState("");
  const [editSummary, setEditSummary] = useState("");
  const [editTags, setEditTags] = useState("");
  const prevSelectedIdRef = useRef<string | null>(null);

  const fetchOverview = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await videoRequest<VideoListResponse>("/api/v1/operations/videos");
      setJobs(data.jobs || []);
      setPolicy(data.policy);
      setPolicyDraft((prev) => prev ?? data.policy);
      setEnvironment(data.environment || null);
      setCandidates(data.candidates || []);
      setLoadError("");
    } catch (err) {
      setLoadError(errorMessage(err));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchOverview(false);
  }, [fetchOverview]);

  useEffect(() => {
    if (route.routeKind) {
      setSelectedJobId(route.routeKind);
    }
  }, [route.routeKind]);

  const filteredJobs = useMemo(
    () => (statusFilter === "all" ? jobs : jobs.filter((job) => job.status === statusFilter)),
    [jobs, statusFilter]
  );

  useEffect(() => {
    if (loading || (selectedJobId && jobs.some((job) => job.id === selectedJobId))) return;
    if (filteredJobs.length > 0) {
      setSelectedJobId(filteredJobs[0].id);
    } else if (jobs.length > 0) {
      setSelectedJobId(jobs[0].id);
    } else {
      setSelectedJobId(null);
    }
  }, [jobs, filteredJobs, selectedJobId, loading]);

  const selectedJob = useMemo(
    () => jobs.find((job) => job.id === selectedJobId) || null,
    [jobs, selectedJobId]
  );

  // Sync metadata editor when switching jobs
  useEffect(() => {
    if (selectedJob && prevSelectedIdRef.current !== selectedJob.id) {
      prevSelectedIdRef.current = selectedJob.id;
      setEditTitle(selectedJob.plan?.title || selectedJob.title || "");
      setEditSummary(selectedJob.plan?.summary || "");
      setEditTags((selectedJob.plan?.tags || []).join(", "));
      setCodeArtifacts({});
      setActionError("");
    } else if (!selectedJob) {
      prevSelectedIdRef.current = null;
    }
  }, [selectedJob]);

  // Auto-poll while any job is rendering
  const hasActiveRender = useMemo(
    () => jobs.some((job) => job.status === "rendering" || job.render_active),
    [jobs]
  );

  useEffect(() => {
    if (!hasActiveRender) return;
    const timer = window.setInterval(() => {
      void fetchOverview(true);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [hasActiveRender, fetchOverview]);

  // Lazy-load code/props/manifest/subtitles when viewing Code tab
  useEffect(() => {
    if (inspectorTab !== "code" || !selectedJob) return;
    const mediaUrl = selectedJob.media?.[codeTab];
    const cacheKey = `${selectedJob.id}:${selectedJob.updated_at}:${codeTab}`;
    if (!mediaUrl || codeArtifacts[cacheKey] !== undefined) return;
    let active = true;
    setCodeLoading(true);
    videoRequest<string>(mediaUrl, { responseType: "text" })
      .then((text) => {
        if (!active) return;
        setCodeArtifacts((prev) => ({ ...prev, [cacheKey]: text }));
      })
      .catch((err) => {
        if (!active) return;
        setCodeArtifacts((prev) => ({ ...prev, [cacheKey]: `加载失败：${errorMessage(err)}` }));
      })
      .finally(() => {
        if (active) setCodeLoading(false);
      });
    return () => {
      active = false;
    };
  }, [inspectorTab, codeTab, selectedJob, codeArtifacts]);

  function handleVideoModeChange(nextMode: VideoMode) {
    setVideoMode(nextMode);
    if (!durationOverridden) {
      const nextDuration = DEFAULT_DURATION_BY_MODE[nextMode] || "standard";
      setDurationPreset(nextDuration);
      if (!maxScenesOverridden) {
        setMaxScenes(DEFAULT_SCENES_BY_DURATION[nextDuration]);
      }
    }
  }

  function handleDurationPresetChange(nextDuration: DurationPreset) {
    setDurationPreset(nextDuration);
    setDurationOverridden(true);
    if (!maxScenesOverridden) {
      setMaxScenes(DEFAULT_SCENES_BY_DURATION[nextDuration]);
    }
  }

  function handleMaxScenesChange(nextCount: number) {
    setMaxScenes(nextCount);
    setMaxScenesOverridden(true);
  }

  function handleApplyVideoIdea(idea: VideoIdeaPitch) {
    setTopic(idea.topic || idea.title);
    setVideoMode(idea.video_mode);
    setDurationPreset(idea.duration_preset);
    setDurationOverridden(true);
    setMaxScenes(DEFAULT_SCENES_BY_DURATION[idea.duration_preset] || 9);
    setMaxScenesOverridden(false);
    if (idea.visual_angle && !styleNote.trim()) {
      setStyleNote(idea.visual_angle.slice(0, 200));
    }
    const bvids = (idea.source_refs || []).filter((ref) => /^BV[0-9A-Za-z]{10}$/.test(ref));
    if (bvids.length > 0) {
      setSelectedBvids(bvids.slice(0, 6));
    }
    if ((idea.source_refs || []).includes("workspace_docs")) {
      setIncludeWorkspaceDocs(true);
    }
    setActionError("");
    emitNotice(`已将灵感「${idea.title}」填入制片面板。`);
  }

  function toggleCandidateBvid(bvid: string) {
    setSelectedBvids((prev) =>
      prev.includes(bvid) ? prev.filter((item) => item !== bvid) : prev.length < 6 ? [...prev, bvid] : prev
    );
  }

  async function handlePlan(runFullPipeline: boolean) {
    const trimmed = topic.trim();
    if (!trimmed) {
      setActionError("请先填写公开视频选题或热点说明。");
      return;
    }
    const parsedUrls = Array.from(
      new Set(
        referenceUrlsText
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean)
      )
    );
    if (parsedUrls.length > MAX_REFERENCE_URLS) {
      setActionError(`公开参考链接最多填写 ${MAX_REFERENCE_URLS} 条。`);
      return;
    }
    setBusyAction(runFullPipeline ? "full_pipeline" : "plan");
    setActionError("");
    try {
      const planned = await videoRequest<{
        ok: boolean;
        job_id: string;
        planner_source?: string;
        reference_diagnostics?: string[];
        editorial_fit?: VideoEditorialFit;
        job?: VideoJobRecord;
      }>("/api/v1/operations/videos/plan", {
        method: "POST",
        body: {
          topic: trimmed,
          video_mode: videoMode,
          duration_preset: durationPreset,
          include_workspace_docs: includeWorkspaceDocs,
          brief_text: briefText,
          reference_urls: parsedUrls,
          candidate_bvids: selectedBvids,
          style_note: styleNote.trim(),
          fps,
          include_avatar: includeAvatar,
          bgm_enabled: bgmEnabled,
          max_scenes: maxScenes,
          ...(pinnedMemes.length > 0 ? { pinned_meme_ids: pinnedMemes.map((m) => m.asset_id) } : {}),
          ...(composerUploads.length > 0
            ? {
                uploaded_images: composerUploads.map((item) => ({
                  filename: item.filename,
                  image_base64: item.image_base64,
                  mime_type: item.mime_type,
                })),
              }
            : {}),
        },
      });
      const jobId = planned.job_id;
      if (planned.editorial_fit) {
        setEditorialFitsByJobId((prev) => ({ ...prev, [jobId]: planned.editorial_fit! }));
      }
      if (resolution === "1280x720") {
        await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(jobId)}/plan`, {
          method: "PATCH",
          body: { width: 1280, height: 720 },
        });
      }
      setSelectedJobId(jobId);
      if (runFullPipeline) {
        await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(jobId)}/generate`, {
          method: "POST",
          body: { tts_mode: ttsMode },
        });
        await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(jobId)}/render`, {
          method: "POST",
          body: {
            composition_id: "AquaVideo",
            concurrency: policy?.max_render_concurrency ?? 2,
            dry_run: false,
            background: true,
          },
        });
        emitNotice("已策划脚本、生成配音与工程，并已提交后台 Remotion 渲染。");
      } else {
        const diagNote =
          planned.reference_diagnostics && planned.reference_diagnostics.length > 0
            ? "（部分外部链接抓取已降级保留原地址）"
            : "";
        emitNotice(
          (planned.planner_source === "llm"
            ? "模型已生成结构化视频分镜脚本。"
            : "已生成结构化分镜脚本（离线兜底模板）。") + diagNote
        );
      }
      setTopic("");
      setStyleNote("");
      setBriefText("");
      setReferenceUrlsText("");
      setSelectedBvids([]);
      setPinnedMemes([]);
      setComposerUploads([]);
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleComposerUploadFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const remaining = MAX_UPLOAD_FILES - composerUploads.length;
    if (remaining <= 0) {
      setActionError(`专属公开配图/截图或短视频最多添加 ${MAX_UPLOAD_FILES} 份。`);
      return;
    }
    const picked = Array.from(files).slice(0, remaining);
    const added: ComposerUploadItem[] = [];
    for (const file of picked) {
      const mimeType = inferUploadMimeType(file);
      if (!mimeType) {
        setActionError("仅支持上传 PNG、JPEG、WebP 图片或 MP4、WebM 短视频片段。");
        return;
      }
      if (isVideoMime(mimeType) && file.size > MAX_VIDEO_UPLOAD_BYTES) {
        setActionError("MP4 / WebM 短视频片段大小不能超过 30 MB。");
        return;
      }
      try {
        const { dataUrl, base64 } = await readMediaDataUrl(file);
        added.push({
          id: `${file.name}-${file.size}-${Math.random().toString(36).slice(2, 8)}`,
          filename: file.name,
          mime_type: mimeType,
          kind: isVideoMime(mimeType) ? "video" : "image",
          image_base64: base64,
          preview_url: dataUrl,
          size_bytes: file.size,
        });
      } catch (err) {
        setActionError(errorMessage(err));
        return;
      }
    }
    setActionError("");
    setComposerUploads((prev) => [...prev, ...added].slice(0, MAX_UPLOAD_FILES));
  }

  async function handleSetSceneMeme(job: VideoJobRecord, sceneId: string, memeAssetId: string | null) {
    const scenes = job.plan?.scenes;
    if (!scenes) return;
    setBusyAction("scene_meme");
    setActionError("");
    try {
      const updatedScenes = scenes.map((scene) =>
        scene.scene_id === sceneId
          ? {
              ...scene,
              visual_props: { ...(scene.visual_props || {}), meme_asset_id: memeAssetId },
            }
          : scene
      );
      await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(job.id)}/plan`, {
        method: "PATCH",
        body: { scenes: updatedScenes },
      });
      emitNotice(memeAssetId ? `已更新分镜表情：${memeAssetId}` : "已清除该分镜的表情配图。");
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleUploadFiles(job: VideoJobRecord, files: FileList | null) {
    if (!files || files.length === 0) return;
    const currentCount = job.uploads?.length || 0;
    const remaining = MAX_UPLOAD_FILES - currentCount;
    if (remaining <= 0) {
      setActionError(`每个视频任务最多上传 ${MAX_UPLOAD_FILES} 份公开参考媒体。`);
      return;
    }
    const picked = Array.from(files).slice(0, remaining);
    for (const file of picked) {
      const mimeType = inferUploadMimeType(file);
      if (!mimeType) {
        setActionError("仅支持上传 PNG、JPEG、WebP 图片或 MP4、WebM 短视频片段。");
        return;
      }
      if (isVideoMime(mimeType) && file.size > MAX_VIDEO_UPLOAD_BYTES) {
        setActionError("MP4 / WebM 短视频片段大小不能超过 30 MB。");
        return;
      }
    }
    setBusyAction("upload");
    setActionError("");
    try {
      for (const file of picked) {
        const mimeType = inferUploadMimeType(file)!;
        const { base64 } = await readMediaDataUrl(file);
        await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(job.id)}/uploads`, {
          method: "POST",
          body: {
            filename: file.name,
            image_base64: base64,
            mime_type: mimeType,
          },
        });
      }
      emitNotice(
        picked.length === 1
          ? `已上传公开参考媒体：${picked[0].name}`
          : `已上传 ${picked.length} 份公开参考媒体。`
      );
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleCaptureUi(job: VideoJobRecord) {
    setBusyAction("capture_ui");
    setActionError("");
    try {
      const res = await videoRequest<{
        ok: boolean;
        error?: string | null;
        captured?: Array<{ route?: string; filename: string; rel_path: string }>;
      }>(`/api/v1/operations/videos/${encodeURIComponent(job.id)}/capture-ui`, {
        method: "POST",
        body: {},
      });
      if (!res.ok) {
        const msg = res.error || "自拍工坊界面失败";
        setActionError(msg);
        emitNotice(msg, true);
      } else {
        const count = res.captured?.length ?? 0;
        emitNotice(`已截取 ${count} 张工坊界面截图并写入参考媒体。`);
      }
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleVisualReview(job: VideoJobRecord) {
    setBusyAction("visual_review");
    setActionError("");
    try {
      const res = await videoRequest<VideoVisualReview>(
        `/api/v1/operations/videos/${encodeURIComponent(job.id)}/visual-review`,
        { method: "POST" }
      );
      setVisualReviewsByJobId((prev) => ({ ...prev, [job.id]: res }));
      if (res.reviewed) {
        emitNotice(`VLM 视觉审阅完成：综合评分 ${res.overall_score} / 100。`);
      } else {
        emitNotice("已执行静态视觉检查（VLM 未启用或未生成缩略图）。");
      }
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleBindUploadToScene(
    job: VideoJobRecord,
    item: VideoUploadItem,
    targetSceneId: string
  ) {
    const scenes = job.plan?.scenes;
    if (!scenes) return;
    const isVideo = isVideoUploadItem(item);
    const fieldKey = isVideo ? "uploaded_video_file" : "uploaded_image_file";
    setBusyAction("bind_upload");
    setActionError("");
    try {
      const updatedScenes = scenes.map((scene) => {
        const prevProps = scene.visual_props || {};
        if (scene.scene_id === targetSceneId) {
          return {
            ...scene,
            visual_props: { ...prevProps, [fieldKey]: item.rel_path },
          };
        }
        if (prevProps[fieldKey] === item.rel_path) {
          return {
            ...scene,
            visual_props: { ...prevProps, [fieldKey]: null },
          };
        }
        return scene;
      });
      await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(job.id)}/plan`, {
        method: "PATCH",
        body: { scenes: updatedScenes },
      });
      emitNotice(
        targetSceneId
          ? `已将${isVideo ? "视频片段" : "图片"}绑定到指定分镜。`
          : `已解除该${isVideo ? "视频片段" : "图片"}的分镜绑定。`
      );
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleGenerate(job: VideoJobRecord, overrideTts?: "omni_speech" | "dry_run") {
    setBusyAction("generate");
    setActionError("");
    try {
      await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(job.id)}/generate`, {
        method: "POST",
        body: { tts_mode: overrideTts ?? ttsMode },
      });
      emitNotice("配音 WAV、字幕与 Remotion 工程已生成。");
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleRender(job: VideoJobRecord, dryRun = false) {
    setBusyAction("render");
    setActionError("");
    try {
      await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(job.id)}/render`, {
        method: "POST",
        body: {
          composition_id: "AquaVideo",
          concurrency: policy?.max_render_concurrency ?? 2,
          dry_run: dryRun,
          background: true,
        },
      });
      emitNotice(dryRun ? "已提交干跑渲染校验。" : "已启动 Remotion 渲染，完成后将自动执行 FFprobe 质检。");
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleSaveMetadata(job: VideoJobRecord) {
    const tags = editTags
      .split(/[,，\n]/)
      .map((s) => s.trim())
      .filter(Boolean)
      .slice(0, 10);
    if (!editTitle.trim() || !editSummary.trim() || tags.length === 0) {
      setActionError("标题、简介和至少一个标签不能为空。");
      return;
    }
    setBusyAction("save_meta");
    setActionError("");
    try {
      await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(job.id)}/plan`, {
        method: "PATCH",
        body: {
          title: editTitle.trim(),
          summary: editSummary.trim(),
          tags,
        },
      });
      emitNotice("视频标题、简介与标签已更新。");
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleStageOrPublish(job: VideoJobRecord, approved: boolean) {
    if (
      approved &&
      (!job.content_hash || !window.confirm(`确认将成片「${job.title}」投稿到当前 B 站账号？`))
    ) {
      return;
    }
    setBusyAction(approved ? "publish" : "stage");
    setActionError("");
    try {
      const res = await videoRequest<VideoPublicationResponse>(`/api/v1/operations/videos/${encodeURIComponent(job.id)}/publish`, {
        method: "POST",
        body: {
          publish_mode: approved ? "auto_publish" : "preview_review",
          approved,
          expected_content_hash: approved ? job.content_hash : undefined,
        },
      });
      if (res.action === "published" || res.action === "already_published") {
        emitNotice("平台已收稿。可在成片预览中核对公开状态。");
      } else if (res.action === "staged_for_review") {
        emitNotice("已保持在人工审阅队列（未触发平台投稿）。");
      } else if (!res.ok) {
        const msg = res.reason || res.error || "投稿已被安全闸门拦截。";
        setActionError(msg);
        emitNotice(msg, true);
      } else {
        emitNotice(`操作完成：${res.action || res.status || "ok"}`);
      }
      await applyPublicationJob(res.job);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function applyPublicationJob(job: VideoJobRecord | undefined) {
    if (job) {
      setJobs((current) => current.map((item) => item.id === job.id ? job : item));
    } else {
      await fetchOverview(true);
    }
  }

  async function handleRecheckPublication(job: VideoJobRecord) {
    setBusyAction("recheck_publish");
    setActionError("");
    setPublicationReadback(null);
    try {
      const res = await videoRequest<VideoPublicationResponse>(
        `/api/v1/operations/videos/${encodeURIComponent(job.id)}/publish/recheck`, { method: "POST" },
      );
      await applyPublicationJob(res.job);
      setPublicationReadback({ jobId: job.id, reason: res.reason || null });
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleDelete(job: VideoJobRecord) {
    if (!window.confirm(`确认删除视频任务「${job.title}」及其本地渲染文件？`)) return;
    setBusyAction("delete");
    setActionError("");
    try {
      await videoRequest(`/api/v1/operations/videos/${encodeURIComponent(job.id)}`, {
        method: "DELETE",
      });
      emitNotice("已删除视频任务。");
      setSelectedJobId(null);
      await fetchOverview(true);
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleSavePolicy() {
    if (!policyDraft) return;
    setBusyAction("policy");
    setActionError("");
    try {
      const res = await videoRequest<{ policy: VideoPolicy }>("/api/v1/operations/videos/policy", {
        method: "PUT",
        body: policyDraft,
      });
      setPolicy(res.policy);
      setPolicyDraft(res.policy);
      emitNotice("视频制片策略已保存。");
    } catch (err) {
      const msg = errorMessage(err);
      setActionError(msg);
      emitNotice(msg, true);
    } finally {
      setBusyAction(null);
    }
  }

  const envChecks: Array<{ key: string; label: string; ready: boolean; detail?: string }> = [
    { key: "remotion", label: "Remotion Studio", ready: Boolean(environment?.remotion_studio?.ready) },
    { key: "node", label: "Node / npx", ready: Boolean(environment?.node?.ready && environment?.npx?.ready) },
    { key: "ffmpeg", label: "FFmpeg / FFprobe", ready: Boolean(environment?.ffmpeg?.ready && environment?.ffprobe?.ready) },
    {
      key: "browser",
      label: "Chromium",
      ready: Boolean(environment?.browser?.ready),
      detail: environment?.browser?.kind || undefined,
    },
    { key: "llm", label: "Omni LLM", ready: Boolean(environment?.omni_llm?.ready) },
    { key: "tts", label: "Omni TTS", ready: Boolean(environment?.omni_tts?.ready) },
    {
      key: "biliup",
      label: "B 站投稿",
      ready: Boolean(environment?.biliup?.ready),
      detail:
        environment?.biliup?.mode === "web_api"
          ? "内置 Web"
          : environment?.biliup?.mode === "biliup_cli"
            ? "biliup CLI"
            : environment?.biliup?.ready
              ? undefined
              : "需登录 B 站",
    },
  ];

  const activeModeOption =
    VIDEO_MODE_OPTIONS.find((item) => item.value === videoMode) || VIDEO_MODE_OPTIONS[0];
  const selectedStatusMeta = selectedJob
    ? STATUS_META[selectedJob.status] || { label: selectedJob.status, tone: "neutral" as StatusPillTone }
    : null;
  const hasPublication = selectedJob && (
    ["publishing", "published", "publish_uncertain"].includes(selectedJob.status) || Boolean(selectedJob.receipt)
  );
  const isPubliclyVisible = selectedJob?.platform_state === "public_visible";
  const publicVideoUrl = isPubliclyVisible ? safeHttpsUrl(selectedJob?.public_url) : null;
  const publicationLabel = isPubliclyVisible ? "公开可见已核实" :
    selectedJob?.status === "published" && (!selectedJob.platform_state || selectedJob.platform_state === "submitted")
      ? "待公开" : "公开状态未知";
  const selectedEditorialFit = selectedJob
    ? selectedJob.editorial_fit || editorialFitsByJobId[selectedJob.id] || null
    : null;
  const selectedVisualReview = selectedJob
    ? visualReviewsByJobId[selectedJob.id] || selectedJob.qc_report?.visual_review || null
    : null;
  const activeCodeCacheKey = selectedJob
    ? `${selectedJob.id}:${selectedJob.updated_at}:${codeTab}`
    : "";

  return (
    <section id="videos-page" aria-label="视频创作工坊">
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-heading">
          <div>
            <h2>Code-as-Video 视频创作</h2>
            <p className="hint" style={{ margin: "4px 0 0" }}>
              基于公开选题与 Remotion 模板生成分镜、参考音色配音、1080p/720p 成片与 FFprobe 质检，默认进入人工预览审阅队列。
            </p>
          </div>
          <div className="actions">
            <StatusPill
              tone={policy?.publish_mode === "auto_publish" ? "amber" : "good"}
              label={policy?.publish_mode === "auto_publish" ? "自动投稿策略" : "人工预览审阅（安全默认）"}
            />
            <a href="#cache-panel" className="win-link-btn">
              媒体与资产缓存 →
            </a>
            <button
              type="button"
              disabled={loading || busyAction !== null}
              onClick={() => void fetchOverview(false)}
            >
              刷新
            </button>
          </div>
        </div>

        <VideoPitchIdeasCard
          disabled={busyAction !== null}
          onApplyVideoIdea={handleApplyVideoIdea}
        />

        <div style={{ display: "grid", gap: 10, marginTop: 12 }}>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr", gap: 10 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span className="hint">公开视频选题 / 观察话题</span>
              <input
                type="text"
                aria-label="视频选题"
                placeholder="例如：一分钟看懂 Remotion 音频驱动帧对齐原理"
                value={topic}
                maxLength={400}
                onChange={(e) => setTopic(e.target.value)}
              />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span className="hint">视频类型</span>
              <select
                aria-label="视频类型"
                value={videoMode}
                onChange={(e) => handleVideoModeChange(e.target.value as VideoMode)}
              >
                {VIDEO_MODE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label} ({opt.value})
                  </option>
                ))}
              </select>
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span className="hint">时长档位</span>
              <select
                aria-label="时长档位"
                value={durationPreset}
                onChange={(e) => handleDurationPresetChange(e.target.value as DurationPreset)}
              >
                {DURATION_PRESET_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span className="hint">风格 / 节奏提示（可选）</span>
              <input
                type="text"
                aria-label="风格提示"
                placeholder="例如：轻松吐槽、带代码示例"
                value={styleNote}
                maxLength={200}
                onChange={(e) => setStyleNote(e.target.value)}
              />
            </label>
          </div>

          <p className="hint" style={{ margin: 0 }}>
            当前类型说明（{activeModeOption.label}）：{activeModeOption.hint}
          </p>

          <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 10 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span className="hint">参考资料 / 原始大纲（可选）</span>
                <span className="hint">
                  {briefText.length} / {MAX_BRIEF_LENGTH}
                </span>
              </div>
              <textarea
                rows={3}
                aria-label="参考资料或原始大纲"
                placeholder={`粘贴公开产品介绍、功能亮点、讲稿大纲或自述要点（最多 ${MAX_BRIEF_LENGTH} 字）`}
                value={briefText}
                maxLength={MAX_BRIEF_LENGTH}
                onChange={(e) => setBriefText(e.target.value)}
              />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span className="hint">
                公开参考链接（可选，每行一个，最多 {MAX_REFERENCE_URLS} 条，支持 GitHub 仓库深度抓取）
              </span>
              <textarea
                rows={3}
                aria-label="公开参考链接"
                placeholder={"https://github.com/owner/repo\nhttps://docs.example.org/guide"}
                value={referenceUrlsText}
                onChange={(e) => setReferenceUrlsText(e.target.value)}
              />
            </label>
          </div>

          {candidates.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
              <span className="hint">关联已看公开视频：</span>
              {candidates.slice(0, 6).map((cand) => {
                const picked = selectedBvids.includes(cand.bvid);
                return (
                  <button
                    key={cand.bvid}
                    type="button"
                    className={picked ? styles.pillActive : undefined}
                    onClick={() => toggleCandidateBvid(cand.bvid)}
                    title={cand.title || cand.bvid}
                  >
                    {picked ? "✓ " : "+ "}
                    {(cand.title || cand.bvid).slice(0, 22)}
                  </button>
                );
              })}
            </div>
          )}

          <div style={{ display: "grid", gap: 8 }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
              <button
                type="button"
                data-pin-meme-btn=""
                disabled={busyAction !== null || pinnedMemes.length >= 3}
                onClick={() => setMemePickerTarget({ mode: "composer" })}
              >
                {`置顶图库表情（0–3 张）${pinnedMemes.length > 0 ? ` (${pinnedMemes.length}/3)` : ""}`}
              </button>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 8, marginTop: 0 }}>
                <span className="hint">本次专属公开配图/截图或短视频（0–12 份，≤30MB）</span>
                <input
                  type="file"
                  multiple
                  accept={UPLOAD_ACCEPT_ATTR}
                  aria-label="本次专属公开配图/截图"
                  disabled={busyAction !== null || composerUploads.length >= MAX_UPLOAD_FILES}
                  onChange={(e) => {
                    const files = e.target.files;
                    void handleComposerUploadFiles(files);
                    e.target.value = "";
                  }}
                />
                <span className="hint">
                  {composerUploads.length} / {MAX_UPLOAD_FILES}
                </span>
              </label>
            </div>

            {pinnedMemes.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }} data-pinned-memes="">
                {pinnedMemes.map((meme) => (
                  <span key={meme.asset_id} className={styles.claimChip}>
                    <img
                      src={defaultMemePreviewUrl(meme)}
                      alt={meme.title || meme.asset_id}
                      style={{ width: 24, height: 24, objectFit: "cover", borderRadius: 4 }}
                    />
                    <span>{meme.title || meme.asset_id}</span>
                    <button
                      type="button"
                      aria-label={`移除置顶表情 ${meme.title || meme.asset_id}`}
                      onClick={() =>
                        setPinnedMemes((prev) => prev.filter((item) => item.asset_id !== meme.asset_id))
                      }
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}

            {composerUploads.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }} data-composer-uploads="">
                {composerUploads.map((item) => (
                  <span key={item.id} className={styles.claimChip}>
                    {item.kind === "video" ? (
                      <video
                        src={item.preview_url}
                        muted
                        playsInline
                        style={{ width: 32, height: 24, objectFit: "cover", borderRadius: 4 }}
                      />
                    ) : (
                      <img
                        src={item.preview_url}
                        alt={item.filename}
                        style={{ width: 24, height: 24, objectFit: "cover", borderRadius: 4 }}
                      />
                    )}
                    <span>
                      {item.filename}
                      {item.kind === "video" ? " (视频)" : ""}
                    </span>
                    <button
                      type="button"
                      aria-label={`移除配图 ${item.filename}`}
                      onClick={() =>
                        setComposerUploads((prev) => prev.filter((existing) => existing.id !== item.id))
                      }
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span className="hint">分辨率</span>
              <select
                aria-label="视频分辨率"
                value={resolution}
                onChange={(e) => setResolution(e.target.value as "1920x1080" | "1280x720")}
              >
                <option value="1920x1080">1920×1080</option>
                <option value="1280x720">1280×720</option>
              </select>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span className="hint">帧率</span>
              <select
                aria-label="视频帧率"
                value={fps}
                onChange={(e) => setFps(Number(e.target.value) as 30 | 60)}
              >
                <option value={30}>30 fps</option>
                <option value={60}>60 fps</option>
              </select>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span className="hint">分镜数</span>
              <select
                aria-label="分镜数量"
                value={maxScenes}
                onChange={(e) => handleMaxScenesChange(Number(e.target.value))}
              >
                {Array.from({ length: 20 }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>
                    {n} 镜
                  </option>
                ))}
              </select>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span className="hint">配音</span>
              <select
                aria-label="配音模式"
                value={ttsMode}
                onChange={(e) => setTtsMode(e.target.value as "omni_speech" | "dry_run")}
              >
                <option value="omni_speech">Omni 参考音色</option>
                <option value="dry_run">离线测试音轨 (dry_run)</option>
              </select>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <input
                type="checkbox"
                checked={includeWorkspaceDocs}
                onChange={(e) => setIncludeWorkspaceDocs(e.target.checked)}
              />
              <span className="hint">纳入本仓库公开文档与架构</span>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <input
                type="checkbox"
                checked={includeAvatar}
                onChange={(e) => setIncludeAvatar(e.target.checked)}
              />
              <span className="hint">Aqua 右下角演出小窗</span>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <input
                type="checkbox"
                checked={bgmEnabled}
                onChange={(e) => setBgmEnabled(e.target.checked)}
              />
              <span className="hint">浅海 BGM 与转场音效</span>
            </label>

            <div className="actions" style={{ marginLeft: "auto" }}>
              <button
                type="button"
                disabled={busyAction !== null}
                onClick={() => void handlePlan(false)}
              >
                {busyAction === "plan" ? "策划中…" : "策划脚本"}
              </button>
              <button
                type="button"
                className="primary"
                disabled={busyAction !== null}
                onClick={() => void handlePlan(true)}
              >
                {busyAction === "full_pipeline" ? "制片提交中…" : "一键制片（策划 + 配音 + 渲染）"}
              </button>
            </div>
          </div>
        </div>

        <details style={{ marginTop: 12 }}>
          <summary className="hint" style={{ cursor: "pointer" }}>
            环境就绪度与制片策略（Remotion / FFmpeg / Chromium / 发布闸门）
          </summary>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10 }}>
            {envChecks.map((item) => (
              <StatusPill
                key={item.key}
                tone={item.ready ? "good" : "amber"}
                label={`${item.ready ? "✓" : "○"} ${item.label}${item.detail ? ` (${item.detail})` : ""}`}
              />
            ))}
          </div>
          {policyDraft && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", marginTop: 12 }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span className="hint">发布模式</span>
                <select
                  aria-label="发布策略模式"
                  value={policyDraft.publish_mode}
                  onChange={(e) =>
                    setPolicyDraft({
                      ...policyDraft,
                      publish_mode: e.target.value as "preview_review" | "auto_publish",
                    })
                  }
                >
                  <option value="preview_review">人工预览审阅 (preview_review)</option>
                  <option value="auto_publish">受控自动投稿 (auto_publish)</option>
                </select>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={policyDraft.auto_publish_enabled}
                  onChange={(e) =>
                    setPolicyDraft({ ...policyDraft, auto_publish_enabled: e.target.checked })
                  }
                />
                <span className="hint">允许自动投稿开关</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span className="hint">渲染并发</span>
                <input
                  type="number"
                  min={1}
                  max={4}
                  style={{ width: 64 }}
                  value={policyDraft.max_render_concurrency}
                  onChange={(e) =>
                    setPolicyDraft({
                      ...policyDraft,
                      max_render_concurrency: Math.max(1, Math.min(4, Number(e.target.value) || 1)),
                    })
                  }
                />
              </label>
              <button
                type="button"
                disabled={busyAction !== null}
                onClick={() => void handleSavePolicy()}
              >
                保存策略
              </button>
            </div>
          )}
        </details>

        {loadError && (
          <p className="hint" role="alert" style={{ color: "var(--bad, #dc2626)", marginTop: 8 }}>
            加载失败：{loadError}
          </p>
        )}
        {actionError && (
          <p className="hint" role="alert" style={{ color: "var(--bad, #dc2626)", marginTop: 8 }}>
            操作提示：{actionError}
          </p>
        )}
      </div>

      <div className={styles.splitWorkspace}>
        <div className={styles.masterPane}>
          <div className={styles.segmentedControl} role="tablist" aria-label="视频任务状态筛选">
            {[
              { key: "all", label: `全部 (${jobs.length})` },
              { key: "script_planned", label: "已策划" },
              { key: "project_ready", label: "待渲染" },
              { key: "rendering", label: "渲染中" },
              { key: "ready_for_review", label: "待审阅" },
              { key: "qc_failed", label: "质检未过" },
              { key: "published", label: "已投稿" },
            ].map((opt) => (
              <button
                key={opt.key}
                type="button"
                className={statusFilter === opt.key ? styles.pillActive : undefined}
                onClick={() => setStatusFilter(opt.key)}
              >
                {opt.label}
              </button>
            ))}
          </div>

          <div className={styles.masterList}>
            {loading && jobs.length === 0 ? (
              <p className="hint">正在加载视频制作任务…</p>
            ) : filteredJobs.length === 0 ? (
              <div>
                <h4>暂无视频创作任务</h4>
                <p className="hint">在上方输入公开话题并点击「策划脚本」或「一键制片」开始。</p>
              </div>
            ) : (
              filteredJobs.map((job) => {
                const meta = STATUS_META[job.status] || { label: job.status, tone: "neutral" as StatusPillTone };
                const isSelected = job.id === selectedJob?.id;
                return (
                  <div
                    key={job.id}
                    role="button"
                    tabIndex={0}
                    data-job-id={job.id}
                    className={`${styles.candidateCard} ${isSelected ? styles.candidateCardSelected : ""}`.trim()}
                    onClick={() => setSelectedJobId(job.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelectedJobId(job.id);
                      }
                    }}
                  >
                    <div className="panel-heading">
                      <StatusPill tone={meta.tone} label={meta.label} />
                      <span className="hint">{formatRelativeTime(job.updated_at)}</span>
                    </div>
                    <div style={{ margin: "6px 0", fontWeight: 600 }}>{job.title || job.id}</div>
                    <div className="hint">
                      {job.plan?.video_mode ? `${VIDEO_MODE_LABELS[job.plan.video_mode] || job.plan.video_mode} · ` : ""}
                      {job.plan?.duration_preset
                        ? `${DURATION_PRESET_LABELS[job.plan.duration_preset] || job.plan.duration_preset} · `
                        : ""}
                      {job.plan?.scenes?.length || 0} 镜
                      {job.manifest?.total_duration_seconds
                        ? ` · ${job.manifest.total_duration_seconds.toFixed(1)}s`
                        : ""}
                      {job.planner_source ? ` · ${job.planner_source === "llm" ? "模型策划" : job.planner_source}` : ""}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        <div className={styles.detailPane}>
          {!selectedJob ? (
            <div>
              <h3>选择一个视频任务</h3>
              <p className="hint">查看分镜脚本、预览 Remotion 渲染成片、检查 FFprobe 质检并控制发布。</p>
            </div>
          ) : (
            <div>
              <div className="panel-heading" style={{ marginBottom: 10 }}>
                <div className="actions">
                  {selectedStatusMeta && (
                    <StatusPill tone={selectedStatusMeta.tone} label={selectedStatusMeta.label} />
                  )}
                  {selectedJob.plan?.video_mode && (
                    <StatusPill
                      tone="blue"
                      label={VIDEO_MODE_LABELS[selectedJob.plan.video_mode] || selectedJob.plan.video_mode}
                    />
                  )}
                  {selectedJob.plan?.duration_preset && (
                    <StatusPill
                      tone="neutral"
                      label={
                        DURATION_PRESET_LABELS[selectedJob.plan.duration_preset] ||
                        selectedJob.plan.duration_preset
                      }
                    />
                  )}
                  {selectedJob.planner_source && (
                    <StatusPill
                      tone={selectedJob.planner_source === "llm" ? "good" : "neutral"}
                      label={
                        selectedJob.planner_source === "llm"
                          ? "LLM 策划"
                          : selectedJob.planner_source === "fallback"
                            ? "兜底模板"
                            : selectedJob.planner_source
                      }
                    />
                  )}
                  <span className="hint">{selectedJob.id}</span>
                </div>
                <div className="actions">
                  <button
                    type="button"
                    disabled={busyAction !== null || selectedJob.status === "rendering" || Boolean(hasPublication)}
                    onClick={() => void handleGenerate(selectedJob)}
                  >
                    {busyAction === "generate" ? "生成中…" : "生成工程与配音"}
                  </button>
                  <button
                    type="button"
                    className="primary"
                    disabled={
                      busyAction !== null ||
                      !selectedJob.manifest ||
                      selectedJob.status === "rendering" ||
                      Boolean(hasPublication)
                    }
                    onClick={() => void handleRender(selectedJob, false)}
                  >
                    {selectedJob.status === "rendering" ? "渲染中…" : "渲染成片"}
                  </button>
                </div>
              </div>

              <h3 style={{ margin: "0 0 8px" }}>{selectedJob.title}</h3>

              {selectedEditorialFit && (
                <div className="panel" style={{ marginBottom: 10 }} data-editorial-fit="">
                  <div className="panel-heading">
                    <div className="actions">
                      <StatusPill
                        tone={CHANNEL_META[selectedEditorialFit.recommended_channel]?.tone || "neutral"}
                        label={`${CHANNEL_META[selectedEditorialFit.recommended_channel]?.label || selectedEditorialFit.recommended_channel} (${Math.round(selectedEditorialFit.fit_score * 100)}%)`}
                      />
                      <strong>渠道与体裁评估建议</strong>
                    </div>
                    <span className="hint">
                      建议模式：{VIDEO_MODE_LABELS[selectedEditorialFit.video_mode] || selectedEditorialFit.video_mode} ·{" "}
                      {DURATION_PRESET_LABELS[selectedEditorialFit.duration_preset] ||
                        selectedEditorialFit.duration_preset}
                    </span>
                  </div>
                  <p className="hint" style={{ margin: "6px 0 0" }}>
                    {selectedEditorialFit.reasoning}
                  </p>
                  {selectedEditorialFit.dynamic_alternative && (
                    <p className="hint" style={{ margin: "4px 0 0" }}>
                      备选建议：{selectedEditorialFit.dynamic_alternative}
                    </p>
                  )}
                  {selectedEditorialFit.key_signals && selectedEditorialFit.key_signals.length > 0 && (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 6 }}>
                      {selectedEditorialFit.key_signals.map((sig, idx) => (
                        <span key={idx} className={styles.claimChip}>
                          {sig}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {selectedJob.error && (
                <div className="panel" style={{ marginBottom: 10, borderColor: "var(--bad, #dc2626)" }}>
                  <strong>程序状态 / 错误：</strong>
                  <span>{selectedJob.error}</span>
                </div>
              )}
              {selectedJob.planner_error && (
                <p className="hint" style={{ margin: "0 0 8px" }}>
                  策划回退原因：{selectedJob.planner_error}
                </p>
              )}

              <div className={styles.segmentedControl} role="tablist" aria-label="任务详情视图" style={{ marginBottom: 12 }}>
                {(
                  [
                    ["preview", "成片预览与发布"],
                    ["script", `分镜脚本 (${selectedJob.plan?.scenes?.length || 0})`],
                    ["code", "Remotion 代码与 Props"],
                  ] as const
                ).map(([tabKey, label]) => (
                  <button
                    key={tabKey}
                    type="button"
                    role="tab"
                    aria-selected={inspectorTab === tabKey}
                    className={inspectorTab === tabKey ? styles.navBtnActive : undefined}
                    onClick={() => setInspectorTab(tabKey)}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {inspectorTab === "preview" && (
                <div style={{ display: "grid", gap: 14 }}>
                  {selectedJob.media?.video ? (
                    <div>
                      <video
                        key={`${selectedJob.id}-${selectedJob.updated_at}`}
                        controls
                        playsInline
                        preload="metadata"
                        poster={selectedJob.media.cover ? studioPath(selectedJob.media.cover) : undefined}
                        style={{
                          width: "100%",
                          maxHeight: 420,
                          borderRadius: 12,
                          backgroundColor: "#060d1f",
                        }}
                        src={studioPath(selectedJob.media.video)}
                      >
                        {selectedJob.media.subtitles_vtt && (
                          <track
                            kind="subtitles"
                            srcLang="zh"
                            label="中文字幕"
                            default
                            src={studioPath(selectedJob.media.subtitles_vtt)}
                          />
                        )}
                      </video>
                      <div className="actions" style={{ marginTop: 8 }}>
                        <a href={studioPath(selectedJob.media.video)} download={`${selectedJob.id}.mp4`}>
                          下载 MP4 成片
                        </a>
                        {selectedJob.media.subtitles && (
                          <a href={studioPath(selectedJob.media.subtitles)} download={`${selectedJob.id}.srt`}>
                            下载 SRT 字幕
                          </a>
                        )}
                        {selectedJob.media.cover && (
                          <a href={studioPath(selectedJob.media.cover)} target="_blank" rel="noreferrer">
                            查看封面截图
                          </a>
                        )}
                      </div>
                    </div>
                  ) : selectedJob.media?.audio ? (
                    <div className="panel">
                      <p className="hint" style={{ marginTop: 0 }}>
                        配音音轨已就绪，点击上方「渲染成片」生成 MP4 视频。
                      </p>
                      <audio controls src={studioPath(selectedJob.media.audio)} style={{ width: "100%" }} />
                    </div>
                  ) : (
                    <div className="panel">
                      <p className="hint" style={{ margin: 0 }}>
                        尚未生成配音与成片。点击「生成工程与配音」开始音频对齐。
                      </p>
                    </div>
                  )}

                  {selectedJob.qc_report && (
                    <div className="panel">
                      <div className="panel-heading">
                        <strong>FFprobe / FFmpeg 成片质检</strong>
                        <StatusPill
                          tone={selectedJob.qc_report.passed ? "good" : "bad"}
                          label={selectedJob.qc_report.passed ? "质检通过" : "质检未通过"}
                        />
                      </div>
                      <p className="hint" style={{ margin: "6px 0 0" }}>
                        分辨率 {selectedJob.qc_report.width || "—"}×{selectedJob.qc_report.height || "—"} ·
                        实测时长 {selectedJob.qc_report.probed_duration_seconds?.toFixed(2) ?? "—"}s（目标{" "}
                        {selectedJob.qc_report.expected_duration_seconds?.toFixed(2) ?? "—"}s，偏差{" "}
                        {selectedJob.qc_report.duration_drift_seconds?.toFixed(3) ?? "—"}s） ·
                        体积 {formatBytes(selectedJob.qc_report.size_bytes)}
                        {selectedJob.qc_report.loudnorm_applied ? " · 已响度归一化" : ""}
                      </p>
                      {selectedJob.qc_report.issues && selectedJob.qc_report.issues.length > 0 && (
                        <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                          {selectedJob.qc_report.issues.map((issue, idx) => (
                            <li key={idx}>{issue}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}

                  {(selectedJob.media?.contact_sheet || selectedVisualReview || selectedJob.plan) && (
                    <div className="panel" data-visual-qc-panel="">
                      <div className="panel-heading">
                        <strong>关键帧审阅图 (Contact Sheet) 与 VLM 视觉审阅</strong>
                        <div className="actions">
                          {selectedJob.media?.contact_sheet && (
                            <a
                              href={studioPath(selectedJob.media.contact_sheet)}
                              target="_blank"
                              rel="noreferrer"
                            >
                              查看原图
                            </a>
                          )}
                          <button
                            type="button"
                            disabled={busyAction !== null || selectedJob.status === "rendering"}
                            onClick={() => void handleVisualReview(selectedJob)}
                          >
                            {busyAction === "visual_review" ? "视觉审阅中…" : "运行 VLM 视觉审阅"}
                          </button>
                        </div>
                      </div>

                      {selectedJob.media?.contact_sheet && (
                        <div style={{ marginTop: 10 }}>
                          <img
                            src={studioPath(selectedJob.media.contact_sheet)}
                            alt={`${selectedJob.title} 关键帧审阅图`}
                            style={{ width: "100%", borderRadius: 10, border: "1px solid var(--win-border-light)" }}
                          />
                        </div>
                      )}

                      {selectedVisualReview && (
                        <div style={{ display: "grid", gap: 6, marginTop: 10 }} data-visual-review="">
                          <div className="actions" style={{ flexWrap: "wrap" }}>
                            <StatusPill
                              tone={
                                selectedVisualReview.reviewed
                                  ? selectedVisualReview.overall_score >= 75
                                    ? "good"
                                    : "amber"
                                  : "neutral"
                              }
                              label={
                                selectedVisualReview.reviewed
                                  ? `视觉评分：${selectedVisualReview.overall_score} / 100`
                                  : "仅静态视觉检查"
                              }
                            />
                            <StatusPill
                              tone={
                                (MONOTONY_RISK_META[selectedVisualReview.monotony_risk] ||
                                  MONOTONY_RISK_META.unknown).tone
                              }
                              label={
                                (MONOTONY_RISK_META[selectedVisualReview.monotony_risk] ||
                                  MONOTONY_RISK_META.unknown).label
                              }
                            />
                          </div>
                          {selectedVisualReview.layout_issues &&
                            selectedVisualReview.layout_issues.length > 0 && (
                              <div>
                                <span className="hint">版式 / 节奏问题：</span>
                                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                                  {selectedVisualReview.layout_issues.map((issue, idx) => (
                                    <li key={idx}>{issue}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          {selectedVisualReview.suggestions &&
                            selectedVisualReview.suggestions.length > 0 && (
                              <div>
                                <span className="hint">优化建议：</span>
                                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                                  {selectedVisualReview.suggestions.map((sug, idx) => (
                                    <li key={idx}>{sug}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          {selectedVisualReview.scenes && selectedVisualReview.scenes.length > 0 && (
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
                              {selectedVisualReview.scenes.map((sc) => (
                                <span key={sc.scene_index} className={styles.claimChip}>
                                  #{sc.scene_index} [{sc.verdict}] {sc.note}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  {hasPublication && (
                    <div className="panel">
                      <div className="panel-heading">
                        <strong>平台投稿状态</strong>
                        <StatusPill tone={isPubliclyVisible ? "good" : "amber"} label={publicationLabel} />
                      </div>
                      {(selectedJob.receipt?.bvid || selectedJob.receipt?.aid) && (
                        <p className="hint">投稿编号 {selectedJob.receipt.bvid || `av${selectedJob.receipt.aid}`}</p>
                      )}
                      <div className="actions">
                        {publicVideoUrl && (
                          <a href={publicVideoUrl} target="_blank" rel="noopener noreferrer">打开 B 站公开视频</a>
                        )}
                        <button type="button" disabled={busyAction !== null}
                          onClick={() => void handleRecheckPublication(selectedJob)}>
                          {busyAction === "recheck_publish" ? "正在核对…" : "核对公开状态"}
                        </button>
                      </div>
                      <p className="hint">只读核对原投稿回执和公开页面。</p>
                      {publicationReadback?.jobId === selectedJob.id && (
                        <p className="hint" role="status">{publicationReadback.reason || "已核对公开状态。"}</p>
                      )}
                    </div>
                  )}

                  {!hasPublication && (
                    <div className="panel">
                      <div className="panel-heading">
                        <strong>
                          公开参考图片 / 截图或短视频（最多 {MAX_UPLOAD_FILES} 份，可选绑定到分镜）
                        </strong>
                        <button
                          type="button"
                          disabled={
                            busyAction !== null ||
                            selectedJob.status === "rendering" ||
                            (selectedJob.uploads?.length || 0) >= MAX_UPLOAD_FILES
                          }
                          onClick={() => void handleCaptureUi(selectedJob)}
                        >
                          {busyAction === "capture_ui" ? "截图中…" : "自拍工坊界面"}
                        </button>
                      </div>
                      <div style={{ display: "grid", gap: 10, marginTop: 8 }}>
                        <label style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                          <span className="hint">上传 PNG / JPEG / WebP 或 MP4 / WebM（≤30MB）</span>
                          <input
                            type="file"
                            multiple
                            accept={UPLOAD_ACCEPT_ATTR}
                            aria-label="上传公开参考图片"
                            disabled={
                              busyAction !== null ||
                              selectedJob.status === "rendering" ||
                              (selectedJob.uploads?.length || 0) >= MAX_UPLOAD_FILES
                            }
                            onChange={(e) => {
                              const files = e.target.files;
                              void handleUploadFiles(selectedJob, files);
                              e.target.value = "";
                            }}
                          />
                          <span className="hint">
                            {selectedJob.uploads?.length || 0} / {MAX_UPLOAD_FILES}
                          </span>
                        </label>
                        {selectedJob.uploads && selectedJob.uploads.length > 0 && (
                          <div style={{ display: "grid", gap: 8 }}>
                            {selectedJob.uploads.map((item) => {
                              const isVideo = isVideoUploadItem(item);
                              const boundScene = (selectedJob.plan?.scenes || []).find((s) =>
                                isVideo
                                  ? s.visual_props?.uploaded_video_file === item.rel_path
                                  : s.visual_props?.uploaded_image_file === item.rel_path
                              );
                              return (
                                <div
                                  key={item.rel_path}
                                  style={{
                                    display: "flex",
                                    alignItems: "center",
                                    gap: 10,
                                    flexWrap: "wrap",
                                  }}
                                >
                                  {isVideo ? (
                                    <video
                                      src={studioPath(item.url)}
                                      muted
                                      playsInline
                                      preload="metadata"
                                      style={{ width: 64, height: 42, objectFit: "cover", borderRadius: 6 }}
                                    />
                                  ) : (
                                    <img
                                      src={studioPath(item.url)}
                                      alt={item.filename}
                                      style={{ width: 64, height: 42, objectFit: "cover", borderRadius: 6 }}
                                    />
                                  )}
                                  <span style={{ fontWeight: 600 }}>{item.filename}</span>
                                  {isVideo && <StatusPill tone="blue" label="视频片段" />}
                                  <span className="hint">{formatBytes(item.size_bytes)}</span>
                                  <select
                                    aria-label={`绑定图片 ${item.filename} 到分镜`}
                                    value={boundScene?.scene_id || ""}
                                    disabled={busyAction !== null || selectedJob.status === "rendering"}
                                    onChange={(e) =>
                                      void handleBindUploadToScene(selectedJob, item, e.target.value)
                                    }
                                  >
                                    <option value="">未绑定分镜</option>
                                    {(selectedJob.plan?.scenes || []).map((scene, idx) => (
                                      <option key={scene.scene_id} value={scene.scene_id}>
                                        #{idx + 1} {scene.heading}
                                      </option>
                                    ))}
                                  </select>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {selectedJob.plan?.sources && selectedJob.plan.sources.length > 0 && (
                    <div className="panel">
                      <strong>参考来源与抓取状态</strong>
                      <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                        {selectedJob.plan.sources.map((src, idx) => {
                          const safeUrl = safeHttpsUrl(src.url);
                          const statusInfo = src.fetch_status ? FETCH_STATUS_META[src.fetch_status] : null;
                          return (
                            <li key={`${src.source_id}-${idx}`} style={{ marginBottom: 4 }}>
                              <span>
                                [{src.kind}] {src.title || src.source_id}
                              </span>
                              {statusInfo && (
                                <>
                                  {" "}
                                  <StatusPill tone={statusInfo.tone} label={statusInfo.label} />
                                </>
                              )}
                              {safeUrl && (
                                <>
                                  {" · "}
                                  <a href={safeUrl} target="_blank" rel="noopener noreferrer">
                                    {safeUrl}
                                  </a>
                                </>
                              )}
                              {src.fetch_status === "fallback" && src.fetch_error && (
                                <div className="hint" style={{ color: "var(--amber, #d97706)" }}>
                                  抓取降级原因：{src.fetch_error}
                                </div>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  )}

                  {!hasPublication && (
                    <div className="panel">
                      <strong>稿件元数据（发布前可微调）</strong>
                      <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
                        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          <span className="hint">标题（≤80 字）</span>
                          <input
                            type="text"
                            value={editTitle}
                            maxLength={80}
                            onChange={(e) => setEditTitle(e.target.value)}
                          />
                        </label>
                        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          <span className="hint">简介</span>
                          <textarea
                            rows={3}
                            value={editSummary}
                            maxLength={1000}
                            onChange={(e) => setEditSummary(e.target.value)}
                          />
                        </label>
                        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          <span className="hint">标签（逗号分隔）</span>
                          <input
                            type="text"
                            value={editTags}
                            onChange={(e) => setEditTags(e.target.value)}
                          />
                        </label>
                        <div className="actions">
                          <button
                            type="button"
                            disabled={busyAction !== null}
                            onClick={() => void handleSaveMetadata(selectedJob)}
                          >
                            保存元数据
                          </button>
                          {selectedJob.qc_report?.passed && (
                            <>
                              <button
                                type="button"
                                disabled={busyAction !== null}
                                onClick={() => void handleStageOrPublish(selectedJob, false)}
                              >
                                暂存人工审阅
                              </button>
                              <button
                                type="button"
                                className="primary"
                                disabled={busyAction !== null || !selectedJob.content_hash}
                                onClick={() => void handleStageOrPublish(selectedJob, true)}
                              >
                                确认投稿到 B 站
                              </button>
                              {!selectedJob.content_hash && <span className="hint">请重新渲染，获取可确认的成片版本。</span>}
                            </>
                          )}
                          <button
                            type="button"
                            disabled={busyAction !== null || selectedJob.status === "rendering"}
                            onClick={() => void handleDelete(selectedJob)}
                            style={{ marginLeft: "auto" }}
                          >
                            删除任务
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {inspectorTab === "script" && (
                <VideoStoryboardTab
                  job={selectedJob}
                  busy={busyAction !== null}
                  onChangeSceneMeme={(sceneId, currentAssetId) =>
                    setMemePickerTarget({
                      mode: "scene",
                      job: selectedJob,
                      sceneId,
                      currentAssetId,
                    })
                  }
                />
              )}

              {inspectorTab === "code" && (
                <div>
                  <div className={styles.segmentedControl} style={{ marginBottom: 10 }}>
                    {(
                      [
                        ["props", "props.json"],
                        ["code", "AquaVideo.tsx"],
                        ["subtitles", "subtitles.srt"],
                        ["manifest", "manifest.json"],
                      ] as const
                    ).map(([key, label]) => (
                      <button
                        key={key}
                        type="button"
                        className={codeTab === key ? styles.pillActive : undefined}
                        onClick={() => setCodeTab(key)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  {!selectedJob.media?.[codeTab] ? (
                    <p className="hint">请先点击「生成工程与配音」后再查看 Remotion 代码与 Props 产物。</p>
                  ) : codeLoading ? (
                    <p className="hint">正在读取产物内容…</p>
                  ) : (
                    <pre
                      style={{
                        maxHeight: 460,
                        overflow: "auto",
                        padding: 14,
                        borderRadius: 10,
                        fontSize: 13,
                        lineHeight: 1.5,
                      }}
                    >
                      {codeArtifacts[activeCodeCacheKey] || ""}
                    </pre>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {memePickerTarget !== null && (
        <MemePickerModal
          open
          selectedAssetId={
            memePickerTarget.mode === "scene" ? memePickerTarget.currentAssetId : undefined
          }
          title={memePickerTarget.mode === "scene" ? "更换分镜表情" : "置顶图库表情（0–3 张）"}
          description="仅显示允许用于全渠道（含动态与视频）的已启用表情图片。"
          resetLabel={memePickerTarget.mode === "scene" ? "清除场景表情" : undefined}
          accept={ACCEPT_ALL_CHANNELS_MEMES}
          onClose={() => setMemePickerTarget(null)}
          onSelect={async (assetId, asset) => {
            if (memePickerTarget.mode === "composer") {
              if (!asset || !assetId) return;
              setPinnedMemes((prev) => {
                if (prev.some((item) => item.asset_id === assetId)) return prev;
                if (prev.length >= 3) return prev;
                return [...prev, asset];
              });
              return;
            }
            await handleSetSceneMeme(memePickerTarget.job, memePickerTarget.sceneId, assetId);
          }}
        />
      )}
    </section>
  );
}
