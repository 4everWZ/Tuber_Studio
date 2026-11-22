import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiGet, apiPost, studioPath } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { list } from "@/shared/lib/display";
import { emitNotice, errorMessage, safeMessage } from "@/shared/lib/errors";
import styles from "./VoicesPage.module.css";
import type { VoiceOption, VoicePreviewResponse } from "./types";

export function voiceAvailability(voice: VoiceOption | undefined): string {
  if (!voice) return "请选择可用的音色。";
  if (voice.provider === "prerecorded") return "";
  if (voice.id === "external_api") {
    return "该角色保留外部 API 音色选择。要用 Omni 试听，请选择参考音色。";
  }
  if (voice.provider === "omni_reference" && voice.reference_asset) return "";
  return "该音色缺少可用的参考录音。";
}

export function VoicesPage() {
  const { status, settings } = useAppSession();
  const [voices, setVoices] = useState<VoiceOption[]>([]);
  const [selectedVoiceId, setSelectedVoiceId] = useState("");
  const [userPickedVoice, setUserPickedVoice] = useState(false);
  const [previewText, setPreviewText] = useState("你好。很高兴和你说说话。");
  const [previewState, setPreviewState] = useState("");
  const [audioSrc, setAudioSrc] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const selectedVoiceRef = useRef("");
  const previewSeqRef = useRef(0);
  selectedVoiceRef.current = selectedVoiceId;

  const activeVoice = status?.character
    ? status.character.voice_profile || settings?.voice_id || ""
    : "";

  useEffect(() => {
    let mounted = true;
    apiGet<"/api/v1/persona/voices", VoiceOption[]>("/api/v1/persona/voices")
      .then((data) => {
        if (!mounted) return;
        const items = list<VoiceOption>(data);
        setVoices(items);
      })
      .catch((error) => {
        if (mounted) emitNotice(errorMessage(error), true);
      });
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    if (!userPickedVoice) {
      setSelectedVoiceId(activeVoice);
    } else if (voices.length > 0 && !voices.some((voice) => voice.id === selectedVoiceRef.current)) {
      setSelectedVoiceId(activeVoice);
      setUserPickedVoice(false);
    }
  }, [activeVoice, userPickedVoice, voices]);

  const currentVoice = voices.find((item) => item.id === selectedVoiceId);
  const unavailableReason = voiceAvailability(currentVoice);
  const isFixed = currentVoice?.provider === "prerecorded";
  const helpText =
    unavailableReason || (isFixed ? `固定录音：${currentVoice?.sample_text || currentVoice?.name || ""}` : "");

  function handleVoiceChange(nextId: string) {
    previewSeqRef.current += 1;
    selectedVoiceRef.current = nextId;
    setSelectedVoiceId(nextId);
    setUserPickedVoice(true);
    setSubmitting(false);
    audioRef.current?.pause();
    setAudioSrc(null);
    setPreviewState("");
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    if (unavailableReason || !currentVoice) {
      emitNotice(unavailableReason || "请选择可用的音色。", true);
      return;
    }
    const voiceAtStart = currentVoice;
    const seq = ++previewSeqRef.current;
    setSubmitting(true);
    setPreviewState("等待合成…");
    audioRef.current?.pause();
    setAudioSrc(null);

    try {
      const result = await apiPost<
        "/api/v1/persona/voices/preview",
        VoicePreviewResponse,
        { text: string; voice_id: string }
      >("/api/v1/persona/voices/preview", {
        body: {
          text: voiceAtStart.provider === "prerecorded" ? voiceAtStart.sample_text || "" : previewText,
          voice_id: voiceAtStart.id,
        },
      });
      if (previewSeqRef.current !== seq || selectedVoiceRef.current !== voiceAtStart.id) return;
      setAudioSrc(studioPath(result.audio_ref));
      const recorded = voiceAtStart.provider === "prerecorded";
      const durationText = result.duration != null ? ` · ${Number(result.duration).toFixed(2)} 秒` : "";
      const actualText = recorded && result.text ? ` 实际录音台词：${result.text}` : "";
      const warningText = result.warning ? ` ${result.warning}` : "";
      setPreviewState(
        safeMessage(`${recorded ? "预录已准备" : "合成完成"}${durationText}，点击播放试听。${actualText}${warningText}`)
      );
    } catch (error) {
      if (previewSeqRef.current !== seq || selectedVoiceRef.current !== voiceAtStart.id) return;
      const msg = errorMessage(error);
      setPreviewState(`合成失败：${safeMessage(msg)}`);
      emitNotice(msg, true);
    } finally {
      if (previewSeqRef.current === seq) {
        setSubmitting(false);
      }
    }
  }

  return (
    <section data-section="voices" className="win-page-view">
      <div className="page-heading win-heading">
        <h1>角色音色试听</h1>
        <a className="button quiet" href="#overview">
          查看语音缓存
        </a>
      </div>

      <article className="win-group-box">
        <div className="win-group-title">试听</div>
        <form id="preview-form" className={styles.formSpacer} onSubmit={(event) => void handleSubmit(event)}>
          <label>
            音色选择
            <select
              id="preview-voice"
              required
              data-active-voice={activeVoice}
              data-selected-voice={userPickedVoice ? selectedVoiceId : undefined}
              value={selectedVoiceId}
              onChange={(event) => handleVoiceChange(event.target.value)}
            >
              {voices.map((voice) => (
                <option key={voice.id} value={voice.id}>
                  {voice.name || voice.id}
                </option>
              ))}
            </select>
          </label>
          <label style={{ marginTop: 8 }} hidden={isFixed}>
            试听文本
            <textarea
              id="preview-text"
              rows={3}
              maxLength={120}
              required={!isFixed}
              disabled={isFixed || Boolean(unavailableReason)}
              value={previewText}
              onChange={(event) => setPreviewText(event.target.value)}
            />
          </label>
          <div className="actions" style={{ marginTop: 10 }}>
            <button
              id="preview-submit"
              type="submit"
              className="primary win-btn"
              disabled={submitting || Boolean(unavailableReason)}
              aria-busy={submitting ? "true" : undefined}
            >
              {isFixed ? "试听固定样本" : "生成试听音频"}
            </button>
            <span id="preview-state" className="hint" role="status">
              {previewState}
            </span>
          </div>
          <audio
            ref={audioRef}
            id="preview-audio"
            className={styles.audioPlayer}
            controls
            preload="none"
            hidden={!audioSrc}
            src={audioSrc ?? undefined}
          />
        </form>
        <p id="voice-preview-help" className={`hint ${styles.helpText}`}>
          {helpText}
        </p>
      </article>

      <div className={`win-group-box ${styles.groupSpacer}`}>
        <div className="win-group-title">音色与许可</div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>音色名称</th>
                <th>许可状态 / 来源</th>
              </tr>
            </thead>
            <tbody id="voices">
              {voices.length === 0 ? (
                <tr>
                  <td colSpan={2} className="empty-cell">
                    暂无可用音色。
                  </td>
                </tr>
              ) : (
                voices.map((voice) => (
                  <tr key={voice.id}>
                    <td>{voice.name || voice.id}</td>
                    <td>{voice.license_status || "待确认"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
