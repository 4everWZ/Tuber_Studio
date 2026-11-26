import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { MONO_FONT_STACK } from "../types";

export type SupportedLanguage =
  | "python"
  | "typescript"
  | "javascript"
  | "bash"
  | "json"
  | "markdown"
  | "text";

type TokenType =
  | "plain"
  | "keyword"
  | "builtin"
  | "function"
  | "string"
  | "number"
  | "comment"
  | "operator"
  | "decorator"
  | "property";

interface Token {
  type: TokenType;
  value: string;
}

const TOKYO_NIGHT: Record<TokenType, string> = {
  plain: "#c0caf5",
  keyword: "#bb9af7",
  builtin: "#2ac3de",
  function: "#7aa2f7",
  string: "#9ece6a",
  number: "#ff9e64",
  comment: "#565f89",
  operator: "#89ddff",
  decorator: "#f7768e",
  property: "#7dcfff",
};

const PY_KEYWORDS = new Set([
  "and", "as", "assert", "async", "await", "break", "class", "continue",
  "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
  "if", "import", "in", "is", "lambda", "match", "case", "nonlocal", "not",
  "or", "pass", "raise", "return", "try", "while", "with", "yield",
]);

const PY_BUILTINS = new Set([
  "True", "False", "None", "self", "cls", "int", "str", "float", "bool",
  "list", "dict", "set", "tuple", "len", "range", "print", "enumerate",
  "zip", "isinstance", "Exception", "ValueError", "TypeError", "Any",
]);

const TS_KEYWORDS = new Set([
  "as", "async", "await", "break", "case", "catch", "class", "const",
  "continue", "debugger", "default", "delete", "do", "else", "enum", "export",
  "extends", "finally", "for", "from", "function", "if", "implements",
  "import", "in", "instanceof", "interface", "let", "new", "of", "package",
  "private", "protected", "public", "readonly", "return", "static", "super",
  "switch", "this", "throw", "try", "type", "typeof", "var", "void", "while",
  "with", "yield", "satisfies", "declare",
]);

const TS_BUILTINS = new Set([
  "true", "false", "null", "undefined", "NaN", "Infinity", "string", "number",
  "boolean", "object", "unknown", "never", "any", "void", "Promise", "Array",
  "Record", "Partial", "React", "console", "Math", "JSON", "Error", "Map", "Set",
]);

const BASH_KEYWORDS = new Set([
  "if", "then", "else", "elif", "fi", "for", "in", "do", "done", "while",
  "until", "case", "esac", "function", "return", "local", "export", "source",
  "echo", "cd", "ls", "rg", "uv", "npm", "npx", "git", "python", "node", "curl",
]);

export function normalizeLanguage(lang?: string): SupportedLanguage {
  const lower = (lang || "text").trim().toLowerCase();
  if (lower === "py" || lower === "python") return "python";
  if (lower === "ts" || lower === "tsx" || lower === "typescript") return "typescript";
  if (lower === "js" || lower === "jsx" || lower === "javascript") return "javascript";
  if (lower === "sh" || lower === "shell" || lower === "zsh" || lower === "bash") return "bash";
  if (lower === "json" || lower === "jsonc") return "json";
  if (lower === "md" || lower === "markdown") return "markdown";
  return "text";
}

function tokenizeMarkdownLine(line: string): Token[] {
  const trimmed = line.trimStart();
  if (trimmed.startsWith("#")) {
    return [{ type: "function", value: line }];
  }
  if (trimmed.startsWith("```") || trimmed.startsWith("---")) {
    return [{ type: "comment", value: line }];
  }
  if (/^[-*+]\s+/.test(trimmed) || /^\d+\.\s+/.test(trimmed)) {
    const idx = line.search(/[-*+\d]/);
    const markerEnd = line.indexOf(" ", idx) + 1;
    return [
      { type: "operator", value: line.slice(0, markerEnd) },
      ...tokenizeMarkdownInline(line.slice(markerEnd)),
    ];
  }
  if (trimmed.startsWith(">")) {
    return [{ type: "string", value: line }];
  }
  return tokenizeMarkdownInline(line);
}

function tokenizeMarkdownInline(text: string): Token[] {
  const tokens: Token[] = [];
  const regex = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIdx) {
      tokens.push({ type: "plain", value: text.slice(lastIdx, match.index) });
    }
    const seg = match[0];
    if (seg.startsWith("`")) {
      tokens.push({ type: "string", value: seg });
    } else if (seg.startsWith("**")) {
      tokens.push({ type: "number", value: seg });
    } else {
      tokens.push({ type: "builtin", value: seg });
    }
    lastIdx = match.index + seg.length;
  }
  if (lastIdx < text.length) {
    tokens.push({ type: "plain", value: text.slice(lastIdx) });
  }
  return tokens;
}

