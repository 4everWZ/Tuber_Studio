import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MemePickerModal } from "./MemePickerModal";

describe("MemePickerModal", () => {
  it("keeps default accept callback referentially stable across re-renders", async () => {
    const loadItems = vi.fn().mockResolvedValue({
      items: [{ asset_id: "m1", title: "摸鱼", enabled: true, missing: false }],
    });
    const onSelect = vi.fn();
    const onClose = vi.fn();

    const { rerender } = render(
      <MemePickerModal open loadItems={loadItems} onSelect={onSelect} onClose={onClose} />
    );

    await screen.findByText("摸鱼");
    expect(loadItems).toHaveBeenCalledTimes(1);

    rerender(
      <MemePickerModal
        open
        selectedAssetId="m1"
        loadItems={loadItems}
        onSelect={onSelect}
        onClose={onClose}
      />
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "选用摸鱼" })).toHaveAttribute("aria-pressed", "true");
    });
    expect(loadItems).toHaveBeenCalledTimes(1);
  });

  it("renders usage_scope badges, new source labels, author_id, and applies accept filter", async () => {
    const loadItems = vi.fn().mockResolvedValue({
      items: [
        {
          asset_id: "m-all",
          title: "全渠道贴图",
          enabled: true,
          missing: false,
          usage_scope: "all_channels",
          source: {
            kind: "remote_feed",
            author: "画师甲",
            author_id: "gh-alpha",
            url: "https://github.com/org/repo",
          },
          license: { name: "CC-BY-4.0" },
        },
        {
          asset_id: "m-int",
          title: "仅互动梗图",
          enabled: true,
          missing: false,
          usage_scope: "interaction_only",
          source: {
            kind: "bilibili_comment",
            author: "路人乙",
            author_id: "uid-42",
          },
        },
      ],
    });

    const { rerender } = render(
      <MemePickerModal
        open
        showSources
        loadItems={loadItems}
        onSelect={vi.fn()}
        onClose={vi.fn()}
      />
    );

    await screen.findByText("全渠道贴图");
    expect(screen.getByText("仅互动梗图")).toBeInTheDocument();
    expect(screen.getByText("全渠道")).toBeInTheDocument();
    expect(screen.getByText("仅互动")).toBeInTheDocument();
    expect(screen.getByText(/画师甲 \(gh-alpha\) · 远程订阅源/)).toBeInTheDocument();
    expect(screen.getByText(/路人乙 \(uid-42\) · B站评论区/)).toBeInTheDocument();

    rerender(
      <MemePickerModal
        open
        showSources
        loadItems={loadItems}
        accept={(a) => (a.usage_scope || "all_channels") === "all_channels"}
        onSelect={vi.fn()}
        onClose={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.queryByText("仅互动梗图")).not.toBeInTheDocument();
    });
    expect(screen.getByText("全渠道贴图")).toBeInTheDocument();
  });
});
