import { useCallback, useEffect, useId, useRef, useState } from "react";
import { apiGet, studioPath } from "../api/client";
import { errorMessage } from "../lib/errors";

export interface MemeSourceInfo {
  kind?: string | null;
  author?: string | null;
  author_id?: string | null;
  url?: string | null;
  revision?: string | null;
  note?: string | null;
}

export interface MemeLicenseInfo {
  name?: string | null;
  url?: string | null;
  note?: string | null;
}

export type MemeUsageScope = "all_channels" | "interaction_only";

export interface MemeAsset {
  asset_id: string;
  title?: string;
  enabled?: boolean;
  missing?: boolean;
  animated?: boolean;
  manifest_owned?: boolean;
  usage_scope?: MemeUsageScope;
  dhash?: string;
  preview_url?: string | null;
  width?: number;
  height?: number;
  frames?: number;
  bytes?: number;
  tags?: string[];
  description?: string | null;
  emotions?: string[];
  source?: MemeSourceInfo | null;
  license?: MemeLicenseInfo | null;
}

export interface MemeListResponse {
  items?: MemeAsset[];
}

export interface MemePickerModalProps {
  open: boolean;
  selectedAssetId?: string | null;
  title?: string;
  description?: string;
  emptyText?: string;
  showSources?: boolean;
  resetLabel?: string;
  loadItems?: () => Promise<MemeListResponse>;
  previewUrl?: (asset: MemeAsset) => string;
  accept?: (asset: MemeAsset) => boolean;
  onSelect: (assetId: string | null, asset: MemeAsset | null) => Promise<void> | void;
  onClose: () => void;
}

export const SOURCE_KIND_LABELS: Record<string, string> = {
  existing_user_assets: "已有素材",
  user_upload: "用户导入",
  licensed_pack: "许可素材包",
  generated: "生成图片",
  remote_feed: "远程订阅源",
  bilibili_comment: "B站评论区",
  bilibili_creator: "B站二创追踪",
};

export function usageScopeLabel(scope?: string | null): string {
  return scope === "interaction_only" ? "仅互动" : "全渠道";
}

export function safeExternalUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

const DEFAULT_ACCEPT = () => true;

export function defaultMemePreviewUrl(asset: MemeAsset): string {
  return studioPath(`/api/v1/persona/memes/${encodeURIComponent(asset.asset_id)}/preview`);
}

export function MemeMetadataDetails({ asset }: { asset: MemeAsset }) {
  const source = asset.source || {};
  const license = asset.license || {};
  const sourceKind = (source.kind && SOURCE_KIND_LABELS[source.kind]) || source.kind;
  const sourceUrl = safeExternalUrl(source.url);
  const licenseUrl = safeExternalUrl(license.url);

  return (
    <details className="meme-metadata">
      <summary>来源与许可</summary>
      <p>
        适用范围：{usageScopeLabel(asset.usage_scope)}
      </p>
      <p>
        来源：{source.author || "未提供"}
        {source.author_id ? ` (${source.author_id})` : ""}
        {sourceKind ? ` · ${sourceKind}` : ""}
        {sourceUrl && (
          <>
            {" · "}
            <a href={sourceUrl} target="_blank" rel="noopener noreferrer">
              查看来源
            </a>
          </>
        )}
      </p>
      <p>
        许可：{license.name || "未提供"}
        {licenseUrl && (
          <>
            {" · "}
            <a href={licenseUrl} target="_blank" rel="noopener noreferrer">
              许可说明
            </a>
          </>
        )}
      </p>
      {source.note && <p>来源说明：{source.note}</p>}
      {source.revision && <p>修订：{source.revision}</p>}
      {license.note && <p>许可备注：{license.note}</p>}
    </details>
  );
}

export function MemePreviewBox({
  asset,
  url,
}: {
  asset: MemeAsset;
  url?: string;
}) {
  const [failed, setFailed] = useState(false);
  const resolved = studioPath(url || defaultMemePreviewUrl(asset));

  if (asset.missing || failed || !resolved) {
    return (
      <div className="meme-preview">
        <span className="hint">{asset.missing ? "图片文件缺失" : "图片预览不可用"}</span>
      </div>
    );
  }

  return (
    <div className="meme-preview">
      <img
        loading="lazy"
        alt={asset.title || "表情包"}
        src={resolved}
        onError={() => setFailed(true)}
      />
    </div>
  );
}

