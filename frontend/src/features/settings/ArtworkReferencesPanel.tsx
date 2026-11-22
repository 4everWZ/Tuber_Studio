import { useCallback, useEffect, useRef, useState } from "react";
import { apiDelete, apiGet, apiPut, studioPath } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { useCharacterDisplay } from "@/shared/hooks/useCharacterDisplay";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import { MemePickerModal, type MemeAsset, type MemeListResponse } from "@/shared/ui/MemePickerModal";
import styles from "./SettingsPage.module.css";
import { characterDisplayName, type ArtworkReferencesResponse } from "./types";

const REFERENCE_VARIANTS: ReadonlyArray<["q" | "mature", string]> = [
  ["q", "Q 版"],
  ["mature", "常规版（mature）"],
];

interface PetOptionsResponse extends MemeListResponse {
  character_id?: string;
  selected_asset_id?: string | null;
}

function hasPetSource(asset: MemeAsset): boolean {
  if (asset.manifest_owned) return true;
  if (!asset.source?.author?.trim() || !asset.license?.name?.trim()) return false;
  return [asset.source.url, asset.license.url].every((value) => {
    if (!value) return false;
    try {
      return new URL(value).protocol === "https:";
    } catch {
      return false;
    }
  });
}

export function CharacterDisplayPanel() {
  const { status } = useAppSession();
  const display = useCharacterDisplay();
  const [petPickerOpen, setPetPickerOpen] = useState(false);
  const [selectedPetId, setSelectedPetId] = useState<string | null>(null);
  const characterId = status?.character?.id ?? null;

  useEffect(() => {
    void display.refresh().catch((error) => emitNotice(errorMessage(error), true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const petsEnabled = display.value?.pets_enabled === true;

  return (
    <fieldset className="win-group-box">
      <legend>角色显示</legend>
      <label className="check-field">
        <input
          type="checkbox"
          data-display-setting="pets_enabled"
          disabled={!display.value || display.saving}
          checked={petsEnabled}
          onChange={(event) => {
            void display.save("pets_enabled", event.target.checked).catch((error) => {
              emitNotice(errorMessage(error), true);
            });
          }}
        />
        {" 显示角色宠物"}
      </label>
      <label className="check-field">
        <input type="checkbox" disabled checked={false} readOnly />
        {" 直播 Avatar（TBD／暂不可用）"}
      </label>
      <p className="hint">宠物开关控制对话、电话和直播中的一只宠物。关闭后仍可通话和播放直播音频。</p>
      {characterId && petsEnabled && (
        <div className={`actions ${styles.displayActions}`}>
          <button
            type="button"
            className="win-btn"
            onClick={() => setPetPickerOpen(true)}
          >
            选择宠物形象
          </button>
        </div>
      )}

      {characterId && (
        <MemePickerModal
          open={petPickerOpen}
          selectedAssetId={selectedPetId}
          title="宠物表情"
          description="选择会保存到当前角色，在其他设备打开对话也会使用。"
          emptyText="当前角色没有符合条件的宠物图片。"
          showSources
          resetLabel="恢复默认形象"
          loadItems={async () => {
            const data = await apiGet<"/api/v1/persona/pets/{character_id}", PetOptionsResponse>(
              "/api/v1/persona/pets/{character_id}",
              { pathParams: { character_id: characterId } }
            );
            setSelectedPetId(data.selected_asset_id ?? null);
            return data;
          }}
          previewUrl={(asset) => asset.preview_url || studioPath(`/api/v1/persona/memes/${encodeURIComponent(asset.asset_id)}/preview`)}
          accept={hasPetSource}
          onSelect={async (assetId) => {
            const result = await apiPut<
              "/api/v1/persona/pets/{character_id}",
              PetOptionsResponse,
              { asset_id: string | null }
            >("/api/v1/persona/pets/{character_id}", {
              pathParams: { character_id: characterId },
              body: { asset_id: assetId },
            });
            setSelectedPetId(result.selected_asset_id ?? null);
            emitNotice("宠物形象已更新。");
          }}
          onClose={() => setPetPickerOpen(false)}
        />
      )}
    </fieldset>
  );
}

export function ArtworkReferencesPanel() {
  const { status } = useAppSession();
  const character = status?.character ?? null;
  const characterId = character?.id ?? null;

  const [data, setData] = useState<ArtworkReferencesResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [resultText, setResultText] = useState("");

  const revisionRef = useRef(0);
  const characterIdRef = useRef<string | null>(characterId);
  const busyRef = useRef(busy);
  characterIdRef.current = characterId;
  busyRef.current = busy;

  const loadReferences = useCallback(async (id: string | null) => {
    const version = ++revisionRef.current;
    setData(null);
    setBusy(false);
    setResultText(id ? "正在读取参考图…" : "");
    if (!id) return;

    try {
      const next = await apiGet<
        "/api/v1/persona/artwork-references/{character_id}",
        ArtworkReferencesResponse
      >("/api/v1/persona/artwork-references/{character_id}", {
        pathParams: { character_id: id },
      });
      if (version !== revisionRef.current || next.character_id !== characterIdRef.current) return;
      setData(next);
      setResultText("");
    } catch (error) {
      if (version === revisionRef.current) {
        setResultText(errorMessage(error));
      }
    }
  }, []);

  useEffect(() => {
    void loadReferences(characterId);
  }, [characterId, loadReferences]);

  async function handleVariantChange(variant: "q" | "mature", file: File | null) {
    if (busyRef.current || !data || !characterId) return;
    if (file && (file.size > 4 * 1024 * 1024 || !["image/png", "image/jpeg"].includes(file.type))) {
      setResultText("请选择不超过 4 MiB 的 PNG 或 JPEG 图片。");
      return;
    }

    const version = ++revisionRef.current;
    busyRef.current = true;
    setBusy(true);
    setResultText(file ? "正在上传参考图…" : "正在恢复默认图…");
    try {
      const next = file
        ? await apiPut<
            "/api/v1/persona/artwork-references/{character_id}/{variant}",
            ArtworkReferencesResponse,
            File
          >("/api/v1/persona/artwork-references/{character_id}/{variant}", {
            pathParams: { character_id: characterId, variant },
            body: file,
          })
        : await apiDelete<
            "/api/v1/persona/artwork-references/{character_id}/{variant}",
            ArtworkReferencesResponse
          >("/api/v1/persona/artwork-references/{character_id}/{variant}", {
            pathParams: { character_id: characterId, variant },
          });

      if (version !== revisionRef.current || next.character_id !== characterIdRef.current) return;
      setData(next);
      setResultText(file ? "参考图已更新。" : "已恢复默认设置。");
    } catch (error) {
      if (version === revisionRef.current) {
        setResultText(errorMessage(error));
      }
    } finally {
      if (version === revisionRef.current) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  }

  return (
    <fieldset className="win-group-box" id="artwork-references">
      <legend>生图参考图</legend>
      <p className="hint" id="artwork-reference-scope">
        {characterId
          ? `${characterDisplayName(character)} · PNG 或 JPEG，最多 4 MiB、800 万像素。`
          : "等待读取当前角色。"}
      </p>

      <div className="form-grid" id="artwork-reference-variants">
        {REFERENCE_VARIANTS.map(([variant, labelText]) => {
          const ref = data?.variants?.[variant];
          const available = Boolean(ref?.available && ref.preview_url);
          const previewHref = available && ref?.preview_url ? studioPath(ref.preview_url) : undefined;
          const thumbHref =
            available && (ref?.thumbnail_url || ref?.preview_url)
              ? studioPath(ref.thumbnail_url || ref.preview_url || "")
              : undefined;
          const statusLabel = !ref
            ? ""
            : ref.source === "upload"
              ? "已上传"
              : ref.available
                ? "默认图"
                : "暂无默认图";

          return (
            <fieldset key={variant}>
              <legend>{labelText}</legend>
              <a
                href={previewHref}
                target="_blank"
                rel="noopener"
                hidden={!available}
              >
                <img
                  src={thumbHref}
                  alt={`${labelText}参考图（打开原图）`}
                  width={112}
                />
              </a>
              <p>{statusLabel}</p>
              <label>
                上传替换
                <input
                  type="file"
                  accept="image/png,image/jpeg"
                  disabled={!data || busy}
                  onChange={(event) => {
                    const file = event.target.files?.[0] || null;
                    event.target.value = "";
                    if (file) void handleVariantChange(variant, file);
                  }}
                />
              </label>
              <button
                type="button"
                className="win-btn"
                style={{ marginTop: 8 }}
                disabled={busy || ref?.source !== "upload"}
                onClick={() => {
                  if (window.confirm(`移除当前角色的${labelText}自定义参考图并恢复默认？`)) {
                    void handleVariantChange(variant, null);
                  }
                }}
              >
                恢复默认
              </button>
            </fieldset>
          );
        })}
      </div>

      <p className="hint" id="artwork-reference-result" role="status">
        {resultText}
      </p>
    </fieldset>
  );
}
