import { StatusPill } from "@/shared";
import {
  CAMERA_MOTION_LABELS,
  FETCH_STATUS_META,
  SCENE_THEME_LABELS,
  SFX_LABELS,
  TEMPLATE_LABELS,
  safeHttpsUrl,
  type VideoJobRecord,
} from "./videoStudioTypes";

interface VideoStoryboardTabProps {
  job: VideoJobRecord;
  busy: boolean;
  onChangeSceneMeme: (sceneId: string, currentAssetId?: string | null) => void;
}

export function VideoStoryboardTab({
  job,
  busy,
  onChangeSceneMeme,
}: VideoStoryboardTabProps) {
  const scenes = job.plan?.scenes || [];
  const sources = job.plan?.sources || [];

  return (
    <div style={{ display: "grid", gap: 12 }}>
      {scenes.map((scene, idx) => {
        const themeMeta = scene.theme ? SCENE_THEME_LABELS[scene.theme] : null;
        const cameraLabel = scene.camera_motion
          ? CAMERA_MOTION_LABELS[scene.camera_motion] || scene.camera_motion
          : null;
        const vp = scene.visual_props;

        return (
          <div key={scene.scene_id} className="panel" data-scene-id={scene.scene_id}>
            <div className="panel-heading">
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                <strong>
                  #{idx + 1} · {scene.heading}
                </strong>
                {scene.chapter_title && (
                  <span className="hint">章节：{scene.chapter_title}</span>
                )}
              </div>
              <div className="actions">
                <StatusPill
                  tone="blue"
                  label={TEMPLATE_LABELS[scene.template] || scene.template}
                />
                {themeMeta && <StatusPill tone={themeMeta.tone} label={themeMeta.label} />}
                {cameraLabel && <StatusPill tone="neutral" label={`运镜: ${cameraLabel}`} />}
                {vp?.meme_asset_id && (
                  <StatusPill tone="amber" label={`表情: ${vp.meme_asset_id}`} />
                )}
                {vp?.uploaded_image_file && (
                  <StatusPill tone="good" label={`配图: ${vp.uploaded_image_file}`} />
                )}
                {vp?.uploaded_video_file && (
                  <StatusPill tone="good" label={`视频片段: ${vp.uploaded_video_file}`} />
                )}
                {scene.avatar_cue && <StatusPill tone="neutral" label={scene.avatar_cue} />}
                <button
                  type="button"
                  data-scene-change-meme={scene.scene_id}
                  disabled={
                    busy ||
                    job.status === "published" ||
                    job.status === "publish_uncertain" ||
                    job.status === "rendering"
                  }
                  onClick={() => onChangeSceneMeme(scene.scene_id, vp?.meme_asset_id)}
                >
                  更换表情
                </button>
              </div>
            </div>

            {vp?.stat_callout && (
              <p style={{ margin: "6px 0", fontWeight: 600 }}>核心指标：{vp.stat_callout}</p>
            )}
            {vp?.subtitle && (
              <p className="hint" style={{ margin: "6px 0" }}>
                {vp.subtitle}
              </p>
            )}
            {vp?.bullets && vp.bullets.length > 0 && (
              <ul style={{ margin: "6px 0", paddingLeft: 18 }}>
                {vp.bullets.map((b, i) => (
                  <li key={i}>{b}</li>
                ))}
              </ul>
            )}
            {vp?.code_snippet && (
              <pre style={{ margin: "8px 0", overflowX: "auto", padding: 10, borderRadius: 8 }}>
                {vp.code_snippet}
              </pre>
            )}
            {vp?.code_steps && vp.code_steps.length > 0 && (
              <div className="hint" style={{ margin: "6px 0" }}>
                分步代码演示：{vp.code_steps.map((step) => step.title || "代码步骤").join(" → ")}
              </div>
            )}
            {vp?.diagram_nodes && vp.diagram_nodes.length > 0 && (
              <div className="hint" style={{ margin: "6px 0" }}>
                架构节点：{vp.diagram_nodes.map((n) => n.label).join(" → ")}
              </div>
            )}
            {vp?.ui_focus_regions && vp.ui_focus_regions.length > 0 && (
              <div className="hint" style={{ margin: "6px 0" }}>
                画面特写区域：
                {vp.ui_focus_regions
                  .map((reg) => reg.label || `(${reg.x}%, ${reg.y}%)`)
                  .join(" / ")}
              </div>
            )}
            {vp?.chart_items && vp.chart_items.length > 0 && (
              <div className="hint" style={{ margin: "6px 0" }}>
                图表项：
                {vp.chart_items.map((pt) => `${pt.label}: ${pt.value}${pt.unit || ""}`).join(" / ")}
              </div>
            )}

            <div style={{ marginTop: 8 }}>
              <strong className="hint">口播台词</strong>
              {scene.lines.map((line, lIdx) => {
                const hasReveal = line.reveal_step !== undefined && line.reveal_step !== null;
                const hasSfx = Boolean(line.sfx && line.sfx !== "none");
                return (
                  <blockquote key={line.line_id || lIdx} className="quoted-text" style={{ margin: "6px 0" }}>
                    <div>{line.text}</div>
                    {(line.emphasis?.length || hasReveal || line.visual_focus || hasSfx) && (
                      <div className="hint" style={{ marginTop: 4, display: "flex", flexWrap: "wrap", gap: 8 }}>
                        {line.emphasis && line.emphasis.length > 0 && (
                          <span>花字高亮：{line.emphasis.join(" / ")}</span>
                        )}
                        {hasReveal && <span>渐进揭晓：第 {line.reveal_step} 步</span>}
                        {line.visual_focus && <span>画面聚焦：{line.visual_focus}</span>}
                        {hasSfx && <span>音效：{SFX_LABELS[line.sfx!] || line.sfx}</span>}
                      </div>
                    )}
                  </blockquote>
                );
              })}
            </div>
          </div>
        );
      })}

      <div className="panel">
        <strong>已核验公开来源（严格隔离私有记忆与私信）</strong>
        <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
          {sources.map((src, idx) => {
            const safeUrl = safeHttpsUrl(src.url);
            const statusInfo = src.fetch_status ? FETCH_STATUS_META[src.fetch_status] : null;
            return (
              <li key={`${src.source_id}-${idx}`}>
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
                      打开原来源
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
    </div>
  );
}
