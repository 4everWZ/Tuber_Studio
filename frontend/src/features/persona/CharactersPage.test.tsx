import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppSessionProvider, type AppSessionValue } from "@/shared/hooks/useAppSession";
import { clearDirtyRegistry, hasAnyUnsavedChanges } from "@/shared/lib/dirtyRegistry";
import { CharactersPage } from "./CharactersPage";
import type { CharacterCard } from "./types";

const WHALE_FIXTURE: CharacterCard = {
  id: "whale",
  name: "大肥鱼",
  display_name: "大肥鱼",
  aliases: ["鱼鱼", "鲸鲸"],
  self_description: "我是蓝发鲸鱼娘。",
  speaking_style: "说话随和。",
  core: {
    identity: "你是蓝发鲸鱼娘。",
    background: "住在海底小屋。",
    traits: ["嘴硬心软", "白饭执念"],
    variants: {
      radio: { name: "深夜电台", prompt: "轻柔" },
    },
  },
  style: {
    min_chars: 1,
    max_chars: 120,
    catchphrases: ["本鱼"],
    instructions: "对白口语化。",
  },
  voice_profile: "whale_reference",
  avatar_manifest: "assets/whale/manifest.json",
  version: 2,
};

function makeSession(overrides: Partial<AppSessionValue> = {}): AppSessionValue {
  return {
    authenticated: true,
    localAccess: true,
    wsConnected: true,
    status: {
      state: "stopped",
      character: { id: "whale", display_name: "大肥鱼", version: 2 },
    },
    settings: { local_access: true },
    refreshStatus: vi.fn(async () => null),
    refreshSettings: vi.fn(async () => null),
    navigate: vi.fn(),
    logout: vi.fn(async () => {}),
    onUnauthorized: vi.fn(),
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("CharactersPage", () => {
  beforeEach(() => {
    clearDirtyRegistry();
  });

  afterEach(() => {
    clearDirtyRegistry();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("switches OC tabs including keyboard navigation and preserves rest of saved persona on visual edits", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/persona/characters")) {
          return Response.json([WHALE_FIXTURE]);
        }
        if (url.endsWith("/api/v1/persona/characters/whale")) {
          return Response.json({ ...WHALE_FIXTURE });
        }
        if (url.endsWith("/api/v1/persona/voices")) {
          return Response.json([{ id: "whale_reference", name: "大肥鱼参考音色" }]);
        }
        if (url.endsWith("/api/v1/persona/daily-memory/recent-events")) {
          return Response.json({ scope: "s1", items: [] });
        }
        return Response.json({});
      })
    );

    render(
      <AppSessionProvider value={makeSession({ status: { state: "stopped", character: { id: "other" } } })}>
        <CharactersPage />
      </AppSessionProvider>
    );

    const bgInput = await screen.findByLabelText(/背景与生活细节/);
    await waitFor(() => expect(bgInput).toHaveValue("住在海底小屋。"));

    const personaTab = screen.getByRole("tab", { name: "核心人设与性格" });
    const styleTab = screen.getByRole("tab", { name: "说话风格与短句示范" });
    const assetsTab = screen.getByRole("tab", { name: "音色与形象绑定" });
    const jsonTab = screen.getByRole("tab", { name: "高级 JSON 源码" });

    personaTab.focus();
    fireEvent.keyDown(personaTab, { key: "ArrowRight" });
    expect(styleTab).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("tab-oc-style")?.hidden).toBe(false);
    expect(document.getElementById("tab-oc-persona")?.hidden).toBe(true);

    await user.click(assetsTab);
    expect(assetsTab).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("tab-oc-assets")?.hidden).toBe(false);

    await user.click(jsonTab);
    expect(jsonTab).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("tab-oc-json")?.hidden).toBe(false);

    await user.click(personaTab);
    fireEvent.change(bgInput, { target: { value: "新背景" } });

    const editor = document.getElementById("character-editor") as HTMLTextAreaElement;
    const saved = JSON.parse(editor.value) as CharacterCard;
    expect(saved.core?.background).toBe("新背景");
    expect(saved.core?.traits).toEqual(WHALE_FIXTURE.core?.traits);
    expect(saved.style).toEqual(WHALE_FIXTURE.style);
    expect(saved.core?.variants).toEqual(WHALE_FIXTURE.core?.variants);

    const nameInput = document.getElementById("oc-name") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "新名字" } });
    expect((JSON.parse(editor.value) as CharacterCard).name).toBe("新名字");
    expect(document.getElementById("activate-character")?.textContent).toBe("保存并激活");
    expect(hasAnyUnsavedChanges()).toBe(true);
  });

  it("binds verified Bilibili owner profile and removes links and avatar when cleared", async () => {
    const user = userEvent.setup();
    const owner = { uid: "123", name: "号主昵称", avatar_url: "https://i0.hdslb.com/bfs/face/example.jpg" };
    const bareCard: CharacterCard = { id: "whale", name: "大肥鱼", core: {}, style: {} };

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/persona/characters")) return Response.json([bareCard]);
      if (url.endsWith("/api/v1/persona/characters/whale")) return Response.json(bareCard);
      if (url.includes("/api/v1/persona/characters/whale/bilibili-owner")) {
        expect(url).toContain("account=https%3A%2F%2Fspace.bilibili.com%2F123");
        return Response.json(owner);
      }
      if (url.endsWith("/api/v1/persona/voices")) return Response.json([]);
      if (url.endsWith("/api/v1/persona/daily-memory/recent-events")) {
        return Response.json({ scope: "s1", items: [] });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AppSessionProvider value={makeSession()}>
        <CharactersPage />
      </AppSessionProvider>
    );

    const ownerInput = (await screen.findByLabelText("绑定的 B 站号主")) as HTMLInputElement;
    await waitFor(() => expect(ownerInput).not.toBeDisabled());

    const profileLink = document.getElementById("oc-owner-profile") as HTMLAnchorElement;
    const avatarWrap = document.getElementById("oc-owner-avatar") as HTMLElement;
    const avatarImg = avatarWrap.querySelector("img") as HTMLImageElement;
    expect(profileLink.hidden).toBe(true);
    expect(avatarWrap.hidden).toBe(true);

    await user.type(ownerInput, "https://space.bilibili.com/123");
    await user.click(document.getElementById("oc-owner-lookup")!);

    await waitFor(() => expect(profileLink.hidden).toBe(false));
    const editor = document.getElementById("character-editor") as HTMLTextAreaElement;
    expect((JSON.parse(editor.value) as CharacterCard).bilibili_owner).toEqual(owner);
    expect(ownerInput.value).toBe("123");
    expect(profileLink.textContent).toBe("号主昵称 · UID 123");
    expect(profileLink.getAttribute("href")).toBe("https://space.bilibili.com/123");
    expect(avatarWrap.hidden).toBe(false);
    expect(avatarImg.getAttribute("src")).toBe(owner.avatar_url);
    expect(document.getElementById("character-dirty")?.textContent).toBe("有未保存修改");

    fireEvent.change(ownerInput, { target: { value: "" } });
    expect((JSON.parse(editor.value) as CharacterCard).bilibili_owner).toBeNull();
    expect(profileLink.hidden).toBe(true);
    expect(profileLink.getAttribute("href")).toBeNull();
    expect(avatarWrap.hidden).toBe(true);
    expect(avatarImg.getAttribute("src")).toBeNull();
    expect(ownerInput.getAttribute("aria-invalid")).toBeNull();
    expect(document.getElementById("oc-owner-status")?.textContent).toBe("");
  });

  it("editing and clearing a resident field preserves the complete legacy card", async () => {
    const legacyCard: CharacterCard = {
      id: "old",
      name: "自定义",
      core: { identity: "旧身份".repeat(40), background: "完整的旧世界观" },
      style: { instructions: ["说话轻柔", "遇到难题时先听对方说完"] },
      lore: [{ text: "旧设定" }],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/persona/characters")) return Response.json([legacyCard]);
        if (url.endsWith("/api/v1/persona/characters/old")) return Response.json(legacyCard);
        if (url.endsWith("/api/v1/persona/voices")) return Response.json([]);
        if (url.endsWith("/api/v1/persona/daily-memory/recent-events")) {
          return Response.json({ scope: "s1", items: [] });
        }
        return Response.json({});
      })
    );

    render(
      <AppSessionProvider value={makeSession({ status: { state: "stopped", character: { id: "old" } } })}>
        <CharactersPage />
      </AppSessionProvider>
    );

    const selfInput = (await screen.findByLabelText("角色自述（常驻）")) as HTMLInputElement;
    const styleInput = screen.getByLabelText("说话风格（常驻）") as HTMLInputElement;
    const identityInput = document.getElementById("oc-identity") as HTMLTextAreaElement;
    const instructionsInput = document.getElementById("oc-instructions") as HTMLTextAreaElement;
    const editor = document.getElementById("character-editor") as HTMLTextAreaElement;

    await waitFor(() => expect(identityInput.value).toBe(legacyCard.core!.identity));
    expect(selfInput.value).toBe("");
    expect(styleInput.value).toBe("");
    expect(instructionsInput.value).toBe((legacyCard.style!.instructions as string[]).join("\n"));

    fireEvent.change(selfInput, { target: { value: "我是图书管理员。" } });
    fireEvent.change(styleInput, { target: { value: "说话简洁。" } });
    const updated = { ...legacyCard, self_description: "我是图书管理员。", speaking_style: "说话简洁。" };
    expect(JSON.parse(editor.value)).toEqual(updated);

    fireEvent.change(editor, {
      target: { value: JSON.stringify({ ...updated, speaking_style: "温和" }, null, 2) },
    });
    expect(styleInput.value).toBe("温和");

    fireEvent.change(selfInput, { target: { value: "" } });
    expect(JSON.parse(editor.value)).toEqual({ ...legacyCard, speaking_style: "温和" });
  });

  it("discards out-of-order character GET responses and uses character ID when display_name is absent", async () => {
    const user = userEvent.setup();
    const firstSlow = deferred<Response>();
    const owner = { uid: "123", name: "当前号主", avatar_url: "https://i1.hdslb.com/bfs/face/current.jpg" };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/persona/characters")) {
          return Response.json([
            { id: "first", name: "第一角色" },
            { id: "second", name: "第二角色", display_name: "第二位" },
            { id: "third", name: "未绑定角色" },
          ]);
        }
        if (url.endsWith("/api/v1/persona/characters/first")) {
          return firstSlow.promise;
        }
        if (url.endsWith("/api/v1/persona/characters/second")) {
          return Response.json({
            id: "second",
            name: "第二角色",
            display_name: "第二位",
            version: 1,
            bilibili_owner: owner,
          });
        }
        if (url.endsWith("/api/v1/persona/characters/third")) {
          return Response.json({ id: "third", name: "未绑定角色", version: 1 });
        }
        if (url.endsWith("/api/v1/persona/voices")) return Response.json([]);
        if (url.endsWith("/api/v1/persona/daily-memory/recent-events")) {
          return Response.json({ scope: "s1", items: [] });
        }
        return Response.json({});
      })
    );

    render(
      <AppSessionProvider value={makeSession({ status: { state: "stopped", character: { id: "first" } } })}>
        <CharactersPage />
      </AppSessionProvider>
    );

    const listEl = await waitFor(() => {
      const el = document.getElementById("character-list");
      expect(el?.querySelectorAll("button").length).toBe(3);
      return el!;
    });

    const buttons = listEl.querySelectorAll("button");
    // Initial auto-selection of active "first" is pending; click "second" before "first" resolves
    await user.click(buttons[1]);
    await waitFor(() => {
      expect(document.getElementById("editor-title")?.textContent).toBe("编辑角色卡 · 第二位");
    });

    // Late resolution of "first" must be discarded
    firstSlow.resolve(
      Response.json({
        id: "first",
        name: "第一角色",
        version: 1,
        bilibili_owner: { uid: "456", name: "旧号主" },
      })
    );
    await new Promise((r) => setTimeout(r, 10));

    expect(document.getElementById("editor-title")?.textContent).toBe("编辑角色卡 · 第二位");
    const editor = document.getElementById("character-editor") as HTMLTextAreaElement;
    expect(Object.hasOwn(JSON.parse(editor.value) as object, "version")).toBe(false);

    const ownerProfile = document.getElementById("oc-owner-profile") as HTMLAnchorElement;
    const ownerAvatar = document.getElementById("oc-owner-avatar") as HTMLElement;
    expect(ownerProfile.textContent).toBe("当前号主 · UID 123");
    expect(ownerProfile.getAttribute("href")).toBe("https://space.bilibili.com/123");
    expect(ownerAvatar.querySelector("img")?.getAttribute("src")).toBe(owner.avatar_url);

    // Switch to "third" (no display_name, no owner)
    await user.click(buttons[2]);
    await waitFor(() => {
      expect(document.getElementById("editor-title")?.textContent).toBe("编辑角色卡 · third");
    });
    expect(ownerProfile.hidden).toBe(true);
    expect(ownerProfile.getAttribute("href")).toBeNull();
    expect(ownerAvatar.hidden).toBe(true);
    expect(ownerAvatar.querySelector("img")?.getAttribute("src")).toBeNull();
  });

  it("retains selection and unsaved edits when canceling a character switch, and supports create/clone/delete/restore", async () => {
    const user = userEvent.setup();
    const cards: CharacterCard[] = [
      { id: "whale", name: "大肥鱼", display_name: "大肥鱼", core: { background: "海底" }, style: {} },
      { id: "radio", name: "深夜电台", display_name: "深夜电台", core: {}, style: {} },
    ];
    let deleted: CharacterCard[] = [];
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        if (url.endsWith("/api/v1/persona/characters") && method === "GET") return Response.json(cards);
        if (url.endsWith("/api/v1/persona/characters") && method === "POST") {
          const body = JSON.parse(String(init?.body)) as { name: string };
          const created: CharacterCard = { id: "oc-new", name: body.name, core: {}, style: {} };
          cards.push(created);
          return Response.json(created);
        }
        if (url.endsWith("/api/v1/persona/characters/deleted")) return Response.json(deleted);
        if (url.endsWith("/api/v1/persona/characters/whale") && method === "GET") {
          return Response.json(cards[0]);
        }
        if (url.endsWith("/api/v1/persona/characters/whale/clone") && method === "POST") {
          const body = JSON.parse(String(init?.body)) as { name: string };
          const cloned: CharacterCard = { id: "oc-clone", name: body.name, core: { ...cards[0].core }, style: {} };
          cards.push(cloned);
          return Response.json(cloned);
        }
        if (url.endsWith("/api/v1/persona/characters/oc-clone") && method === "GET") {
          return Response.json(cards.find((c) => c.id === "oc-clone"));
        }
        if (url.endsWith("/api/v1/persona/characters/oc-new") && method === "GET") {
          return Response.json(cards.find((c) => c.id === "oc-new"));
        }
        if (url.endsWith("/api/v1/persona/characters/oc-clone") && method === "DELETE") {
          const idx = cards.findIndex((c) => c.id === "oc-clone");
          if (idx >= 0) deleted.push(cards.splice(idx, 1)[0]);
          return Response.json({ active_character_id: "whale" });
        }
        if (url.endsWith("/api/v1/persona/characters/oc-clone/restore") && method === "POST") {
          const restored = deleted[0];
          deleted = [];
          cards.push(restored);
          return Response.json(restored);
        }
        if (url.endsWith("/api/v1/persona/voices")) return Response.json([]);
        if (url.endsWith("/api/v1/persona/daily-memory/recent-events")) {
          return Response.json({ scope: "s1", items: [] });
        }
        return Response.json({});
      })
    );

    render(
      <AppSessionProvider value={makeSession()}>
        <CharactersPage />
      </AppSessionProvider>
    );

    const bgInput = (await screen.findByLabelText(/背景与生活细节/)) as HTMLTextAreaElement;
    await waitFor(() => expect(bgInput.value).toBe("海底"));

    fireEvent.change(bgInput, { target: { value: "还未保存的背景" } });
    const listButtons = document.getElementById("character-list")!.querySelectorAll("button");
    await user.click(listButtons[1]);

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(listButtons[0]).toHaveClass("selected");
    expect(bgInput.value).toBe("还未保存的背景");
    expect(document.getElementById("character-dirty")?.textContent).toBe("有未保存修改");

    // Reset dirty edits and test clone -> delete -> restore
    confirmSpy.mockReturnValue(true);
    await user.click(document.getElementById("reset-character")!);
    expect(bgInput.value).toBe("海底");

    await user.click(screen.getByText("克隆当前角色卡"));
    await user.type(document.getElementById("clone-character-name")!, "分身鱼");
    await user.click(document.getElementById("clone-character")!);
    await waitFor(() => {
      expect(document.getElementById("editor-title")?.textContent).toBe("编辑角色卡 · oc-clone");
    });

    await user.click(document.getElementById("delete-character")!);
    await user.click(document.getElementById("confirm-delete-character")!);
    await waitFor(() => {
      expect(document.getElementById("editor-title")?.textContent).toBe("编辑角色卡 · 大肥鱼");
    });

    const deletedSummary = document.querySelector("#deleted-characters > summary") as HTMLElement;
    await user.click(deletedSummary);
    const restoreBtn = await screen.findByRole("button", { name: "恢复 oc-clone" });
    await user.click(restoreBtn);
    await waitFor(() => {
      expect(screen.getByText("暂无已删除角色。")).toBeInTheDocument();
    });
  });
});
