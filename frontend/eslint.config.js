import path from "node:path";
import { fileURLToPath } from "node:url";
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const srcRoot = path.resolve(frontendDir, "src");

function resolveSrcRelative(fromFile, specifier) {
  if (typeof specifier !== "string") return null;
  let targetPath = null;
  if (specifier.startsWith("@/")) {
    targetPath = path.resolve(srcRoot, specifier.slice(2));
  } else if (specifier.startsWith(".")) {
    targetPath = path.resolve(path.dirname(fromFile), specifier);
  } else {
    return null;
  }
  const rel = path.relative(srcRoot, targetPath);
  if (rel.startsWith("..") || path.isAbsolute(rel)) return null;
  return rel.split(path.sep).join("/");
}

const architecturalBoundariesPlugin = {
  rules: {
    "layer-boundaries": {
      meta: {
        type: "problem",
        docs: {
          description: "Enforce §7.3 frontend architectural layer and sibling feature boundaries.",
        },
        schema: [],
      },
      create(context) {
        const filename = context.filename;
        if (!filename || filename === "<input>") return {};
        const relFromSrc = path.relative(srcRoot, filename).split(path.sep).join("/");
        if (relFromSrc.startsWith("..") || path.isAbsolute(relFromSrc)) return {};

        const sourceLayer = relFromSrc.split("/")[0];
        const sourceFeature = sourceLayer === "features" ? relFromSrc.split("/")[1] : null;

        function checkImport(node, specifier) {
          const targetRel = resolveSrcRelative(filename, specifier);
          if (!targetRel) return;
          const targetLayer = targetRel.split("/")[0];
          const targetFeature = targetLayer === "features" ? targetRel.split("/")[1] : null;

          if (sourceLayer === "shared" && ["shell", "features", "entries", "app"].includes(targetLayer)) {
            context.report({
              node,
              message: `src/shared/** must never import src/${targetLayer}/** (${specifier}).`,
            });
            return;
          }

          if (sourceLayer === "shell" && ["features", "entries", "app"].includes(targetLayer)) {
            context.report({
              node,
              message: `src/shell/** must never import src/${targetLayer}/** (${specifier}).`,
            });
            return;
          }

          if (sourceLayer === "features") {
            if (targetLayer === "entries" || targetLayer === "app") {
              context.report({
                node,
                message: `src/features/${sourceFeature}/** must not import src/${targetLayer}/** (${specifier}).`,
              });
              return;
            }
            if (targetLayer === "features" && targetFeature && targetFeature !== sourceFeature) {
              context.report({
                node,
                message: `Cross-feature import forbidden: src/features/${sourceFeature}/** cannot import src/features/${targetFeature}/** (${specifier}). Promote shared code to src/shared/.`,
              });
            }
          }
        }

        return {
          ImportDeclaration(node) {
            checkImport(node.source, node.source.value);
          },
          ExportNamedDeclaration(node) {
            if (node.source) checkImport(node.source, node.source.value);
          },
          ExportAllDeclaration(node) {
            if (node.source) checkImport(node.source, node.source.value);
          },
          ImportExpression(node) {
            if (node.source && node.source.type === "Literal") {
              checkImport(node.source, node.source.value);
            }
          },
        };
      },
    },
  },
};

export default tseslint.config(
  {
    ignores: ["dist/**", ".vite/**", "node_modules/**", "../src/tuber_studio/web_dist/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "tuber-arch": architecturalBoundariesPlugin,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "tuber-arch/layer-boundaries": "error",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/shared/api/**"],
    rules: {
      "no-restricted-globals": [
        "error",
        {
          name: "fetch",
          message: "Raw fetch() is forbidden outside src/shared/api/. Use shared/api/client.ts or shared/api/sse.ts.",
        },
      ],
      "no-restricted-properties": [
        "error",
        {
          object: "window",
          property: "fetch",
          message: "Raw window.fetch() is forbidden outside src/shared/api/. Use shared/api/client.ts or shared/api/sse.ts.",
        },
        {
          object: "globalThis",
          property: "fetch",
          message: "Raw globalThis.fetch() is forbidden outside src/shared/api/. Use shared/api/client.ts or shared/api/sse.ts.",
        },
        {
          object: "self",
          property: "fetch",
          message: "Raw self.fetch() is forbidden outside src/shared/api/. Use shared/api/client.ts or shared/api/sse.ts.",
        },
      ],
    },
  },
  {
    files: ["scripts/**/*.mjs", "public/**/*.js", "*.config.{js,ts}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.node,
      },
    },
  },
  {
    files: ["public/studio-path.js"],
    languageOptions: {
      globals: {
        ...globals.browser,
      },
    },
  },
  {
    files: ["public/worklets/**/*.js"],
    languageOptions: {
      globals: {
        AudioWorkletProcessor: "readonly",
        registerProcessor: "readonly",
        sampleRate: "readonly",
        currentFrame: "readonly",
        currentTime: "readonly",
      },
    },
  }
);
