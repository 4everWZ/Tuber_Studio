import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { apiDelete, apiGet, apiPost, apiPut, studioPath } from "@/shared/api/client";
import { useAppSession } from "@/shared/hooks/useAppSession";
import { useUnsavedGuard } from "@/shared/hooks/useUnsavedGuard";
import { list } from "@/shared/lib/display";
import { emitNotice, errorMessage } from "@/shared/lib/errors";
import styles from "./CharactersPage.module.css";
import { RecentEventsDialog, clipSummaryText } from "./RecentEventsDialog";
import {
  characterDisplayName,
  editableCard,
  extractValidationErrors,
  type BilibiliOwnerProfile,
  type CharacterCard,
  type RecentEventItem,
  type RecentEventsResponse,
  type VoiceOption,
} from "./types";

type OcTabId = "tab-oc-persona" | "tab-oc-style" | "tab-oc-assets" | "tab-oc-json";

interface VisualFormState {
  name: string;
  displayName: string;
  ownerAddress: string;
  aliases: string;
  selfDescription: string;
  speakingStyle: string;
  bilibiliOwner: string;
  identity: string;
  background: string;
  traits: string;
  minChars: string;
  maxChars: string;
  catchphrases: string;
  instructions: string;
  voiceProfile: string;
  avatarManifest: string;
}

const EMPTY_VISUAL: VisualFormState = {
  name: "",
  displayName: "",
  ownerAddress: "",
  aliases: "",
  selfDescription: "",
  speakingStyle: "",
  bilibiliOwner: "",
  identity: "",
  background: "",
  traits: "",
  minChars: "1",
  maxChars: "60",
  catchphrases: "",
  instructions: "",
  voiceProfile: "",
  avatarManifest: "",
};

const OC_TABS: ReadonlyArray<{
  tabId: OcTabId;
  buttonId: string;
  label: string;
}> = [
  { tabId: "tab-oc-persona", buttonId: "oc-tab-persona", label: "核心人设与性格" },
  { tabId: "tab-oc-style", buttonId: "oc-tab-style", label: "说话风格与短句示范" },
  { tabId: "tab-oc-assets", buttonId: "oc-tab-assets", label: "音色与形象绑定" },
  { tabId: "tab-oc-json", buttonId: "oc-tab-json", label: "高级 JSON 源码" },
];

const FIELD_ID_BY_SERVER_KEY: Record<string, string> = {
  name: "oc-name",
  display_name: "oc-display-name",
  owner_address: "oc-owner-address",
  bilibili_owner: "oc-bilibili-owner",
  aliases: "oc-aliases",
  self_description: "oc-self-description",
  speaking_style: "oc-speaking-style",
  voice_profile: "oc-voice-profile",
  avatar_manifest: "oc-avatar-manifest",
};

const FIELD_LABEL_BY_SERVER_KEY: Record<string, string> = {
  name: "角色名称",
  self_description: "角色自述",
  speaking_style: "说话风格",
  bilibili_owner: "B 站号主",
  voice_profile: "音色",
  avatar_manifest: "形象素材",
};

const VALID_OWNER_UID = /^[1-9][0-9]{0,19}$/;
const VALID_OWNER_AVATAR = /^https:\/\/i[012]\.hdslb\.com\/bfs\/[\w/.~-]+$/;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function ownerObjectFromCard(owner: unknown): BilibiliOwnerProfile | null {
  if (!isPlainObject(owner)) return null;
  if (typeof owner.uid !== "string" || typeof owner.name !== "string") return null;
  return {
    uid: owner.uid,
    name: owner.name,
    avatar_url: typeof owner.avatar_url === "string" ? owner.avatar_url : null,
  };
}

function visualFromCard(card: CharacterCard): VisualFormState {
  const traits = card.core?.traits;
  const catchphrases = card.style?.catchphrases;
  const instructions = card.style?.instructions;
  const owner = card.bilibili_owner;
  return {
    name: card.name || "",
    displayName: card.display_name || "",
    ownerAddress: card.owner_address || "",
    aliases: Array.isArray(card.aliases) ? card.aliases.join("、") : "",
    selfDescription: card.self_description || "",
    speakingStyle: card.speaking_style || "",
    bilibiliOwner: typeof owner === "string" ? owner : ownerObjectFromCard(owner)?.uid || "",
    identity: card.core?.identity || "",
    background: card.core?.background || "",
    traits: Array.isArray(traits) ? traits.join("\n") : typeof traits === "string" ? traits : "",
    minChars: String(card.style?.min_chars ?? 1),
    maxChars: String(card.style?.max_chars ?? 60),
    catchphrases: Array.isArray(catchphrases)
      ? catchphrases.join("\n")
      : typeof catchphrases === "string"
        ? catchphrases
        : "",
    instructions: Array.isArray(instructions)
      ? instructions.join("\n")
      : typeof instructions === "string"
        ? instructions
        : "",
    voiceProfile: card.voice_profile || "",
    avatarManifest: card.avatar_manifest || "",
  };
}