export function MemePickerModal({
  open,
  selectedAssetId,
  title = "选择私信表情包",
  description = "显示图库中已启用的图片。选图后仍需审核，最终仅发送这一张图片。",
  emptyText = "图库没有可用表情包，请先在图库启用或导入图片。",
  showSources = false,
  resetLabel,
  loadItems,
  previewUrl = defaultMemePreviewUrl,
  accept = DEFAULT_ACCEPT,
  onSelect,
  onClose,
}: MemePickerModalProps) {
  const headingId = useId();
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const requestSeqRef = useRef(0);
  const [items, setItems] = useState<MemeAsset[]>([]);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [statusText, setStatusText] = useState("");

  const fetchItems = useCallback(async () => {
    const seq = ++requestSeqRef.current;
    setLoading(true);
    setStatusText("正在读取可用图片…");
    setItems([]);
    try {
      const result = loadItems
        ? await loadItems()
        : await apiGet<"/api/v1/persona/memes", MemeListResponse>("/api/v1/persona/memes");
      if (seq !== requestSeqRef.current) return;
      const available = (result.items || []).filter((item) => item.enabled && !item.missing && accept(item));
      setItems(available);
      setStatusText(available.length ? `${available.length} 张可用图片` : emptyText);
    } catch (error) {
      if (seq === requestSeqRef.current) {
        setStatusText(`图库读取失败：${errorMessage(error)}`);
      }
    } finally {
      if (seq === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, [accept, emptyText, loadItems]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (open) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
      void fetchItems();
    } else {
      requestSeqRef.current += 1;
      if (dialog.open) {
        if (typeof dialog.close === "function") {
          dialog.close();
        } else {
          dialog.removeAttribute("open");
        }
      }
    }
  }, [open, fetchItems]);

  async function choose(asset: MemeAsset | null) {
    if (pending) return;
    setPending(true);
    try {
      await onSelect(asset?.asset_id ?? null, asset);
      requestSeqRef.current += 1;
      onClose();
    } catch (error) {
      setStatusText(`选图未保存：${errorMessage(error)}`);
    } finally {
      setPending(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="meme-picker"
      aria-labelledby={headingId}
      onCancel={(event) => {
        if (pending) {
          event.preventDefault();
          return;
        }
        requestSeqRef.current += 1;
        onClose();
      }}
    >
      <div className="panel-heading">
        <h2 id={headingId} data-picker-title="">
          {title}
        </h2>
        <button
          type="button"
          data-picker-close=""
          aria-label="关闭选图"
          disabled={pending}
          onClick={() => {
            if (!pending) onClose();
          }}
        >
          关闭
        </button>
      </div>
      <p className="hint" data-picker-help="">
        {description}
      </p>
      <p data-picker-state="" role="status">
        {statusText}
      </p>
      <div data-picker-grid="" className="meme-grid">
        {items.map((asset) => {
          const choice = (
            <button
              key={asset.asset_id}
              type="button"
              className="meme-choice"
              aria-pressed={asset.asset_id === selectedAssetId}
              aria-label={`选用${asset.title || asset.asset_id}`}
              disabled={pending}
              onClick={() => void choose(asset)}
            >
              <MemePreviewBox asset={asset} url={previewUrl(asset)} />
              <strong>{asset.title || asset.asset_id}</strong>
              <span
                className={asset.usage_scope === "interaction_only" ? "badge amber" : "badge"}
                data-usage-scope={asset.usage_scope || "all_channels"}
              >
                {usageScopeLabel(asset.usage_scope)}
              </span>
              <small>{asset.license?.name || "许可未提供"}</small>
            </button>
          );
          if (showSources) {
            return (
              <article key={asset.asset_id} className="meme-card">
                {choice}
                <MemeMetadataDetails asset={asset} />
              </article>
            );
          }
          return choice;
        })}
      </div>
      <div className="actions">
        <a
          href="#assets"
          data-picker-manage=""
          aria-disabled={pending}
          onClick={(event) => {
            if (pending) {
              event.preventDefault();
              return;
            }
            onClose();
          }}
        >
          管理表情包图库 →
        </a>
        <button
          type="button"
          data-picker-refresh=""
          disabled={pending || loading}
          onClick={() => void fetchItems()}
        >
          重新读取
        </button>
        {resetLabel && (
          <button type="button" disabled={pending} onClick={() => void choose(null)}>
            {resetLabel}
          </button>
        )}
      </div>
    </dialog>
  );
}
