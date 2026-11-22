import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const entriesDir = path.resolve(frontendDir, "entries");
const outDir = path.resolve(frontendDir, "../src/tuber_studio/web_dist");

function flattenEntriesAndRelativeStaticBase(): Plugin {
  return {
    name: "tuber-studio-flatten-entries",
    enforce: "post",
    generateBundle(_options, bundle) {
      for (const [key, asset] of Object.entries(bundle)) {
        if (asset.type !== "asset" || !key.endsWith(".html")) continue;
        const baseName = path.posix.basename(key);
        if (typeof asset.source === "string") {
          // Ensure HTML references resolve relative to <base href="{root_path}/">
          // without a leading slash so /studio/ subpaths resolve to /studio/static/bundle/...
          asset.source = asset.source.replace(/(src|href)="\/static\//g, '$1="static/');
        }
        if (key !== baseName) {
          asset.fileName = baseName;
          delete bundle[key];
          bundle[baseName] = asset;
        }
      }
    },
  };
}

export default defineConfig(({ command }) => ({
  root: frontendDir,
  publicDir: path.resolve(frontendDir, "public"),
  base: command === "build" ? "static/" : "/",
  plugins: [react(), ...(command === "build" ? [flattenEntriesAndRelativeStaticBase()] : [])],
  resolve: {
    alias: {
      "@": path.resolve(frontendDir, "src"),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8020",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8020",
        ws: true,
      },
      "/media": {
        target: "http://127.0.0.1:8020",
        changeOrigin: true,
      },
      "/assets": {
        target: "http://127.0.0.1:8020",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir,
    emptyOutDir: true,
    assetsDir: "bundle",
    rollupOptions: {
      input: {
        index: path.resolve(entriesDir, "index.html"),
        live: path.resolve(entriesDir, "live.html"),
        overlay: path.resolve(entriesDir, "overlay.html"),
        "draw-guess-overlay": path.resolve(entriesDir, "draw-guess-overlay.html"),
      },
    },
  },
  experimental: {
    renderBuiltUrl(filename, { hostType }) {
      if (hostType === "html") {
        return `static/${filename}`;
      }
      return { relative: true };
    },
  },
}));
