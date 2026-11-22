import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AssetsPage } from "./AssetsPage";

interface StubMeme {
  asset_id: string;
  title: string;
  enabled: boolean;
  missing?: boolean;
  animated?: boolean;
  frames?: number;
  width?: number;
  height?: number;
  bytes?: number;
  emotions?: string[];
  tags?: string[];
  description?: string;
  preview_url?: string;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("MemeGallery & AssetsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists, filters, edits labels, toggles enabled state, and deletes a disabled meme", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);

    let memes: StubMeme[] = [
      {
        asset_id: "meme-1",
        title: "开心摸鱼",
        enabled: true,
        animated: true,
        frames: 12,
        width: 256,
        height: 256,
        bytes: 4096,
        emotions: ["cheerful"],
        tags: ["摸鱼", "开心"],
        description: "举手欢呼",
        preview_url: "/api/v1/persona/memes/meme-1/preview",
      },
      {
        asset_id: "meme-2",
        title: "害羞躲角落",
        enabled: false,
        animated: false,
        width: 128,
        height: 128,
        bytes: 2048,
        emotions: ["shy"],
        tags: ["害羞"],
        preview_url: "/api/v1/persona/memes/meme-2/preview",
      },
    ];

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/persona/memes" && method === "GET") {
        return jsonResponse({ items: memes });
      }
      if (url === "/api/v1/persona/assets" && method === "GET") {
        return jsonResponse([
          {
            id: "audio-intro",
            kind: "audio",
            url: "/assets/intro.wav",
            tag: "开场白",
            character_id: "fish",
            license_status: "原创",
          },
        ]);
      }
      if (url === "/api/v1/persona/memes/meme-2/labels" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        memes = memes.map((item) =>
          item.asset_id === "meme-2"
            ? { ...item, tags: body.tags, description: body.description }
            : item
        );
        return jsonResponse(memes.find((m) => m.asset_id === "meme-2"));
      }
      if (url === "/api/v1/persona/memes/meme-1" && method === "PUT") {
        const body = JSON.parse(String(init?.body || "{}"));
        memes = memes.map((item) =>
          item.asset_id === "meme-1" ? { ...item, enabled: body.enabled } : item
        );
        return jsonResponse(memes.find((m) => m.asset_id === "meme-1"));
      }
      if (url === "/api/v1/persona/memes/meme-1" && method === "DELETE") {
        memes = memes.filter((item) => item.asset_id !== "meme-1");
        return jsonResponse({ deleted: true });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(<AssetsPage />);

    await waitFor(() => {
      expect(container.querySelectorAll(".meme-card").length).toBe(2);
    });
    expect(container.querySelector("[data-meme-state]")?.textContent).toBe("2 张图片 · 1 张可用");
    expect(container.querySelector("#asset-grid audio")).not.toBeNull();

    // Filter by tag
    const filterInput = container.querySelector<HTMLInputElement>("[data-meme-filter]")!;
    fireEvent.change(filterInput, { target: { value: "害羞" } });
    expect(container.querySelectorAll(".meme-card").length).toBe(1);
    expect(container.querySelector(".meme-card h3")?.textContent).toBe("害羞躲角落");

    // Edit labels on meme-2
    const card2 = container.querySelector<HTMLElement>(".meme-card")!;
    fireEvent.change(card2.querySelector<HTMLInputElement>('input[name="tags"]')!, {
      target: { value: "害羞，脸红" },
    });
    fireEvent.change(card2.querySelector<HTMLTextAreaElement>('textarea[name="description"]')!, {
      target: { value: "缩在气泡里" },
    });
    fireEvent.submit(card2.querySelector<HTMLFormElement>("form")!);
    await waitFor(() => {
      expect(container.querySelector("[data-meme-feedback]")?.textContent).toBe(
        "已保存「害羞躲角落」的标签与描述。"
      );
    });
    expect(card2.textContent).toContain("害羞 · 脸红");
    expect(card2.textContent).toContain("缩在气泡里");

    // Clear filter and verify delete guard on enabled meme-1 -> disable -> delete
    fireEvent.change(filterInput, { target: { value: "" } });
    expect(container.querySelectorAll(".meme-card").length).toBe(2);

    const card1 = container.querySelectorAll<HTMLElement>(".meme-card")[0];
    const toggleBtn = screen.getAllByRole("button", { name: "禁用" })[0];
    const deleteBtn1 = card1.querySelector<HTMLButtonElement>("button.danger")!;
    expect(deleteBtn1.disabled).toBe(true);

    fireEvent.click(toggleBtn);
    await waitFor(() => {
      expect(container.querySelector("[data-meme-feedback]")?.textContent).toBe("已禁用此表情包。");
    });

    const refreshedCard1 = container.querySelectorAll<HTMLElement>(".meme-card")[0];
    const deleteBtnUnlocked = refreshedCard1.querySelector<HTMLButtonElement>("button.danger")!;
    expect(deleteBtnUnlocked.disabled).toBe(false);
    fireEvent.click(deleteBtnUnlocked);

    await waitFor(() => {
      expect(container.querySelectorAll(".meme-card").length).toBe(1);
    });
    expect(container.querySelector("[data-meme-feedback]")?.textContent).toBe("图片已从图库删除。");
  });

  it("uploads a multi-file batch with import_id and retries only failed items", async () => {
    const uploadedPayloads: Array<{ title: string; import_id: string }> = [];
    let failSecondOnce = true;
    const uploadedItems: StubMeme[] = [];

    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method || "GET").toUpperCase();
      if (url === "/api/v1/persona/memes" && method === "GET") {
        return jsonResponse({ items: uploadedItems });
      }
      if (url === "/api/v1/persona/assets" && method === "GET") {
        return jsonResponse([]);
      }
      if (url === "/api/v1/persona/memes" && method === "POST") {
        const body = JSON.parse(String(init?.body || "{}"));
        uploadedPayloads.push({ title: body.title, import_id: body.import_id });
        if (body.title === "second" && failSecondOnce) {
          failSecondOnce = false;
          return new Response(JSON.stringify({ code: "ERROR", message: "尺寸超出限制" }), {
            status: 422,
            headers: { "Content-Type": "application/json" },
          });
        }
        uploadedItems.push({
          asset_id: `uploaded-${uploadedItems.length + 1}`,
          title: body.title,
          enabled: true,
          emotions: body.emotions,
        });
        return jsonResponse(uploadedItems[uploadedItems.length - 1]);
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchSpy);

    const { container } = render(<AssetsPage />);
    await waitFor(() => {
      expect(container.querySelector("[data-meme-state]")?.textContent).toBe("0 张图片 · 0 张可用");
    });

    const uploadForm = container.querySelector<HTMLFormElement>("[data-meme-upload]")!;
    const fileInput = uploadForm.querySelector<HTMLInputElement>('input[name="file"]')!;
    const file1 = new File(["img1"], "first.png", { type: "image/png" });
    const file2 = new File(["img2"], "second.png", { type: "image/png" });

    fireEvent.change(fileInput, { target: { files: [file1, file2] } });
    fireEvent.change(uploadForm.querySelector<HTMLInputElement>('input[name="author"]')!, {
      target: { value: "画师A" },
    });
    fireEvent.change(uploadForm.querySelector<HTMLInputElement>('input[name="license_name"]')!, {
      target: { value: "CC-BY-4.0" },
    });

    fireEvent.submit(uploadForm);

    await waitFor(() => {
      expect(container.querySelector("[data-meme-feedback]")?.textContent).toContain(
        "已导入 1 张，1 张失败。再次上传时只重试失败项。"
      );
    });
    expect(container.querySelector("[data-meme-failures]")?.textContent).toContain(
      "second.png · 尺寸超出限制"
    );
    expect(uploadedPayloads.map((p) => p.title)).toEqual(["first", "second"]);
    const secondImportId = uploadedPayloads[1].import_id;

    // Retry submit -> should ONLY re-POST "second" with the exact same import_id
    fireEvent.submit(uploadForm);
    await waitFor(() => {
      expect(container.querySelector("[data-meme-feedback]")?.textContent).toBe("已导入 2 张图片。");
    });
    expect(uploadedPayloads.map((p) => p.title)).toEqual(["first", "second", "second"]);
    expect(uploadedPayloads[2].import_id).toBe(secondImportId);
    expect(container.querySelectorAll(".meme-card").length).toBe(2);
  });
});
