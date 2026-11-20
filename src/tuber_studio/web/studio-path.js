globalThis.StudioPath = function studioPath(value) {
  const base = globalThis.document?.querySelector?.("base")?.getAttribute?.("href") || "/";
  const prefix = base.replace(/\/$/, "");
  if (!prefix || typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return value;
  if (value.split(/[?#]/, 1)[0] === prefix || value.startsWith(prefix + "/")) return value;
  return prefix + value;
};
