import { useEffect, useMemo, useRef } from "react";
import markdownit from "markdown-it";

const TABLE_RULE = /^\s*\|(?:\s*:?[—–－-]+:?\s*\|){2,}\s*$/;

const chatMarkdown = markdownit({ html: false }).disable(["link", "image", "autolink"]);

const artifactMarkdown = markdownit({ html: false }).disable(["link", "image"]);
artifactMarkdown.validateLink = (value: string): boolean => {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password;
  } catch {
    return false;
  }
};
const defaultLinkOpen = artifactMarkdown.renderer.rules.link_open;
artifactMarkdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  if (defaultLinkOpen) {
    return defaultLinkOpen(tokens, index, options, env, renderer);
  }
  return renderer.renderToken(tokens, index, options);
};

export function normalizeTableRules(text: string): string {
  let fence = "";
  return text
    .split("\n")
    .map((line, index, lines) => {
      const marker = /^\s*(`{3,}|~{3,})/.exec(line)?.[1];
      if (marker) {
        if (!fence) fence = marker;
        else if (marker[0] === fence[0] && marker.length >= fence.length) fence = "";
        return line;
      }
      if (
        !fence &&
        line.trimStart().startsWith("|") &&
        lines[index - 1]?.trim() &&
        !lines[index - 1].trimStart().startsWith("|") &&
        TABLE_RULE.test(lines[index + 1] || "")
      ) {
        return "\n" + line;
      }
      if (fence || !TABLE_RULE.test(line)) return line;
      return line.replace(/[—–－-]+/g, "---");
    })
    .join("\n");
}

export function renderMarkdownHtml(text: string, options: { allowLinks?: boolean } = {}): string {
  const engine = options.allowLinks ? artifactMarkdown : chatMarkdown;
  return engine.render(normalizeTableRules(text || ""));
}

export interface MarkdownTextProps {
  text: string;
  allowLinks?: boolean;
  className?: string;
}

export function MarkdownText({ text, allowLinks = false, className }: MarkdownTextProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const html = useMemo(() => renderMarkdownHtml(text, { allowLinks }), [text, allowLinks]);

  useEffect(() => {
    const root = containerRef.current;
    if (!root) return;
    for (const region of root.querySelectorAll("pre, table")) {
      region.setAttribute("tabindex", "0");
      region.setAttribute("aria-label", region.tagName === "PRE" ? "代码" : "表格");
    }
  }, [html]);

  return (
    <div
      ref={containerRef}
      className={className}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
