import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { characterDisplayStore } from "@/shared/hooks/useCharacterDisplay";
import { ChatHeaderMenu } from "./ChatHeaderMenu";
import { ChatPetFloat, hasPetSource } from "./ChatPetFloat";

describe("ChatPetFloat", () => {
  beforeEach(() => {
    characterDisplayStore.reset();
    vi.stubGlobal("matchMedia", () => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
  });

  afterEach(() => {
    characterDisplayStore.reset();
    vi.unstubAllGlobals();
  });

  it("pet picker requires attributable assets with source and permission links", () => {
    const asset = {
      source: { author: "creator", url: "https://example.org/art" },
      license: { name: "permission", url: "https://example.org/license" },
    };
    expect(hasPetSource(asset)).toBe(true);
    expect(hasPetSource({ ...asset, source: { ...asset.source, author: "" } })).toBe(false);
    expect(hasPetSource({ ...asset, license: { name: "unknown" } })).toBe(false);
    expect(
      hasPetSource({ ...asset, source: { ...asset.source, url: "javascript:void(0)" } })
    ).toBe(false);
  });

  it("follows chat identity, persists visibility across remount, clears on null character, and skips startup gallery fetch", async () => {
    let saved = { pets_enabled: true, avatar_enabled: false };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/system/settings/performance")) {
        if (init?.method === "PUT" && typeof init.body === "string") {
          saved = { ...saved, ...(JSON.parse(init.body) as Partial<typeof saved>) };
        }
        return new Response(JSON.stringify(saved), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`Unexpected request during startup/pet flow: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await characterDisplayStore.refresh();

    const character = { character_id: "whale", avatar: "/assets/whale/idle.gif" };
    const { container, rerender, unmount } = render(
      <>
        <ChatHeaderMenu
          open
          pending={false}
          character={character}
          onToggleOpen={vi.fn()}
          onClose={vi.fn()}
          onOpenTemporaryChat={vi.fn()}
          onClearChat={vi.fn()}
          onNotify={vi.fn()}
        />
        <ChatPetFloat character={character} />
      </>
    );

    const avatar = container.querySelector<HTMLImageElement>("#chat-agent-avatar")!;
    const petBtn = container.querySelector<HTMLButtonElement>("#chat-pet")!;
    expect(avatar.getAttribute("src")).toBe("/assets/whale/idle.gif");
    expect(petBtn.hidden).toBe(false);

    // Ensure no meme or pet gallery fetch happened on initial render
    const galleryCalls = fetchMock.mock.calls.filter(
      ([u]) => String(u).includes("/memes") || String(u).includes("/pets/")
    );
    expect(galleryCalls).toHaveLength(0);

    // Toggle hide via header menu -> saves via characterDisplayStore and hides #chat-pet
    fireEvent.click(container.querySelector("#chat-pet-toggle")!);
    await waitFor(() => {
      expect(container.querySelector<HTMLButtonElement>("#chat-pet")!.hidden).toBe(true);
    });

    // Simulate reload/remount pulling persisted backend state
    unmount();
    characterDisplayStore.reset();
    await characterDisplayStore.refresh();

    const remounted = render(
      <>
        <ChatHeaderMenu
          open
          pending={false}
          character={character}
          onToggleOpen={vi.fn()}
          onClose={vi.fn()}
          onOpenTemporaryChat={vi.fn()}
          onClearChat={vi.fn()}
          onNotify={vi.fn()}
        />
        <ChatPetFloat character={character} />
      </>
    );
    expect(remounted.container.querySelector<HTMLButtonElement>("#chat-pet")!.hidden).toBe(true);

    // Restore visibility via toggle
    fireEvent.click(remounted.container.querySelector("#chat-pet-toggle")!);
    await waitFor(() => {
      expect(
        remounted.container.querySelector<HTMLImageElement>("#chat-agent-avatar")!.getAttribute("src")
      ).toBe("/assets/whale/idle.gif");
    });

    // Clearing character hides pet and removes image src
    remounted.rerender(<ChatPetFloat character={null} />);
    expect(remounted.container.querySelector<HTMLButtonElement>("#chat-pet")!.hidden).toBe(true);
    expect(
      remounted.container.querySelector<HTMLImageElement>("#chat-agent-avatar")!.getAttribute("src")
    ).toBeNull();
    void rerender;
  });

  it("uses still fallback under reduced motion and portals cleanly into #private-call-pet-slot when call opens", async () => {
    vi.stubGlobal("matchMedia", () => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    await act(async () => {
      characterDisplayStore.publish({ pets_enabled: true, avatar_enabled: false });
    });

    const slot = document.createElement("div");
    slot.id = "private-call-pet-slot";
    document.body.appendChild(slot);

    try {
      const character = {
        character_id: "whale",
        avatar: "/assets/whale/idle.gif",
        avatar_still: "/assets/whale/still.png",
      };

      const { container, rerender } = render(
        <ChatPetFloat character={character} callDialogOpen={false} petSlotEl={slot} />
      );

      expect(container.querySelectorAll("#chat-pet")).toHaveLength(1);
      expect(slot.querySelectorAll("#chat-pet")).toHaveLength(0);
      expect(
        container.querySelector<HTMLImageElement>("#chat-agent-avatar")!.getAttribute("src")
      ).toBe("/assets/whale/still.png");

      rerender(<ChatPetFloat character={character} callDialogOpen petSlotEl={slot} />);
      expect(container.querySelectorAll("#chat-pet")).toHaveLength(0);
      expect(slot.querySelectorAll("#chat-pet")).toHaveLength(1);
      expect(document.querySelectorAll("#chat-pet")).toHaveLength(1);

      rerender(<ChatPetFloat character={character} callDialogOpen={false} petSlotEl={slot} />);
      expect(container.querySelectorAll("#chat-pet")).toHaveLength(1);
      expect(slot.querySelectorAll("#chat-pet")).toHaveLength(0);
    } finally {
      slot.remove();
    }
  });
});
