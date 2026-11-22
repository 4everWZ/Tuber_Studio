import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { apiGet, apiPut, studioPath } from "@/shared/api/client";
import { useCharacterDisplay } from "@/shared/hooks/useCharacterDisplay";
import { defaultMemePreviewUrl, MemePickerModal, type MemeAsset, type MemeListResponse } from "@/shared/ui/MemePickerModal";
import type { PetAssetRef, PetCharacterInfo, PetSelectionState } from "./types";

export function hasPetSource(asset: {
  source?: { author?: string | null; url?: string | null } | null;
  license?: { name?: string | null; url?: string | null } | null;
}): boolean {
  return Boolean(
    asset.source?.author?.trim() &&
      asset.license?.name?.trim() &&
      [asset.source?.url, asset.license?.url].every((value) => {
        try {
          return typeof value === "string" && new URL(value).protocol === "https:";
        } catch {
          return false;
        }
      })
  );
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mq.matches);
    update();
    mq.addEventListener?.("change", update);
    return () => mq.removeEventListener?.("change", update);
  }, []);

  return reduced;
}

export interface ChatPetFloatProps {
  character: PetCharacterInfo | null;
  callDialogOpen?: boolean;
  petSlotEl?: HTMLElement | null;
}

export function ChatPetFloat({
  character,
  callDialogOpen = false,
  petSlotEl = null,
}: ChatPetFloatProps) {
  const display = useCharacterDisplay();
  const reducedMotion = useReducedMotion();

  const [pickerOpen, setPickerOpen] = useState(false);
  const [expressionState, setExpressionState] = useState<{
    characterId: string;
    revision: number;
    selected: PetAssetRef | null;
  } | null>(null);
  const [scopeKey, setScopeKey] = useState("");
  const [failedSrc, setFailedSrc] = useState<string | null>(null);

  const nextScopeKey = character
    ? `${character.character_id}/${character.character_version ?? ""}`
    : "";

  useEffect(() => {
    if (nextScopeKey !== scopeKey) {
      setScopeKey(nextScopeKey);
      setPickerOpen(false);
      setExpressionState(null);
    }
  }, [nextScopeKey, scopeKey]);

  const applySelection = useCallback(
    (state: PetSelectionState | null | undefined) => {
      if (!state || !character || state.character_id !== character.character_id) return;
      setExpressionState((prev) => {
        const currentRev = prev && prev.characterId === character.character_id ? prev.revision : 0;
        if (state.revision < currentRev) return prev;
        return {
          characterId: state.character_id,
          revision: state.revision,
          selected: state.selected,
        };
      });
    },
    [character]
  );

  useEffect(() => {
    if (character?.pet) {
      applySelection(character.pet);
    }
  }, [character, applySelection]);

  useEffect(() => {
    if (reducedMotion) {
      setPickerOpen(false);
    }
  }, [reducedMotion]);

  const activeExpression =
    character && expressionState?.characterId === character.character_id
      ? expressionState.selected
      : null;

  const petHidden = !character || !display.value?.pets_enabled;

  let url: string | null | undefined = character?.avatar;
  if (reducedMotion) {
    url =
      character?.avatar_still ||
      (url && !url.toLowerCase().endsWith(".gif") ? url : null);
  }
  if (activeExpression && !(reducedMotion && activeExpression.animated)) {
    url = activeExpression.preview_url;
  }

  const resolvedUrl = url ? studioPath(url) : null;
  const imageHidden = petHidden || !resolvedUrl || failedSrc === resolvedUrl;
  const isPlaceholder = !resolvedUrl || failedSrc === resolvedUrl;

  const loadPetItems = useCallback(async (): Promise<MemeListResponse> => {
    if (!character) return { items: [] };
    return apiGet<"/api/v1/persona/pets/{character_id}", MemeListResponse>(
      "/api/v1/persona/pets/{character_id}",
      { pathParams: { character_id: character.character_id } }
    );
  }, [character]);

  const acceptAsset = useCallback(
    (asset: MemeAsset) =>
      Boolean(asset.manifest_owned || hasPetSource(asset)) && !(reducedMotion && asset.animated),
    [reducedMotion]
  );

  const previewUrl = useCallback(
    (asset: MemeAsset) => asset.preview_url || defaultMemePreviewUrl(asset),
    []
  );

  const handleSelect = useCallback(
    async (assetId: string | null) => {
      if (!character) return;
      const saved = await apiPut<
        "/api/v1/persona/pets/{character_id}",
        PetSelectionState,
        { asset_id: string | null }
      >("/api/v1/persona/pets/{character_id}", {
        pathParams: { character_id: character.character_id },
        body: { asset_id: assetId },
      });
      applySelection(saved);
    },
    [character, applySelection]
  );

  const petButton = (
    <button
      type="button"
      id="chat-pet"
      className={isPlaceholder ? "pet-placeholder" : undefined}
      aria-label="为角色宠物选表情"
      title={activeExpression?.title ? `${activeExpression.title} · 换个表情` : "换个表情"}
      hidden={petHidden}
      onClick={() => setPickerOpen(true)}
    >
      <img
        id="chat-agent-avatar"
        alt=""
        draggable={false}
        hidden={imageHidden}
        src={imageHidden || !resolvedUrl ? undefined : resolvedUrl}
        onError={() => {
          if (resolvedUrl) setFailedSrc(resolvedUrl);
        }}
      />
    </button>
  );

  return (
    <>
      <div className="chat-pet-float">
        {!(callDialogOpen && petSlotEl) && petButton}
      </div>
      {callDialogOpen && petSlotEl ? createPortal(petButton, petSlotEl) : null}
      <MemePickerModal
        open={pickerOpen && Boolean(character)}
        selectedAssetId={activeExpression?.asset_id}
        title="宠物表情"
        showSources
        resetLabel="恢复默认形象"
        description="选择会保存到当前角色，在其他设备打开对话也会使用。"
        loadItems={loadPetItems}
        previewUrl={previewUrl}
        accept={acceptAsset}
        emptyText="当前角色没有符合条件的宠物图片。"
        onSelect={handleSelect}
        onClose={() => setPickerOpen(false)}
      />
    </>
  );
}