function applyVisualFieldToCard(
  baseCard: CharacterCard,
  fieldKey: keyof VisualFormState,
  value: string
): CharacterCard {
  const next: CharacterCard = {
    ...editableCard(baseCard),
    core: { ...(baseCard.core || {}) },
    style: { ...(baseCard.style || {}) },
  };
  switch (fieldKey) {
    case "name":
      next.name = value.trim();
      break;
    case "displayName":
      next.display_name = value.trim();
      break;
    case "ownerAddress": {
      const trimmed = value.trim();
      if (trimmed) next.owner_address = trimmed;
      else delete next.owner_address;
      break;
    }
    case "aliases":
      next.aliases = value
        .split(/[、，,]/)
        .map((item) => item.trim())
        .filter(Boolean);
      break;
    case "selfDescription": {
      const trimmed = value.trim();
      if (trimmed) next.self_description = trimmed;
      else delete next.self_description;
      break;
    }
    case "speakingStyle": {
      const trimmed = value.trim();
      if (trimmed) next.speaking_style = trimmed;
      else delete next.speaking_style;
      break;
    }
    case "bilibiliOwner": {
      const trimmed = value.trim();
      const existingUid = ownerObjectFromCard(next.bilibili_owner)?.uid;
      if (trimmed !== existingUid) {
        next.bilibili_owner = trimmed || null;
      }
      break;
    }
    case "identity":
      next.core = { ...next.core, identity: value.trim() };
      break;
    case "background":
      next.core = { ...next.core, background: value.trim() };
      break;
    case "traits":
      next.core = {
        ...next.core,
        traits: value
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
      };
      break;
    case "minChars":
      next.style = { ...next.style, min_chars: Number(value) };
      break;
    case "maxChars":
      next.style = { ...next.style, max_chars: Number(value) };
      break;
    case "catchphrases":
      next.style = {
        ...next.style,
        catchphrases: value
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
      };
      break;
    case "instructions": {
      const trimmed = value.trim();
      next.style = {
        ...next.style,
        instructions: Array.isArray(next.style?.instructions)
          ? trimmed
              .split("\n")
              .map((item) => item.trim())
              .filter(Boolean)
          : trimmed,
      };
      break;
    }
    case "voiceProfile":
      next.voice_profile = value.trim();
      break;
    case "avatarManifest": {
      const trimmed = value.trim();
      if (trimmed) next.avatar_manifest = trimmed;
      else delete next.avatar_manifest;
      break;
    }
  }
  return next;
}

