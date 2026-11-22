import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPost, apiPut } from "@/shared/api/client";
import { emitWorkspaceIssue, errorMessage } from "@/shared/lib/errors";
import {
  MemeMetadataDetails,
  MemePreviewBox,
  type MemeAsset,
  type MemeListResponse,
} from "@/shared/ui/MemePickerModal";
import styles from "./MemeGallery.module.css";
import type { MemeUploadPayload } from "./types";

const MEME_EMOTIONS: ReadonlyArray<[string, string]> = [
  ["idle", "平常"],
  ["cheerful", "开心"],
  ["angry", "生气"],
  ["shy", "害羞"],
  ["confused", "困惑"],
  ["excited", "兴奋"],
];

const MEME_EMOTION_MAP = Object.fromEntries(MEME_EMOTIONS);
const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

interface BatchItem {
  file: File;
  importId: string;
  done: boolean;
}

interface UploadFailure {
  name: string;
  message: string;
}

function readImageBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("图片文件读取失败，请重新选择。"));
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result.split(",", 2)[1] : null;
      if (result) resolve(result);
      else reject(new Error("图片文件未能读取为有效内容。"));
    };
    reader.readAsDataURL(file);
  });
}

function parseTags(raw: string): string[] {
  return raw
    .split(/[,，、\n]/u)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function MemeCard({
  asset,
  busy,
  onSaveLabels,
  onToggleEnabled,
  onDelete,
}: {
  asset: MemeAsset;
  busy: boolean;
  onSaveLabels: (asset: MemeAsset, tags: string[], description: string) => Promise<void>;
  onToggleEnabled: (asset: MemeAsset) => Promise<void>;
  onDelete: (asset: MemeAsset) => Promise<void>;
}) {
  const [tagsInput, setTagsInput] = useState(() => (asset.tags || []).join("、"));
  const [descInput, setDescInput] = useState(() => asset.description || "");

  useEffect(() => {
    setTagsInput((asset.tags || []).join("、"));
    setDescInput(asset.description || "");
  }, [asset.asset_id, asset.tags, asset.description]);

  const statusLine = `${asset.missing ? "文件缺失" : asset.enabled ? "已启用" : "已禁用"} · ${(asset.emotions || [])
    .map((item) => MEME_EMOTION_MAP[item] || item)
    .join(" / ")}`;
  const sizeLine = `${asset.width ?? "—"} × ${asset.height ?? "—"} · ${
    asset.animated ? `${asset.frames ?? 0} 帧动图` : "静态图"
  } · ${typeof asset.bytes === "number" ? `${(asset.bytes / 1024).toFixed(1)} KiB` : "大小未知"}`;

  return (
    <article className="meme-card">
      <MemePreviewBox asset={asset} />
      <h3 className={styles.cardTitle}>{asset.title}</h3>
      <p className={`hint ${styles.cardText}`}>{statusLine}</p>
      <p className={`hint ${styles.cardText}`}>{sizeLine}</p>
      <MemeMetadataDetails asset={asset} />
      {asset.tags && asset.tags.length > 0 && <p className={styles.cardTags}>{asset.tags.join(" · ")}</p>}
      {asset.description && <p className={styles.cardText}>{asset.description}</p>}
      <details className="meme-metadata">
        <summary>编辑标签与描述</summary>
        <form
          className={styles.metadataForm}
          onSubmit={(event) => {
            event.preventDefault();
            void onSaveLabels(asset, parseTags(tagsInput), descInput.trim());
          }}
        >
          <label>
            题材标签
            <input
              name="tags"
              placeholder="例如 雨天、撑伞、Q版"
              value={tagsInput}
              onChange={(event) => setTagsInput(event.target.value)}
            />
          </label>
          <label>
            画面描述
            <textarea
              name="description"
              maxLength={400}
              rows={2}
              value={descInput}
              onChange={(event) => setDescInput(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            保存标签
          </button>
        </form>
      </details>
      <div className={`actions ${styles.cardActions}`}>
        <button
          type="button"
          aria-pressed={Boolean(asset.enabled)}
          data-unavailable={String(Boolean(asset.missing))}
          disabled={busy || Boolean(asset.missing)}
          onClick={() => void onToggleEnabled(asset)}
        >
          {asset.enabled ? "禁用" : "启用"}
        </button>
        <button
          type="button"
          className="danger"
          data-unavailable={String(Boolean(asset.enabled))}
          title={asset.enabled ? "请先禁用再删除" : "草稿正在引用的图片由服务保护，无法删除"}
          disabled={busy || Boolean(asset.enabled)}
          onClick={() => {
            if (window.confirm(`删除「${asset.title}」？正在被草稿引用的图片无法删除。`)) {
              void onDelete(asset);
            }
          }}
        >
          删除
        </button>
      </div>
    </article>
  );
}

export function MemeGallery({ onChange }: { onChange?: () => void }) {
  const [items, setItems] = useState<MemeAsset[]>([]);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [stateText, setStateText] = useState("");
  const [feedback, setFeedback] = useState<{ text: string; error: boolean }>({ text: "", error: false });
  const [failures, setFailures] = useState<UploadFailure[]>([]);
  const [filterText, setFilterText] = useState("");

  const [batch, setBatch] = useState<BatchItem[]>([]);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadTags, setUploadTags] = useState("");
  const [uploadDescription, setUploadDescription] = useState("");
  const [emotions, setEmotions] = useState<string[]>(["idle"]);
  const [author, setAuthor] = useState("");
  const [licenseName, setLicenseName] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [licenseUrl, setLicenseUrl] = useState("");
  const [sourceRevision, setSourceRevision] = useState("");
  const [sourceNote, setSourceNote] = useState("");
  const [licenseNote, setLicenseNote] = useState("");

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const sequenceRef = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++sequenceRef.current;
    setLoading(true);
    setStateText("正在读取图库…");
    try {
      const result = await apiGet<"/api/v1/persona/memes", MemeListResponse>("/api/v1/persona/memes");
      if (seq !== sequenceRef.current) return;
      const listItems = Array.isArray(result.items) ? result.items : [];
      setItems(listItems);
      setStateText(
        `${listItems.length} 张图片 · ${listItems.filter((item) => item.enabled && !item.missing).length} 张可用`
      );
    } catch (error) {
      if (seq !== sequenceRef.current) return;
      const msg = errorMessage(error);
      emitWorkspaceIssue(msg, "#assets");
      setStateText(`图库读取失败：${msg}`);
      setItems([]);
    } finally {
      if (seq === sequenceRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function runAction(work: () => Promise<void>) {
    if (pending) return;
    setPending(true);
    try {
      await work();
      onChange?.();
    } catch (error) {
      const msg = errorMessage(error);
      emitWorkspaceIssue(msg, "#assets");
      setFeedback({ text: msg, error: true });
    } finally {
      setPending(false);
    }
  }

  function resetUploadForm() {
    setBatch([]);
    setUploadTitle("");
    setUploadTags("");
    setUploadDescription("");
    setEmotions(["idle"]);
    setAuthor("");
    setLicenseName("");
    setSourceUrl("");
    setLicenseUrl("");
    setSourceRevision("");
    setSourceNote("");
    setLicenseNote("");
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function handleUploadSubmit(event: FormEvent) {
    event.preventDefault();
    if (!batch.some((item) => !item.done)) return;
    if (emotions.length === 0) {
      setFeedback({ text: "请至少选择一种情绪。", error: true });
      return;
    }

    const common = {
      tags: parseTags(uploadTags),
      description: uploadDescription.trim(),
      emotions,
      source: {
        kind: "user_upload" as const,
        author: author.trim(),
        url: sourceUrl.trim(),
        revision: sourceRevision.trim(),
        note: sourceNote.trim(),
      },
      license: {
        name: licenseName.trim(),
        url: licenseUrl.trim(),
        note: licenseNote.trim(),
      },
    };

    await runAction(async () => {
      const nextFailures: UploadFailure[] = [];
      setFailures([]);

      for (const [index, item] of batch.entries()) {
        if (item.done) continue;
        const { file } = item;
        setFeedback({ text: `正在导入第 ${index + 1}／${batch.length} 张`, error: false });
        try {
          if (file.size > MAX_UPLOAD_BYTES) throw new Error("图片超过 8 MiB。");
          const title =
            (batch.length === 1 && uploadTitle.trim()) ||
            file.name.replace(/\.[^.]+$/u, "").trim().slice(0, 120) ||
            "未命名图片";
          const content_base64 = await readImageBase64(file);
          await apiPost<"/api/v1/persona/memes", MemeAsset, MemeUploadPayload>("/api/v1/persona/memes", {
            body: {
              ...common,
              title,
              import_id: item.importId,
              content_base64,
            },
          });
          item.done = true;
        } catch (error) {
          const msg = errorMessage(error);
          emitWorkspaceIssue(msg, "#assets");
          nextFailures.push({ name: file.name, message: msg });
        }
      }

      if (batch.some((item) => item.done)) {
        await refresh();
      }
      if (nextFailures.length === 0) {
        const count = batch.length;
        resetUploadForm();
        setFeedback({ text: `已导入 ${count} 张图片。`, error: false });
      } else if (batch.length === 1) {
        setFeedback({ text: nextFailures[0].message, error: true });
      } else {
        setFeedback({
          text: `已导入 ${batch.length - nextFailures.length} 张，${nextFailures.length} 张失败。再次上传时只重试失败项。`,
          error: true,
        });
        setFailures(nextFailures);
      }
    });
  }

  const normalizedQuery = filterText.trim().normalize("NFKC").toLocaleLowerCase();
  const visibleItems = items.filter(
    (asset) =>
      !normalizedQuery ||
      [asset.title, asset.description, ...(asset.tags || [])].some((value) =>
        value?.normalize("NFKC").toLocaleLowerCase().includes(normalizedQuery)
      )
  );

  return (
    <article className="panel meme-library">
      <div className="panel-heading">
        <h2>角色图库</h2>
        <button
          data-meme-refresh=""
          type="button"
          disabled={pending || loading}
          onClick={() => void refresh()}
        >
          刷新图库
        </button>
      </div>

      <details className={`meme-upload ${styles.uploadDetails}`}>
        <summary>导入图片</summary>
        <form data-meme-upload="" onSubmit={(event) => void handleUploadSubmit(event)}>
          <fieldset data-meme-upload-fields="" className={styles.fields} disabled={pending}>
            <div className="form-grid">
              <label>
                图片文件
                <input
                  ref={fileInputRef}
                  name="file"
                  type="file"
                  multiple
                  accept="image/png,image/jpeg,image/gif,image/webp,.png,.jpg,.jpeg,.gif,.webp"
                  required
                  onChange={(event) => {
                    const selected = Array.from(event.target.files || [], (file) => ({
                      file,
                      importId: crypto.randomUUID(),
                      done: false,
                    }));
                    setBatch(selected);
                    setFailures([]);
                    setFeedback({ text: "", error: false });
                  }}
                />
              </label>
              <label>
                名称（单张选填）
                <input
                  name="title"
                  maxLength={120}
                  disabled={batch.length !== 1}
                  value={uploadTitle}
                  onChange={(event) => setUploadTitle(event.target.value)}
                />
              </label>
            </div>
            <p className="hint">
              可多选 PNG、JPEG、GIF 或 WebP，每张最多 8 MiB、4096 像素宽高和 300 帧。批量导入采用文件名，以下标签、作者与许可应用于这批图片。
            </p>
            <div className="form-grid">
              <label>
                题材标签
                <input
                  name="tags"
                  placeholder="例如 雨天、撑伞、Q版"
                  value={uploadTags}
                  onChange={(event) => setUploadTags(event.target.value)}
                />
              </label>
              <label>
                画面描述
                <textarea
                  name="description"
                  maxLength={400}
                  rows={2}
                  value={uploadDescription}
                  onChange={(event) => setUploadDescription(event.target.value)}
                />
              </label>
            </div>
            <fieldset className={styles.emotions}>
              <legend>情绪（至少一项）</legend>
              {MEME_EMOTIONS.map(([key, labelText]) => {
                const checked = emotions.includes(key);
                return (
                  <label key={key}>
                    <input
                      type="checkbox"
                      name="emotion"
                      value={key}
                      checked={checked}
                      onChange={(event) => {
                        const isChecked = event.target.checked;
                        setEmotions((prev) =>
                          isChecked ? [...new Set([...prev, key])] : prev.filter((item) => item !== key)
                        );
                      }}
                    />
                    {labelText}
                  </label>
                );
              })}
            </fieldset>
            <div className="form-grid">
              <label>
                来源作者
                <input
                  name="author"
                  maxLength={300}
                  required
                  placeholder="实际作者或权利人"
                  value={author}
                  onChange={(event) => setAuthor(event.target.value)}
                />
              </label>
              <label>
                许可名称
                <input
                  name="license_name"
                  maxLength={300}
                  required
                  placeholder="实际许可或授权名称"
                  value={licenseName}
                  onChange={(event) => setLicenseName(event.target.value)}
                />
              </label>
            </div>
            <div className="form-grid">
              <label>
                来源链接（可选）
                <input
                  name="source_url"
                  type="url"
                  value={sourceUrl}
                  onChange={(event) => setSourceUrl(event.target.value)}
                />
              </label>
              <label>
                许可链接（可选）
                <input
                  name="license_url"
                  type="url"
                  value={licenseUrl}
                  onChange={(event) => setLicenseUrl(event.target.value)}
                />
              </label>
            </div>
            <label>
              来源版本（可选）
              <input
                name="source_revision"
                maxLength={160}
                placeholder="提交 SHA 或素材包版本"
                value={sourceRevision}
                onChange={(event) => setSourceRevision(event.target.value)}
              />
            </label>
            <label>
              来源备注（可选）
              <textarea
                name="source_note"
                rows={2}
                maxLength={2000}
                value={sourceNote}
                onChange={(event) => setSourceNote(event.target.value)}
              />
            </label>
            <label>
              许可备注（可选）
              <textarea
                name="license_note"
                rows={2}
                maxLength={2000}
                value={licenseNote}
                onChange={(event) => setLicenseNote(event.target.value)}
              />
            </label>
            <div className="actions" style={{ marginTop: 10 }}>
              <button type="submit" className="primary">
                上传到图库
              </button>
            </div>
          </fieldset>
        </form>
      </details>

      <p data-meme-feedback="" className={feedback.error ? "hint error-text" : "hint"} role="status">
        {feedback.text}
      </p>
      <ul data-meme-failures="" className={styles.failuresList} hidden={failures.length === 0}>
        {failures.map((item, idx) => (
          <li key={`${item.name}-${idx}`}>{`${item.name} · ${item.message}`}</li>
        ))}
      </ul>

      <label>
        搜索图库
        <input
          type="search"
          data-meme-filter=""
          className={styles.filterInput}
          placeholder="名称、标签或画面描述"
          value={filterText}
          onChange={(event) => setFilterText(event.target.value)}
        />
      </label>

      <p data-meme-state="" className="hint" role="status">
        {stateText}
      </p>

      <div data-meme-grid="" className="meme-grid">
        {visibleItems.length === 0 ? (
          <p className="hint">{normalizedQuery ? "没有匹配的图片。" : "图库还没有图片。"}</p>
        ) : (
          visibleItems.map((asset) => (
            <MemeCard
              key={asset.asset_id}
              asset={asset}
              busy={pending || loading}
              onSaveLabels={async (target, tags, description) => {
                await runAction(async () => {
                  await apiPut<
                    "/api/v1/persona/memes/{asset_id}/labels",
                    MemeAsset,
                    { tags: string[]; description: string }
                  >("/api/v1/persona/memes/{asset_id}/labels", {
                    pathParams: { asset_id: target.asset_id },
                    body: { tags, description },
                  });
                  await refresh();
                  setFeedback({ text: `已保存「${target.title}」的标签与描述。`, error: false });
                });
              }}
              onToggleEnabled={async (target) => {
                await runAction(async () => {
                  await apiPut<"/api/v1/persona/memes/{asset_id}", MemeAsset, { enabled: boolean }>(
                    "/api/v1/persona/memes/{asset_id}",
                    {
                      pathParams: { asset_id: target.asset_id },
                      body: { enabled: !target.enabled },
                    }
                  );
                  await refresh();
                  setFeedback({
                    text: target.enabled ? "已禁用此表情包。" : "已启用此表情包。",
                    error: false,
                  });
                });
              }}
              onDelete={async (target) => {
                await runAction(async () => {
                  await apiDelete("/api/v1/persona/memes/{asset_id}", {
                    pathParams: { asset_id: target.asset_id },
                  });
                  await refresh();
                  setFeedback({ text: "图片已从图库删除。", error: false });
                });
              }}
            />
          ))
        )}
      </div>
    </article>
  );
}