function tokenizeLine(
  line: string,
  lang: SupportedLanguage,
  state: { blockComment: boolean; pyTripleQuote: string | null }
): Token[] {
  if (lang === "text") {
    return [{ type: "plain", value: line }];
  }
  if (lang === "markdown") {
    return tokenizeMarkdownLine(line);
  }

  const tokens: Token[] = [];
  let i = 0;
  const n = line.length;

  const push = (type: TokenType, value: string) => {
    if (!value) return;
    const prev = tokens[tokens.length - 1];
    if (prev && prev.type === type) {
      prev.value += value;
    } else {
      tokens.push({ type, value });
    }
  };

  while (i < n) {
    if (state.blockComment) {
      const endIdx = line.indexOf("*/", i);
      if (endIdx === -1) {
        push("comment", line.slice(i));
        break;
      }
      push("comment", line.slice(i, endIdx + 2));
      i = endIdx + 2;
      state.blockComment = false;
      continue;
    }

    if (state.pyTripleQuote) {
      const endIdx = line.indexOf(state.pyTripleQuote, i);
      if (endIdx === -1) {
        push("string", line.slice(i));
        break;
      }
      push("string", line.slice(i, endIdx + 3));
      i = endIdx + 3;
      state.pyTripleQuote = null;
      continue;
    }

    const ch = line[i];
    const two = line.slice(i, i + 2);
    const three = line.slice(i, i + 3);

    if (/\s/.test(ch)) {
      let j = i + 1;
      while (j < n && /\s/.test(line[j])) j++;
      push("plain", line.slice(i, j));
      i = j;
      continue;
    }

    if (lang === "python" || lang === "bash") {
      if (ch === "#") {
        push("comment", line.slice(i));
        break;
      }
    }

    if (lang === "typescript" || lang === "javascript") {
      if (two === "//") {
        push("comment", line.slice(i));
        break;
      }
      if (two === "/*") {
        const endIdx = line.indexOf("*/", i + 2);
        if (endIdx === -1) {
          push("comment", line.slice(i));
          state.blockComment = true;
          break;
        }
        push("comment", line.slice(i, endIdx + 2));
        i = endIdx + 2;
        continue;
      }
    }

    if (lang === "python" && (three === '"""' || three === "'''")) {
      const endIdx = line.indexOf(three, i + 3);
      if (endIdx === -1) {
        push("string", line.slice(i));
        state.pyTripleQuote = three;
        break;
      }
      push("string", line.slice(i, endIdx + 3));
      i = endIdx + 3;
      continue;
    }

    if (ch === '"' || ch === "'" || (ch === "`" && (lang === "typescript" || lang === "javascript"))) {
      let j = i + 1;
      while (j < n) {
        if (line[j] === "\\") {
          j += 2;
          continue;
        }
        if (line[j] === ch) {
          j += 1;
          break;
        }
        j += 1;
      }
      const strVal = line.slice(i, j);
      if (lang === "json") {
        const rest = line.slice(j).trimStart();
        push(rest.startsWith(":") ? "property" : "string", strVal);
      } else {
        push("string", strVal);
      }
      i = j;
      continue;
    }

    if (lang === "python" && ch === "@") {
      let j = i + 1;
      while (j < n && /[A-Za-z0-9_.]/.test(line[j])) j++;
      push("decorator", line.slice(i, j));
      i = j;
      continue;
    }

    if (lang === "bash" && ch === "$") {
      let j = i + 1;
      if (line[j] === "{") {
        const close = line.indexOf("}", j + 1);
        j = close === -1 ? n : close + 1;
      } else {
        while (j < n && /[A-Za-z0-9_?@*#]/.test(line[j])) j++;
      }
      push("builtin", line.slice(i, j));
      i = j;
      continue;
    }

    if (lang === "bash" && ch === "-" && /[A-Za-z0-9-]/.test(line[i + 1] || "")) {
      let j = i + 1;
      while (j < n && /[A-Za-z0-9_-]/.test(line[j])) j++;
      push("property", line.slice(i, j));
      i = j;
      continue;
    }

    if (/[0-9]/.test(ch)) {
      let j = i + 1;
      while (j < n && /[0-9a-fA-FxXbBoO_.]/.test(line[j])) j++;
      push("number", line.slice(i, j));
      i = j;
      continue;
    }

    if (/[A-Za-z_]/.test(ch)) {
      let j = i + 1;
      while (j < n && /[A-Za-z0-9_-]/.test(line[j])) {
        if (line[j] === "-" && lang !== "bash") break;
        j++;
      }
      const word = line.slice(i, j);
      const nextNonSpace = line.slice(j).trimStart()[0];

      if (lang === "json") {
        if (word === "true" || word === "false" || word === "null") {
          push("number", word);
        } else {
          push("plain", word);
        }
      } else if (lang === "python") {
        if (PY_KEYWORDS.has(word)) push("keyword", word);
        else if (PY_BUILTINS.has(word)) push("builtin", word);
        else if (nextNonSpace === "(") push("function", word);
        else if (/^[A-Z][A-Za-z0-9_]*$/.test(word)) push("builtin", word);
        else push("plain", word);
      } else if (lang === "typescript" || lang === "javascript") {
        if (TS_KEYWORDS.has(word)) push("keyword", word);
        else if (TS_BUILTINS.has(word)) push("builtin", word);
        else if (nextNonSpace === "(") push("function", word);
        else if (/^[A-Z][A-Za-z0-9_]*$/.test(word)) push("builtin", word);
        else push("plain", word);
      } else if (lang === "bash") {
        if (BASH_KEYWORDS.has(word)) push("keyword", word);
        else push("plain", word);
      } else {
        push("plain", word);
      }
      i = j;
      continue;
    }

    if ("=+-*/%<>!&|^~?:.,;()[]{}".includes(ch)) {
      push("operator", ch);
      i += 1;
      continue;
    }

    push("plain", ch);
    i += 1;
  }

  return tokens;
}

export function tokenizeCode(code: string, language?: string): Token[][] {
  const lang = normalizeLanguage(language);
  const lines = code.split("\n");
  const state = { blockComment: false, pyTripleQuote: null as string | null };
  return lines.map((line) => tokenizeLine(line, lang, state));
}

export interface CodeHighlighterProps {
  code: string;
  language?: string;
  highlightLines?: number[];
  highlightTarget?: string;
  autoFocusLine?: number;
  dimInactive?: boolean;
  fontSize?: number;
  maxLines?: number;
  animateEntrance?: boolean;
}

export const CodeHighlighter: React.FC<CodeHighlighterProps> = ({
  code,
  language = "python",
  highlightLines,
  highlightTarget,
  autoFocusLine,
  dimInactive = true,
  fontSize = 21,
  maxLines = 18,
  animateEntrance = true,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const rawLines = code.split("\n").slice(0, maxLines);
  const tokenized = tokenizeCode(rawLines.join("\n"), language);
  const explicitSet = new Set((highlightLines || []).map((n) => Math.floor(n)));
  const cleanTarget = (highlightTarget || "").trim().toLowerCase();

  const hasAnySpotlight =
    explicitSet.size > 0 ||
    cleanTarget.length > 0 ||
    typeof autoFocusLine === "number";

  return (
    <div
      style={{
        padding: "14px 0",
        fontFamily: MONO_FONT_STACK,
        fontSize,
        lineHeight: 1.54,
        backgroundColor: "#1a1b26",
        color: TOKYO_NIGHT.plain,
        flex: 1,
        overflow: "hidden",
      }}
    >
      {tokenized.map((tokens, idx) => {
        const lineNum = idx + 1;
        const rawLine = rawLines[idx] ?? "";
        const isHighlighted =
          explicitSet.has(lineNum) ||
          (cleanTarget.length > 0 && rawLine.toLowerCase().includes(cleanTarget)) ||
          (explicitSet.size === 0 && cleanTarget.length === 0 && idx === autoFocusLine);

        const enter = animateEntrance
          ? spring({
              frame: Math.max(0, frame - idx * 1.5),
              fps,
              config: { damping: 20, stiffness: 170 },
            })
          : 1;

        const targetOpacity =
          hasAnySpotlight && dimInactive && !isHighlighted ? 0.45 : 1;
        const lineOpacity = interpolate(enter, [0, 1], [0, targetOpacity]);

        return (
          <div
            key={`${lineNum}-${rawLine.slice(0, 14)}`}
            style={{
              display: "flex",
              alignItems: "baseline",
              padding: "3px 20px",
              backgroundColor: isHighlighted
                ? "rgba(122, 162, 247, 0.16)"
                : "transparent",
              borderLeft: isHighlighted
                ? "4px solid #7aa2f7"
                : "4px solid transparent",
              opacity: lineOpacity,
              transform: animateEntrance
                ? `translateX(${interpolate(enter, [0, 1], [8, 0]).toFixed(1)}px)`
                : undefined,
            }}
          >
            <span
              style={{
                width: 40,
                flexShrink: 0,
                color: isHighlighted ? "#7dcfff" : "#414868",
                userSelect: "none",
                fontSize: Math.max(14, fontSize - 4),
                fontWeight: 700,
              }}
            >
              {lineNum}
            </span>
            <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", flex: 1 }}>
              {tokens.length > 0 ? (
                tokens.map((tok, tIdx) => (
                  <span
                    key={tIdx}
                    style={{
                      color: TOKYO_NIGHT[tok.type],
                      fontStyle: tok.type === "comment" ? "italic" : "normal",
                      fontWeight:
                        tok.type === "keyword" || tok.type === "decorator"
                          ? 600
                          : 400,
                    }}
                  >
                    {tok.value}
                  </span>
                ))
              ) : (
                " "
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
};
