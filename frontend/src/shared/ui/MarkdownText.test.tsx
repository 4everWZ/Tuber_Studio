import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownText, normalizeTableRules } from "./MarkdownText";

describe("MarkdownText & normalizeTableRules", () => {
  it("normalizes unicode dash table rules and inserts leading newline outside code fences", () => {
    const input = [
      "标题行",
      "| 列1 | 列2 |",
      "| —— | –– |",
      "| a | b |",
      "```md",
      "| x | y |",
      "| —— | —— |",
      "```",
    ].join("\n");

    const normalized = normalizeTableRules(input);
    expect(normalized).toContain("标题行\n\n| 列1 | 列2 |");
    expect(normalized).toContain("| --- | --- |");
    expect(normalized).toContain("```md\n| x | y |\n| —— | —— |\n```");
  });

  it("renders accessible pre/table regions and restricts links according to allowLinks", () => {
    const { container, rerender } = render(
      <MarkdownText
        text={"| A | B |\n| — | — |\n| 1 | 2 |\n\n```js\nconsole.log(1)\n```\n\n[link](https://example.com)"}
      />
    );

    const table = container.querySelector("table");
    const pre = container.querySelector("pre");
    expect(table).toHaveAttribute("tabindex", "0");
    expect(table).toHaveAttribute("aria-label", "表格");
    expect(pre).toHaveAttribute("tabindex", "0");
    expect(pre).toHaveAttribute("aria-label", "代码");
    expect(container.querySelector("a")).toBeNull();

    rerender(
      <MarkdownText
        allowLinks
        text={"<https://example.com> <javascript:alert(1)> <https://u:p@example.com> [md](https://example.com)"}
      />
    );
    const links = container.querySelectorAll("a");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "https://example.com");
    expect(links[0]).toHaveAttribute("target", "_blank");
    expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
  });
});
