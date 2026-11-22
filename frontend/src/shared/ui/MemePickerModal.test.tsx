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
});
