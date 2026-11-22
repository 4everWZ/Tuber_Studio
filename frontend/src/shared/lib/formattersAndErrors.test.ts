import { afterEach, describe, expect, it } from "vitest";
import { safeMessage, webCharacterName } from "./errors";
import { humanizeRoundReport, humanizeStatus, humanizeTaskReason } from "./formatters";
import { illustrationStatus, illustrationUrl } from "./operationImage";

describe("shared lib helpers", () => {
  afterEach(() => {
    document.head.innerHTML = "";
  });

  it("masks credentials in safeMessage and formats character names", () => {
    expect(safeMessage("https://x.test?sessdata=secret123&bili_jct=abc&token=xyz&api_key=k1")).toBe(
      "https://x.test?sessdata=[已隐藏]&bili_jct=[已隐藏]&token=[已隐藏]&api_key=[已隐藏]"
    );
    expect(webCharacterName({ id: "whale", display_name: "大肥鱼" })).toBe("大肥鱼");
    expect(webCharacterName({ id: "whale", name: "Whale" })).toBe("Whale");
  });

  it("humanizes statuses, task reasons, and round reports", () => {
    expect(humanizeStatus("publish_uncertain").label).toContain("发布核验中");
    expect(humanizeTaskReason({ status: "invalid" })).toMatch(/未形成有效完整候选/);
    expect(humanizeTaskReason({ status: "awaiting_review" })).toMatch(/等待人工审核/);
    const report = humanizeRoundReport({
      id: "r1",
      status: "completed",
      created_at: Date.now() / 1000,
      budget: { reads: 3, model_calls: 2, input_tokens: 1500, output_tokens: 500 },
      config: { interaction_only: true },
      public_drafts: 1,
    });
    expect(report.interactionOnly).toBe(true);
    expect(report.metrics.tokens).toBe("2.0k");
    expect(report.title).toContain("互动收件箱");
  });

  it("builds /api/v1 illustration URLs honoring <base href>", () => {
    const base = document.createElement("base");
    base.setAttribute("href", "/studio/");
    document.head.appendChild(base);

    expect(illustrationUrl("comments", "d1", { asset_id: "meme_1" })).toBe(
      "/studio/api/v1/persona/memes/meme_1/preview"
    );
    expect(illustrationUrl("dynamics", "d2", { artifact_id: "art_9" })).toBe(
      "/studio/api/v1/operations/illustrations/dynamics/d2/art_9"
    );
    expect(illustrationStatus({ status: "failed", error: "生成超时" })).toBe("生成超时");
  });
});