export function CharactersPage() {
  const { status, refreshStatus } = useAppSession();
  const activeCharacterId = status?.character?.id ?? null;

  const [characters, setCharacters] = useState<CharacterCard[]>([]);
  const [deletedCharacters, setDeletedCharacters] = useState<CharacterCard[]>([]);
  const [deletedOpen, setDeletedOpen] = useState(false);
  const [voices, setVoices] = useState<VoiceOption[]>([]);

  const [characterId, setCharacterId] = useState<string | null>(null);
  const [character, setCharacter] = useState<CharacterCard | null>(null);
  const [editorText, setEditorText] = useState("");
  const [visual, setVisual] = useState<VisualFormState>(EMPTY_VISUAL);
  const [activeTab, setActiveTab] = useState<OcTabId>("tab-oc-persona");
  const [ownerPreview, setOwnerPreview] = useState<BilibiliOwnerProfile | null>(null);
  const [ownerStatus, setOwnerStatus] = useState("");
  const [dirtyStatusNote, setDirtyStatusNote] = useState<string | null>(null);
  const [invalidFieldId, setInvalidFieldId] = useState<string | null>(null);

  const [newName, setNewName] = useState("");
  const [cloneName, setCloneName] = useState("");
  const [writing, setWriting] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteTargetId, setDeleteTargetId] = useState<string | null>(null);
  const [replacementId, setReplacementId] = useState("");
  const [deleteError, setDeleteError] = useState("");

  const [recentPreviewText, setRecentPreviewText] = useState("正在读取…");
  const [recentDialogOpen, setRecentDialogOpen] = useState(false);
  const [recentLoading, setRecentLoading] = useState(false);
  const [recentError, setRecentError] = useState("");
  const [recentItems, setRecentItems] = useState<RecentEventItem[]>([]);

  const selectionSeqRef = useRef(0);
  const ownerSeqRef = useRef(0);
  const recentSeqRef = useRef(0);
  const characterRef = useRef<CharacterCard | null>(null);
  const characterIdRef = useRef<string | null>(null);
  const editorTextRef = useRef("");
  const ownerInputValueRef = useRef("");
  const activeStatusIdRef = useRef<string | null>(activeCharacterId);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const deleteDialogRef = useRef<HTMLDialogElement | null>(null);

  characterRef.current = character;
  characterIdRef.current = characterId;
  editorTextRef.current = editorText;
  ownerInputValueRef.current = visual.bilibiliOwner;
  activeStatusIdRef.current = activeCharacterId;

  const baselineJson = character ? JSON.stringify(editableCard(character), null, 2) : "";
  const characterDirty = Boolean(character && editorText !== baselineJson);

  useUnsavedGuard("character-studio", characterDirty);

  const applyCharacterEditor = useCallback((nextCard: CharacterCard) => {
    const serialized = JSON.stringify(editableCard(nextCard), null, 2);
    characterRef.current = nextCard;
    editorTextRef.current = serialized;
    setCharacter(nextCard);
    setEditorText(serialized);
    setVisual(visualFromCard(nextCard));
    setOwnerPreview(ownerObjectFromCard(nextCard.bilibili_owner));
    setOwnerStatus("");
    setInvalidFieldId(null);
    setDirtyStatusNote(null);
  }, []);

  const selectCharacter = useCallback(
    async (id: string) => {
      const requestSeq = ++selectionSeqRef.current;
      const loaded = await apiGet<"/api/v1/persona/characters/{character_id}", CharacterCard>(
        "/api/v1/persona/characters/{character_id}",
        { pathParams: { character_id: id } }
      );
      if (requestSeq !== selectionSeqRef.current) return;
      if (!loaded) throw new Error("角色配置不可用");
      characterIdRef.current = id;
      setCharacterId(id);
      applyCharacterEditor(loaded);
    },
    [applyCharacterEditor]
  );

  const loadCharacters = useCallback(async () => {
    const items = list<CharacterCard>(
      await apiGet<"/api/v1/persona/characters", CharacterCard[]>("/api/v1/persona/characters")
    );
    setCharacters(items);
    const currentId = characterIdRef.current;
    if (!items.some((card) => card.id === currentId) && items.length > 0) {
      const active = items.find((card) => card.id === activeStatusIdRef.current);
      await selectCharacter((active || items[0]).id);
    }
  }, [selectCharacter]);

  const loadDeletedCharacters = useCallback(async () => {
    const items = list<CharacterCard>(
      await apiGet<"/api/v1/persona/characters/deleted", CharacterCard[]>(
        "/api/v1/persona/characters/deleted"
      )
    );
    setDeletedCharacters(items);
  }, []);

  const loadVoices = useCallback(async () => {
    const items = list<VoiceOption>(
      await apiGet<"/api/v1/persona/voices", VoiceOption[]>("/api/v1/persona/voices")
    );
    setVoices(items);
  }, []);

  const loadRecentEvents = useCallback(async ({ openLoad = false } = {}) => {
    const seq = ++recentSeqRef.current;
    if (openLoad) {
      setRecentLoading(true);
      setRecentError("");
    }
    try {
      const data = await apiGet<"/api/v1/persona/daily-memory/recent-events", RecentEventsResponse>(
        "/api/v1/persona/daily-memory/recent-events"
      );
      if (seq !== recentSeqRef.current) return;
      const items = Array.isArray(data.items) ? data.items : [];
      setRecentItems(items);
      const top = items[0];
      setRecentPreviewText(top ? clipSummaryText(top.source.text, 70) : "近期暂无有来源的运营事件");
    } catch (error) {
      if (seq !== recentSeqRef.current) return;
      setRecentPreviewText("读取失败，可打开重试");
      setRecentError(`读取近期趣事失败：${errorMessage(error)}`);
    } finally {
      if (seq === recentSeqRef.current) {
        setRecentLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void Promise.allSettled([
      loadCharacters().catch((error) => emitNotice(errorMessage(error), true)),
      loadVoices().catch(() => {}),
      loadRecentEvents(),
    ]);
  }, [loadCharacters, loadVoices, loadRecentEvents]);

  useEffect(() => {
    const dialog = deleteDialogRef.current;
    if (!dialog) return;
    if (deleteDialogOpen) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
    } else if (dialog.open || dialog.hasAttribute("open")) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [deleteDialogOpen]);

  function focusFieldById(id: string) {
    const targetTab: OcTabId =
      id === "character-editor"
        ? "tab-oc-json"
        : ["oc-min-chars", "oc-max-chars", "oc-catchphrases", "oc-instructions"].includes(id)
          ? "tab-oc-style"
          : ["oc-voice-profile", "oc-avatar-manifest"].includes(id)
            ? "tab-oc-assets"
            : "tab-oc-persona";
    setActiveTab(targetTab);
    setInvalidFieldId(id);
    setTimeout(() => {
      document.getElementById(id)?.focus();
    }, 0);
  }

  function handleTabClick(targetTab: OcTabId) {
    if (targetTab !== "tab-oc-json" && character) {
      try {
        const parsed = JSON.parse(editorText) as CharacterCard;
        if (!isPlainObject(parsed)) throw new SyntaxError("角色配置必须为 JSON 对象");
        setVisual(visualFromCard(parsed));
        setOwnerPreview(ownerObjectFromCard(parsed.bilibili_owner));
      } catch {
        emitNotice("请先修正高级 JSON 的格式，再切换到可视化编辑。", true);
        return;
      }
    }
    setActiveTab(targetTab);
  }

  function handleTabKeyDown(index: number, event: KeyboardEvent<HTMLButtonElement>) {
    const offset = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
    if (!offset) return;
    event.preventDefault();
    const nextIndex = (index + offset + OC_TABS.length) % OC_TABS.length;
    tabRefs.current[nextIndex]?.focus();
    handleTabClick(OC_TABS[nextIndex].tabId);
  }

  function handleVisualInput(fieldKey: keyof VisualFormState, nextValue: string) {
    setVisual((prev) => ({ ...prev, [fieldKey]: nextValue }));
    setInvalidFieldId(null);
    setDirtyStatusNote(null);
    if (!character) return;
    try {
      const parsed = JSON.parse(editorText) as CharacterCard;
      if (!isPlainObject(parsed)) throw new SyntaxError();
      const updated = applyVisualFieldToCard(parsed, fieldKey, nextValue);
      const serialized = JSON.stringify(updated, null, 2);
      editorTextRef.current = serialized;
      setEditorText(serialized);
      if (fieldKey === "bilibiliOwner") {
        setOwnerPreview(ownerObjectFromCard(updated.bilibili_owner));
        setOwnerStatus("");
      }
    } catch {
      if (fieldKey === "bilibiliOwner") {
        setOwnerStatus("请先修正高级 JSON，再修改 B 站号主。");
      }
    }
  }

  function handleEditorTextChange(nextText: string) {
    editorTextRef.current = nextText;
    setEditorText(nextText);
    setInvalidFieldId(null);
    setDirtyStatusNote(null);
    try {
      const parsed = JSON.parse(nextText) as CharacterCard;
      if (isPlainObject(parsed)) {
        setVisual(visualFromCard(parsed));
        setOwnerPreview(ownerObjectFromCard(parsed.bilibili_owner));
      }
    } catch {
      // mid-typing invalid JSON
    }
  }

  async function lookupOwner(useCurrent: boolean) {
    if (!character) return;
    const requestSeq = ++ownerSeqRef.current;
    const account = ownerInputValueRef.current.trim();
    const selSeq = selectionSeqRef.current;
    const currentCharacter = character;

    const isCurrent = () =>
      requestSeq === ownerSeqRef.current &&
      selSeq === selectionSeqRef.current &&
      currentCharacter === characterRef.current &&
      ownerInputValueRef.current.trim() === account;

    if (!useCurrent && !account) {
      setOwnerStatus("请输入 UID 或个人空间链接。");
      document.getElementById("oc-bilibili-owner")?.focus();
      return;
    }

    setInvalidFieldId((prev) => (prev === "oc-bilibili-owner" ? null : prev));
    setOwnerStatus("正在查询 B 站账号…");

    try {
      const owner = await apiGet<
        "/api/v1/persona/characters/{character_id}/bilibili-owner",
        BilibiliOwnerProfile
      >("/api/v1/persona/characters/{character_id}/bilibili-owner", {
        pathParams: { character_id: currentCharacter.id },
        query: useCurrent ? undefined : { account },
      });
      if (!isCurrent()) return;

      let card: CharacterCard;
      try {
        const parsed = JSON.parse(editorTextRef.current) as CharacterCard;
        if (!isPlainObject(parsed)) throw new SyntaxError();
        card = parsed;
      } catch {
        setOwnerStatus("请先修正高级 JSON，再修改 B 站号主。");
        return;
      }

      const nextCard: CharacterCard = { ...card, bilibili_owner: owner };
      const serialized = JSON.stringify(nextCard, null, 2);
      editorTextRef.current = serialized;
      ownerInputValueRef.current = owner.uid;
      setEditorText(serialized);
      setVisual((prev) => ({ ...prev, bilibiliOwner: owner.uid }));
      setOwnerPreview(owner);
      setOwnerStatus("");
      setDirtyStatusNote(null);
    } catch (error) {
      if (!isCurrent()) return;
      setOwnerStatus(errorMessage(error));
      if (!useCurrent) {
        setInvalidFieldId("oc-bilibili-owner");
      }
    }
  }

  function readCardForSave(): CharacterCard {
    let parsed: unknown;
    try {
      parsed = JSON.parse(editorTextRef.current);
    } catch (error) {
      throw Object.assign(new Error(`角色 JSON 格式错误：${errorMessage(error)}`), {
        field: "character-editor",
      });
    }
    if (!isPlainObject(parsed)) {
      throw Object.assign(new Error("角色配置必须为 JSON 对象"), { field: "character-editor" });
    }
    const card = parsed as CharacterCard;
    if (typeof card.name !== "string" || !card.name.trim() || card.name.trim().length > 60) {
      throw Object.assign(new Error("请填写 1 至 60 字的角色名称。"), { field: "oc-name" });
    }
    if (!characterIdRef.current) {
      throw Object.assign(new Error("请先选择角色卡"), { field: "character-editor" });
    }
    return {
      ...editableCard(card),
      id: characterIdRef.current,
      name: card.name.trim(),
    };
  }

  async function saveCard(id: string, card: CharacterCard): Promise<boolean> {
    const selSeq = selectionSeqRef.current;
    const inputAtStart = editorTextRef.current;
    const result = await apiPut<"/api/v1/persona/characters/{character_id}", CharacterCard, CharacterCard>(
      "/api/v1/persona/characters/{character_id}",
      { pathParams: { character_id: id }, body: card }
    );
    const sameEditor = selectionSeqRef.current === selSeq && editorTextRef.current === inputAtStart;
    if (sameEditor) {
      applyCharacterEditor(result);
    } else if (selectionSeqRef.current === selSeq) {
      characterRef.current = result;
      setCharacter(result);
    }
    return sameEditor;
  }

  async function runWrite(key: string, action: () => Promise<void>) {
    if (writing) return;
    setWriting(true);
    setBusyKey(key);
    try {
      await action();
    } catch (error) {
      const validationErrors = extractValidationErrors(error);
      const first = validationErrors[0];
      const fieldError = (error as { field?: string }).field;
      if (fieldError || first) {
        const mappedField = fieldError || (first?.field && FIELD_ID_BY_SERVER_KEY[first.field]) || "character-editor";
        const target =
          mappedField !== "oc-bilibili-owner" && activeTab === "tab-oc-json" ? "character-editor" : mappedField;
        focusFieldById(target);
      }
      const msg = first
        ? `${(first.field && FIELD_LABEL_BY_SERVER_KEY[first.field]) || first.field || "字段"}：${first.message || ""}`
        : errorMessage(error);
      setDirtyStatusNote(msg);
      if (first?.field === "bilibili_owner") {
        setOwnerStatus(first.message || msg);
      }
      emitNotice(msg, true);
    } finally {
      setWriting(false);
      setBusyKey(null);
    }
  }

  async function handleSelectCharacter(id: string) {
    if (characterDirty && !window.confirm("角色配置有未保存修改。放弃修改并切换所选角色？")) {
      return;
    }
    setBusyKey(`select-${id}`);
    try {
      await selectCharacter(id);
    } catch (error) {
      emitNotice(errorMessage(error), true);
    } finally {
      setBusyKey(null);
    }
  }

  async function handleCreateSubmit(event: FormEvent) {
    event.preventDefault();
    await runWrite("create", async () => {
      if (characterDirty && !window.confirm("放弃当前角色的未保存修改并新建角色？")) return;
      const trimmed = newName.trim();
      if (!trimmed) throw new Error("请填写角色名称");
      const created = await apiPost<"/api/v1/persona/characters", CharacterCard, { name: string }>(
        "/api/v1/persona/characters",
        { body: { name: trimmed } }
      );
      setNewName((prev) => (prev.trim() === trimmed ? "" : prev));
      await loadCharacters();
      await selectCharacter(created.id);
      setActiveTab("tab-oc-persona");
      setTimeout(() => document.getElementById("oc-self-description")?.focus(), 0);
      emitNotice("角色卡已创建。");
    });
  }

  async function handleCloneSubmit(event: FormEvent) {
    event.preventDefault();
    if (!characterId) return;
    await runWrite("clone", async () => {
      if (characterDirty && !window.confirm("放弃当前角色的未保存修改并克隆角色？")) return;
      const trimmed = cloneName.trim();
      if (!trimmed) throw new Error("请填写克隆角色名称");
      const cloned = await apiPost<
        "/api/v1/persona/characters/{character_id}/clone",
        CharacterCard,
        { name: string }
      >("/api/v1/persona/characters/{character_id}/clone", {
        pathParams: { character_id: characterId },
        body: { name: trimmed },
      });
      setCloneName("");
      await loadCharacters();
      await selectCharacter(cloned.id);
      setActiveTab("tab-oc-persona");
      emitNotice("角色卡已克隆。");
    });
  }

  async function handleSaveSubmit(event: FormEvent) {
    event.preventDefault();
    if (!characterId) return;
    const targetId = characterId;
    await runWrite("save", async () => {
      await saveCard(targetId, readCardForSave());
      emitNotice("角色已保存。");
      if (activeStatusIdRef.current === targetId) {
        await refreshStatus();
        document.dispatchEvent(new Event("studio-character-change"));
      }
      await loadCharacters();
    });
  }

  async function handleActivateClick() {
    if (!characterId || !character) return;
    const targetId = characterId;
    await runWrite("activate", async () => {
      const card = characterDirty ? readCardForSave() : null;
      const alreadyActive = activeStatusIdRef.current === targetId;
      if (
        !alreadyActive &&
        (status?.current_job || status?.queue_length) &&
        !window.confirm(`切换到「${characterDisplayName(card || character)}」？当前直播待播内容将取消。`)
      ) {
        return;
      }
      if (card && !(await saveCard(targetId, card))) {
        await loadCharacters();
        emitNotice("角色已保存。请在当前编辑完成后激活。");
        return;
      }
      if (!alreadyActive) {
        try {
          await apiPost<"/api/v1/persona/characters/{character_id}/activate", CharacterCard>(
            "/api/v1/persona/characters/{character_id}/activate",
            { pathParams: { character_id: targetId } }
          );
        } catch (error) {
          if (!card) throw error;
          await loadCharacters().catch(() => {});
          throw new Error(`角色已保存，激活失败：${errorMessage(error)}。可重试激活。`);
        }
      }
      await refreshStatus();
      document.dispatchEvent(new Event("studio-character-change"));
      await loadCharacters();
      emitNotice(card && alreadyActive ? "角色已保存。" : "角色已激活。");
    });
  }

  function handleResetClick() {
    if (!character) return;
    if (!characterDirty || window.confirm("放弃未保存的角色修改？")) {
      applyCharacterEditor(character);
    }
  }

  function openDeleteModal() {
    if (writing || !character || !characterId) return;
    setDeleteTargetId(characterId);
    setReplacementId("");
    setDeleteError("");
    setDeleteDialogOpen(true);
  }

  async function handleDeleteConfirm(event: FormEvent) {
    event.preventDefault();
    if (!deleteTargetId) return;
    const targetId = deleteTargetId;
    await runWrite("delete", async () => {
      setDeleteError("");
      try {
        await apiDelete<
          "/api/v1/persona/characters/{character_id}",
          { active_character_id?: string },
          { replacement_id?: string | null }
        >("/api/v1/persona/characters/{character_id}", {
          pathParams: { character_id: targetId },
          body: { replacement_id: replacementId || null },
        });
      } catch (error) {
        setDeleteError(errorMessage(error));
        return;
      }
      setDeleteDialogOpen(false);
      await refreshStatus();
      document.dispatchEvent(new Event("studio-character-change"));
      await loadCharacters();
      if (deletedOpen) {
        await loadDeletedCharacters();
      }
      setTimeout(() => {
        document.querySelector<HTMLButtonElement>("#character-list .selected")?.focus();
      }, 0);
      emitNotice("角色卡已移到「已删除」。");
    });
  }

  async function handleRestore(card: CharacterCard) {
    setBusyKey(`restore-${card.id}`);
    try {
      await apiPost<"/api/v1/persona/characters/{character_id}/restore", CharacterCard>(
        "/api/v1/persona/characters/{character_id}/restore",
        { pathParams: { character_id: card.id } }
      );
      await Promise.all([loadCharacters(), loadDeletedCharacters()]);
      setTimeout(() => {
        document.querySelector<HTMLElement>("#deleted-characters > summary")?.focus();
      }, 0);
      emitNotice(`已恢复 ${characterDisplayName(card)}。`);
    } catch (error) {
      emitNotice(errorMessage(error), true);
    } finally {
      setBusyKey(null);
    }
  }

  const isSelectedActive = Boolean(character && character.id === activeCharacterId);
  const activateLabel = characterDirty
    ? isSelectedActive
      ? "保存修改"
      : "保存并激活"
    : isSelectedActive
      ? "已激活"
      : "激活此角色";

  const canLinkOwner = Boolean(
    ownerPreview && VALID_OWNER_UID.test(ownerPreview.uid) && typeof ownerPreview.name === "string"
  );
  const canShowOwnerAvatar = Boolean(
    canLinkOwner && ownerPreview?.avatar_url && VALID_OWNER_AVATAR.test(ownerPreview.avatar_url)
  );

  const deleteTargetCard = characters.find((c) => c.id === deleteTargetId) || character;
  const deletingActive = Boolean(deleteTargetId && activeCharacterId === deleteTargetId);
  const replacementChoices = characters.filter((c) => c.id !== deleteTargetId);
  const replacementRequired = deletingActive && replacementChoices.length > 0;
  const cannotDeleteSoleActive = deletingActive && replacementChoices.length === 0;

  return (
    <section data-section="characters" className="win-page-view">
      <div className="page-heading win-heading">
        <div className="heading-title-box">
          <h1>角色卡</h1>
          <p className="hint">
            当前角色 ·{" "}
            <strong id="persona-current">
              {characterDisplayName(status?.character) || "待读取"}
            </strong>
          </p>
        </div>
      </div>

      <div className={`character-layout win-split-pane ${styles.layout}`}>
        <article className="win-group-box">
          <div className="win-group-title">切换角色</div>
          <div id="character-list" className={`object-list ${styles.objectList}`}>
            {characters.length === 0 ? (
              <p className="muted">暂无角色配置。</p>
            ) : (
              characters.map((item) => {
                const selected = item.id === characterId;
                const owner = ownerObjectFromCard(item.bilibili_owner);
                return (
                  <button
                    key={item.id}
                    type="button"
                    className={selected ? `selected ${styles.selected}` : undefined}
                    disabled={busyKey === `select-${item.id}`}
                    aria-busy={busyKey === `select-${item.id}` ? "true" : undefined}
                    onClick={() => void handleSelectCharacter(item.id)}
                  >
                    <span className={`character-list-heading ${styles.listHeading}`}>
                      {item.avatar_url && <img src={studioPath(item.avatar_url)} alt="" />}
                      <span>{characterDisplayName(item)}</span>
                      {item.id === activeCharacterId && <small>当前角色</small>}
                    </span>
                    {owner?.name && (
                      <small className={`hint character-list-owner ${styles.listOwner}`}>
                        绑定的 B 站号主 · {owner.name}
                      </small>
                    )}
                  </button>
                );
              })
            )}
          </div>

          <div className={`win-subgroup-box ${styles.subgroupSpacer}`}>
            <label className="win-subgroup-title" htmlFor="oc-bilibili-owner">
              绑定的 B 站号主
            </label>
            <input
              id="oc-bilibili-owner"
              maxLength={2048}
              placeholder="UID 或 B 站个人空间链接"
              aria-describedby="oc-owner-help oc-owner-status"
              aria-invalid={invalidFieldId === "oc-bilibili-owner" ? "true" : undefined}
              autoComplete="off"
              disabled={!character}
              value={visual.bilibiliOwner}
              onChange={(event) => handleVisualInput("bilibiliOwner", event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
                event.preventDefault();
                void lookupOwner(false);
              }}
            />
            <div className="actions" style={{ marginTop: 8 }}>
              <button
                id="oc-owner-lookup"
                type="button"
                className="win-btn"
                disabled={!character}
                onClick={() => void lookupOwner(false)}
              >
                查询账号
              </button>
              <button
                id="oc-owner-current"
                type="button"
                className="win-btn"
                disabled={!character}
                onClick={() => void lookupOwner(true)}
              >
                使用运营登录账号
              </button>
            </div>
            <div className="actions" style={{ marginTop: 8 }}>
              <span id="oc-owner-avatar" className="account-avatar" hidden={!canShowOwnerAvatar}>
                <img
                  src={canShowOwnerAvatar ? ownerPreview?.avatar_url || undefined : undefined}
                  alt=""
                  referrerPolicy="no-referrer"
                />
              </span>
              <a
                id="oc-owner-profile"
                className={styles.ownerProfileLink}
                href={canLinkOwner ? `https://space.bilibili.com/${ownerPreview!.uid}` : undefined}
                target="_blank"
                rel="noopener noreferrer"
                hidden={!canLinkOwner}
              >
                {canLinkOwner ? `${ownerPreview!.name} · UID ${ownerPreview!.uid}` : ""}
              </a>
              <span id="oc-owner-status" className="hint" role="status">
                {ownerStatus}
              </span>
            </div>
            <p id="oc-owner-help" className="hint">
              点击「保存角色」后生效。清空此项并保存可解除绑定。
            </p>
          </div>

          <div className={`win-subgroup-box ${styles.subgroupSpacer}`}>
            <div className="win-subgroup-title">新建角色卡</div>
            <form id="create-character-form" onSubmit={(event) => void handleCreateSubmit(event)}>
              <label>
                新角色名称
                <input
                  id="new-character-name"
                  maxLength={60}
                  required
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                />
              </label>
              <button
                type="submit"
                className="win-btn"
                style={{ marginTop: 8 }}
                disabled={busyKey === "create"}
                aria-busy={busyKey === "create" ? "true" : undefined}
              >
                创建角色卡
              </button>
            </form>
          </div>

          <details className={styles.cloneDetails}>
            <summary>克隆当前角色卡</summary>
            <form id="clone-character-form" onSubmit={(event) => void handleCloneSubmit(event)}>
              <label>
                克隆后的角色名称
                <input
                  id="clone-character-name"
                  maxLength={60}
                  required
                  disabled={!character}
                  value={cloneName}
                  onChange={(event) => setCloneName(event.target.value)}
                />
              </label>
              <button
                id="clone-character"
                type="submit"
                className="win-btn"
                style={{ marginTop: 8 }}
                disabled={!character || writing}
                aria-busy={busyKey === "clone" ? "true" : undefined}
              >
                克隆角色卡
              </button>
            </form>
          </details>

          <details
            id="deleted-characters"
            style={{ marginTop: 16 }}
            onToggle={(event) => {
              const isOpen = event.currentTarget.open;
              setDeletedOpen(isOpen);
              if (isOpen) {
                void loadDeletedCharacters().catch((error) => emitNotice(errorMessage(error), true));
              }
            }}
          >
            <summary>已删除</summary>
            <div id="deleted-character-list" className={`object-list ${styles.objectList}`}>
              {deletedCharacters.length === 0 ? (
                <p className="hint">暂无已删除角色。</p>
              ) : (
                deletedCharacters.map((card) => (
                  <div key={card.id} className="panel-heading">
                    <span>{characterDisplayName(card)}</span>
                    <button
                      type="button"
                      className="win-btn"
                      aria-label={`恢复 ${characterDisplayName(card)}`}
                      disabled={busyKey === `restore-${card.id}`}
                      onClick={() => void handleRestore(card)}
                    >
                      恢复
                    </button>
                  </div>
                ))
              )}
            </div>
          </details>
        </article>

        <article className="win-group-box">
          <div className="panel-heading">
            <div className="win-group-title" id="editor-title" style={{ margin: 0 }}>
              {character ? `编辑角色卡 · ${characterDisplayName(character)}` : "选择角色"}
            </div>
            <button
              id="activate-character"
              type="button"
              className="primary win-btn"
              disabled={!character || writing || (isSelectedActive && !characterDirty)}
              aria-busy={busyKey === "activate" ? "true" : undefined}
              onClick={() => void handleActivateClick()}
            >
              {activateLabel}
            </button>
          </div>

          <div className={`oc-tab-nav win-property-tabs ${styles.tabNav}`} role="tablist" aria-label="角色卡编辑栏目">
            {OC_TABS.map((tab, index) => {
              const selected = activeTab === tab.tabId;
              return (
                <button
                  key={tab.tabId}
                  ref={(el) => {
                    tabRefs.current[index] = el;
                  }}
                  type="button"
                  id={tab.buttonId}
                  className={`oc-tab-btn prop-tab ${styles.tabBtn} ${selected ? `active ${styles.tabBtnActive}` : ""}`}
                  role="tab"
                  aria-selected={selected}
                  aria-controls={tab.tabId}
                  tabIndex={selected ? 0 : -1}
                  data-tab={tab.tabId}
                  onClick={() => handleTabClick(tab.tabId)}
                  onKeyDown={(event) => handleTabKeyDown(index, event)}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>

          <div
            className={`oc-tab-content ${styles.tabContent}${activeTab === "tab-oc-persona" ? " active" : ""}`}
            id="tab-oc-persona"
            role="tabpanel"
            aria-labelledby="oc-tab-persona"
            hidden={activeTab !== "tab-oc-persona"}
          >
            <label>
              人设名称
              <input
                id="oc-name"
                maxLength={60}
                disabled={!character}
                aria-invalid={invalidFieldId === "oc-name" ? "true" : undefined}
                value={visual.name}
                onChange={(event) => handleVisualInput("name", event.target.value)}
              />
            </label>
            <label>
              前台显示名
              <input
                id="oc-display-name"
                maxLength={60}
                disabled={!character}
                aria-describedby="oc-display-help"
                aria-invalid={invalidFieldId === "oc-display-name" ? "true" : undefined}
                value={visual.displayName}
                onChange={(event) => handleVisualInput("displayName", event.target.value)}
              />
            </label>
            <p className="hint" id="oc-display-help">
              用于对话、通话和直播。留空显示角色 ID。
            </p>
            <label>
              对你的称呼
              <input
                id="oc-owner-address"
                maxLength={40}
                placeholder="默认使用绑定 B 站号主昵称"
                disabled={!character}
                aria-invalid={invalidFieldId === "oc-owner-address" ? "true" : undefined}
                value={visual.ownerAddress}
                onChange={(event) => handleVisualInput("ownerAddress", event.target.value)}
              />
            </label>
            <label>
              常用称呼
              <input
                id="oc-aliases"
                maxLength={732}
                placeholder="多个称呼用逗号分隔"
                disabled={!character}
                aria-invalid={invalidFieldId === "oc-aliases" ? "true" : undefined}
                value={visual.aliases}
                onChange={(event) => handleVisualInput("aliases", event.target.value)}
              />
            </label>
            <label>
              角色自述（常驻）
              <input
                id="oc-self-description"
                disabled={!character}
                aria-describedby="oc-profile-help"
                aria-invalid={invalidFieldId === "oc-self-description" ? "true" : undefined}
                value={visual.selfDescription}
                onChange={(event) => handleVisualInput("selfDescription", event.target.value)}
              />
            </label>
            <label>
              说话风格（常驻）
              <input
                id="oc-speaking-style"
                disabled={!character}
                aria-describedby="oc-profile-help"
                aria-invalid={invalidFieldId === "oc-speaking-style" ? "true" : undefined}
                value={visual.speakingStyle}
                onChange={(event) => handleVisualInput("speakingStyle", event.target.value)}
              />
            </label>
            <p className="hint" id="oc-profile-help">
              各用一行填写，最多 120 字。留空时，从已有身份或表达规范取可识别的简短说明。
            </p>
            <label>
              核心身份与世界观 (Identity)
              <textarea
                id="oc-identity"
                className="code-editor"
                rows={4}
                disabled={!character}
                value={visual.identity}
                onChange={(event) => handleVisualInput("identity", event.target.value)}
              />
            </label>
            <label style={{ marginTop: 10 }}>
              背景与生活细节 (Background)
              <textarea
                id="oc-background"
                className="code-editor"
                rows={3}
                disabled={!character}
                value={visual.background}
                onChange={(event) => handleVisualInput("background", event.target.value)}
              />
            </label>
            <label style={{ marginTop: 10 }}>
              性格与动作反应特质 (Traits，每行一条)
              <textarea
                id="oc-traits"
                className="code-editor"
                rows={7}
                disabled={!character}
                value={visual.traits}
                onChange={(event) => handleVisualInput("traits", event.target.value)}
              />
            </label>
          </div>

          <div
            className={`oc-tab-content ${styles.tabContent}${activeTab === "tab-oc-style" ? " active" : ""}`}
            id="tab-oc-style"
            role="tabpanel"
            aria-labelledby="oc-tab-style"
            hidden={activeTab !== "tab-oc-style"}
          >
            <div className="form-grid">
              <label>
                单句台词最短字数 (min_chars)
                <input
                  id="oc-min-chars"
                  type="number"
                  min={1}
                  max={120}
                  disabled={!character}
                  value={visual.minChars}
                  onChange={(event) => handleVisualInput("minChars", event.target.value)}
                />
              </label>
              <label>
                单句台词最长字数 (max_chars)
                <input
                  id="oc-max-chars"
                  type="number"
                  min={1}
                  max={120}
                  disabled={!character}
                  value={visual.maxChars}
                  onChange={(event) => handleVisualInput("maxChars", event.target.value)}
                />
              </label>
            </div>
            <label style={{ marginTop: 10 }}>
              口头禅 (Catchphrases，每行一条)
              <textarea
                id="oc-catchphrases"
                rows={3}
                disabled={!character}
                value={visual.catchphrases}
                onChange={(event) => handleVisualInput("catchphrases", event.target.value)}
              />
            </label>
            <label style={{ marginTop: 10 }}>
              表达规范与去AI味指导语 (Instructions)
              <textarea
                id="oc-instructions"
                className="code-editor"
                rows={4}
                disabled={!character}
                value={visual.instructions}
                onChange={(event) => handleVisualInput("instructions", event.target.value)}
              />
            </label>
          </div>

          <div
            className={`oc-tab-content ${styles.tabContent}${activeTab === "tab-oc-assets" ? " active" : ""}`}
            id="tab-oc-assets"
            role="tabpanel"
            aria-labelledby="oc-tab-assets"
            hidden={activeTab !== "tab-oc-assets"}
          >
            <div className="form-grid">
              <label>
                绑定默认音色 (Voice Profile)
                <input
                  id="oc-voice-profile"
                  list="character-voice-options"
                  disabled={!character}
                  aria-invalid={invalidFieldId === "oc-voice-profile" ? "true" : undefined}
                  value={visual.voiceProfile}
                  onChange={(event) => handleVisualInput("voiceProfile", event.target.value)}
                />
              </label>
              <datalist id="character-voice-options">
                {voices.map((voice) => (
                  <option key={voice.id} value={voice.id}>
                    {voice.name || voice.id}
                  </option>
                ))}
              </datalist>
              <label>
                形象描述清单 (Avatar Manifest)
                <input
                  id="oc-avatar-manifest"
                  placeholder="assets/default/manifest.json"
                  disabled={!character}
                  aria-invalid={invalidFieldId === "oc-avatar-manifest" ? "true" : undefined}
                  value={visual.avatarManifest}
                  onChange={(event) => handleVisualInput("avatarManifest", event.target.value)}
                />
              </label>
            </div>
          </div>

          <div
            className={`oc-tab-content ${styles.tabContent}${activeTab === "tab-oc-json" ? " active" : ""}`}
            id="tab-oc-json"
            role="tabpanel"
            aria-labelledby="oc-tab-json"
            hidden={activeTab !== "tab-oc-json"}
          >
            <form id="character-form" onSubmit={(event) => void handleSaveSubmit(event)}>
              <label>
                角色卡完整底层 JSON
                <textarea
                  id="character-editor"
                  className={`code-editor ${styles.codeEditor}`}
                  rows={18}
                  spellCheck={false}
                  disabled={!character}
                  aria-describedby="character-help"
                  aria-invalid={invalidFieldId === "character-editor" ? "true" : undefined}
                  value={editorText}
                  onChange={(event) => handleEditorTextChange(event.target.value)}
                />
              </label>
              <p id="character-help" className="hint">
                可粘贴完整角色卡。保存时使用当前卡片的内部 ID。
              </p>
            </form>
          </div>

          <div className={`actions ${styles.actionsSpacer}`}>
            <button
              id="save-character"
              type="submit"
              form="character-form"
              className="primary win-btn"
              disabled={!character || writing}
              aria-busy={busyKey === "save" ? "true" : undefined}
            >
              保存角色
            </button>
            <button
              id="reset-character"
              type="button"
              className="win-btn"
              disabled={!character || writing}
              onClick={handleResetClick}
            >
              放弃未保存修改
            </button>
            <button
              id="delete-character"
              type="button"
              className="win-btn danger"
              disabled={!character || writing}
              onClick={openDeleteModal}
            >
              删除角色
            </button>
            <span id="character-dirty" className="hint" role="status">
              {dirtyStatusNote ?? (characterDirty ? "有未保存修改" : "")}
            </span>
          </div>
        </article>
      </div>

      <dialog
        ref={deleteDialogRef}
        id="delete-character-dialog"
        className={`account-dialog ${styles.deleteDialog}`}
        aria-labelledby="delete-character-title"
        onCancel={(event) => {
          if (writing) {
            event.preventDefault();
            return;
          }
          setDeleteDialogOpen(false);
        }}
      >
        <form id="delete-character-form" onSubmit={(event) => void handleDeleteConfirm(event)}>
          <h2 id="delete-character-title">删除角色卡</h2>
          <p id="delete-character-description">
            {cannotDeleteSoleActive
              ? "这是唯一可用的角色卡。请先新建一张替代角色卡。"
              : `删除「${characterDisplayName(deleteTargetCard)}」后可在「已删除」恢复。${
                  characterDirty ? "未保存的修改会丢失。" : ""
                }`}
          </p>
          <label id="replacement-character-label" hidden={!replacementRequired}>
            先激活另一张角色卡
            <select
              id="replacement-character"
              required={replacementRequired}
              value={replacementId}
              onChange={(event) => setReplacementId(event.target.value)}
            >
              {replacementRequired && (
                <option value="" disabled>
                  选择替代角色
                </option>
              )}
              {replacementChoices.map((card) => (
                <option key={card.id} value={card.id}>
                  {characterDisplayName(card)}
                </option>
              ))}
            </select>
          </label>
          <p id="delete-character-error" className="hint" role="alert">
            {deleteError}
          </p>
          <div className="actions">
            <button
              id="cancel-delete-character"
              type="button"
              className="win-btn"
              autoFocus
              disabled={writing}
              onClick={() => setDeleteDialogOpen(false)}
            >
              取消
            </button>
            <button
              id="confirm-delete-character"
              type="submit"
              className="win-btn danger"
              disabled={cannotDeleteSoleActive || writing}
              aria-busy={busyKey === "delete" ? "true" : undefined}
            >
              删除角色卡
            </button>
          </div>
        </form>
      </dialog>

      <div className={`personalization-row recent-memory-preview ${styles.recentPreviewRow}`}>
        <span>
          <strong>近期趣事</strong>
          <small id="recent-memory-preview-text">{recentPreviewText}</small>
        </span>
        <button
          type="button"
          className={`personalization-button ${styles.recentButton}`}
          id="open-role-recent-events"
          onClick={() => {
            setRecentDialogOpen(true);
            void loadRecentEvents({ openLoad: true });
          }}
        >
          查看
        </button>
      </div>

      <RecentEventsDialog
        open={recentDialogOpen}
        loading={recentLoading}
        error={recentError}
        items={recentItems}
        onClose={() => setRecentDialogOpen(false)}
        onRetry={() => void loadRecentEvents({ openLoad: true })}
      />
    </section>
  );
}
