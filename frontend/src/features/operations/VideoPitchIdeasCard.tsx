import { useState } from "react";
import { StatusPill } from "@/shared";
import { errorMessage } from "@/shared/lib/errors";
import {
  CHANNEL_META,
  DURATION_PRESET_LABELS,
  VIDEO_MODE_LABELS,
  type VideoIdeaPitch,
  videoRequest,
} from "./videoStudioTypes";

interface VideoPitchIdeasCardProps {
  disabled?: boolean;
  onApplyVideoIdea: (idea: VideoIdeaPitch) => void;
}

export function VideoPitchIdeasCard({ disabled, onApplyVideoIdea }: VideoPitchIdeasCardProps) {
  const [open, setOpen] = useState(false);
  const [briefHint, setBriefHint] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [ideas, setIdeas] = useState<VideoIdeaPitch[]>([]);
  const [guidance, setGuidance] = useState("");

  async function handlePitchIdeas() {
    setLoading(true);
    setError("");
    try {
      const res = await videoRequest<{
        ok?: boolean;
        ideas?: VideoIdeaPitch[];
        channel_guidance?: string;
      }>("/api/v1/operations/videos/pitch-ideas", {
        method: "POST",
        body: {
          brief_hint: briefHint.trim(),
          max_ideas: 6,
        },
      });
      setIdeas(res.ideas || []);
      setGuidance(res.channel_guidance || "");
      setOpen(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="panel" style={{ marginTop: 12 }} data-pitch-ideas-card="">
      <div className="panel-heading">
        <div>
          <strong>创作灵感与渠道判定（视频 / 动态 / 评论）</strong>
          <p className="hint" style={{ margin: "2px 0 0" }}>
            让 Aqua 结合近期已看视频与公开记忆，建议哪些选题适合做多镜视频、发动态或评论回复。
          </p>
        </div>
        <div className="actions">
          {ideas.length > 0 && (
            <button type="button" onClick={() => setOpen((prev) => !prev)}>
              {open ? "收起灵感列表" : `展开灵感 (${ideas.length})`}
            </button>
          )}
          <button
            type="button"
            disabled={disabled || loading}
            onClick={() => void handlePitchIdeas()}
          >
            {loading ? "构思中…" : "获取创作灵感"}
          </button>
        </div>
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
        <input
          type="text"
          aria-label="灵感方向提示"
          placeholder="可选：输入想聊的关键词（例如：LoRA 微调对比、近期看片、工作台架构）"
          value={briefHint}
          maxLength={400}
          style={{ flex: "1 1 260px" }}
          onChange={(e) => setBriefHint(e.target.value)}
        />
      </div>

      {error && (
        <p className="hint" role="alert" style={{ color: "var(--bad, #dc2626)", marginTop: 8 }}>
          灵感获取失败：{error}
        </p>
      )}

      {open && (
        <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
          {guidance && (
            <p className="hint" style={{ margin: 0, whiteSpace: "pre-line" }}>
              {guidance}
            </p>
          )}
          {ideas.length === 0 ? (
            <p className="hint" style={{ margin: 0 }}>
              暂无候选灵感，请填写方向提示后重试。
            </p>
          ) : (
            <div style={{ display: "grid", gap: 8 }}>
              {ideas.map((idea) => {
                const channelMeta = CHANNEL_META[idea.channel] || {
                  label: idea.channel,
                  tone: "neutral" as const,
                };
                return (
                  <div
                    key={idea.idea_id}
                    className="panel"
                    style={{ padding: 10 }}
                    data-pitch-idea-id={idea.idea_id}
                  >
                    <div className="panel-heading" style={{ gap: 8 }}>
                      <div className="actions" style={{ flexWrap: "wrap" }}>
                        <StatusPill tone={channelMeta.tone} label={channelMeta.label} />
                        {idea.channel === "video" && (
                          <>
                            <StatusPill
                              tone="blue"
                              label={VIDEO_MODE_LABELS[idea.video_mode] || idea.video_mode}
                            />
                            <StatusPill
                              tone="neutral"
                              label={
                                DURATION_PRESET_LABELS[idea.duration_preset] || idea.duration_preset
                              }
                            />
                          </>
                        )}
                        <strong>{idea.title}</strong>
                      </div>
                      {idea.channel === "video" ? (
                        <button
                          type="button"
                          className="primary"
                          disabled={disabled}
                          onClick={() => onApplyVideoIdea(idea)}
                        >
                          填入制片面板
                        </button>
                      ) : (
                        <span className="hint">非视频优先（不阻塞手填）</span>
                      )}
                    </div>
                    <p className="hint" style={{ margin: "6px 0 4px" }}>
                      <strong>选题：</strong>
                      {idea.topic}
                    </p>
                    <p className="hint" style={{ margin: "2px 0" }}>
                      <strong>切入钩子：</strong>
                      {idea.hook} · <strong>视觉角度：</strong>
                      {idea.visual_angle}
                    </p>
                    <p className="hint" style={{ margin: "2px 0 0" }}>
                      <strong>渠道判定理由：</strong>
                      {idea.why_this_channel}
                      {idea.source_refs && idea.source_refs.length > 0
                        ? ` · 来源参考：${idea.source_refs.join(", ")}`
                        : ""}
                    </p>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
