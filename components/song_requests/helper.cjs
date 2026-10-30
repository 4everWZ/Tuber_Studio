"use strict";

// JSON-lines on stdio only: no HTTP server, proxy port, model or shell commands.
const readline = require("node:readline");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const write = process.stdout.write.bind(process.stdout);
if (parseInt(process.versions.node, 10) < 22) {
  write(JSON.stringify({ fatal: "在线点歌组件需要 Node.js 22 或更新版本" }) + "\n");
  process.exit(1);
}
process.on("uncaughtException", error => {
  write(JSON.stringify({ fatal: `组件初始化／执行失败（${error.code || error.name}）` }) + "\n");
  process.exit(1);
});
const temporary = path.resolve(process.env.TUBER_SONG_TEMP || path.join(__dirname, "../../runtime/song_component"));
fs.mkdirSync(temporary, { recursive: true, mode: 0o700 });
if (fs.lstatSync(temporary).isSymbolicLink()) throw new Error("临时目录不能使用链接");
os.tmpdir = () => temporary;
try { fs.writeFileSync(path.join(temporary, "anonymous_token"), "", { flag: "wx", mode: 0o600 }); }
catch (error) { if (error.code !== "EEXIST") throw error; }
if (fs.lstatSync(path.join(temporary, "anonymous_token")).isSymbolicLink()) throw new Error("匿名 token 不能使用链接");
const { select } = require("./matching.cjs");
process.env.ENABLE_FLAC = "true";
process.env.FOLLOW_SOURCE_ORDER = "true";
delete process.env.SELECT_MAX_BR;
process.env.LOG_LEVEL = "silent";
// Upstreams sometimes log to stdout. Reserve it exclusively for the protocol.
console.log = console.info = console.warn = console.error = () => {};
const unmRoot = path.dirname(path.dirname(path.dirname(require.resolve("@unblockneteasemusic/server"))));
const selectorPath = path.join(unmRoot, "src/provider/select.js");
require.cache[selectorPath] = { id: selectorPath, filename: selectorPath, loaded: true, exports: select };
// Disable upstream's remote "insure" resolver: only the two configured providers
// may resolve media. Their failures must never invoke a third-party resolver.
const insurePath = path.join(unmRoot, "src/provider/insure.js");
const unavailable = () => Promise.reject(new Error("音源未返回可用音频"));
require.cache[insurePath] = { id: insurePath, filename: insurePath, loaded: true,
  exports: () => ({ bodian: { track: unavailable }, migu: { track: unavailable } }) };
// Import metadata endpoints individually. Never load song_url_v1 or the fork's
// second resolver, nor start its Express server.
const metadataRoot = path.dirname(require.resolve("netease-metadata/package.json"));
const request = require(path.join(metadataRoot, "util/request.js"));
const metadataRequire = require("node:module").createRequire(path.join(metadataRoot, "package.json"));
const axios = metadataRequire("axios").default;
axios.defaults.timeout = 8000;
axios.defaults.maxContentLength = 1000000;
async function metadata(method, query) {
  const result = await require(path.join(metadataRoot, `module/${method}.js`))({ cookie: {}, ...query }, request);
  if (result.status !== 200 || result.body?.code !== 200) throw new Error(`网易云元数据接口 ${method} 返回 ${result.body?.code || result.status}`);
  return result.body;
}
function song(row) {
  return { id: String(row.id), title: row.name, artists: (row.ar || row.artists || []).map(a => a.name),
    artist: (row.ar || row.artists || []).map(a => a.name).join(" / "),
    album: (row.al || row.album || {}).name || "", cover: (row.al || row.album || {}).picUrl || "",
    duration: (row.dt || row.duration || 0) / 1000, aliases: row.alia || row.alias || [] };
}
async function detail(id) {
  if (!/^\d{1,20}$/.test(String(id))) throw new Error("歌曲 ID 无效");
  const row = (await metadata("song_detail", { ids: String(id) })).songs?.[0];
  if (!row) throw new Error("找不到该歌曲");
  return song(row);
}
async function lyrics(id) {
  const errors = [];
  for (const method of ["lyric", "lyric_new"]) {
    try {
      const body = await metadata(method, { id });
      const text = body.lrc?.lyric;
      if (text && /\[\d+:\d+/.test(text)) return { lrc: text.slice(0, 100000), method };
    } catch (_) { errors.push(method); }
  }
  return { lrc: "", warning: errors.length ? "歌词接口不可用，继续播放" : "暂无逐行歌词" };
}
async function resolve({ id, source, flac }) {
  if (!["bodian", "migu"].includes(source)) throw new Error("不支持的音源");
  if (source === "migu" && !process.env.MIGU_COOKIE) throw new Error("咪咕未配置 MIGU_COOKIE");
  const value = await detail(id);
  select.ENABLE_FLAC = !!flac;
  // Preserve the original title/version: find.js otherwise strips cover labels.
  const info = { id: value.id, name: value.title, duration: value.duration * 1000,
    artists: value.artists.map(name => ({ name })), album: { name: value.album },
    keyword: `${value.title} - ${value.artist}` };
  const provider = require(path.join(unmRoot, `src/provider/${source}.js`));
  const url = await provider.check(info);
  if (typeof url !== "string") throw new Error("音源未返回下载地址");
  return { song: value, source, url };
}
async function dispatch(method, params) {
  switch (method) {
    case "ping": return { protocol: 1, sources: ["bodian", "migu"], migu_configured: !!process.env.MIGU_COOKIE };
    case "search": {
      const query = String(params.query || "").trim();
      if (!query || query.length > 100) throw new Error("请输入100字以内的歌名／歌手");
      const body = await metadata("cloudsearch", { keywords: query, limit: 20, type: 1 });
      return { songs: (body.result?.songs || []).map(song) };
    }
    case "detail": return detail(params.id);
    case "lyrics": return lyrics(params.id);
    case "resolve": return resolve(params);
    default: throw new Error("未知组件指令");
  }
}
// Only resolution is serialized; metadata and ping stay responsive while a
// provider is slow. Sequential resolution isolates upstream global quality.
let work = Promise.resolve();
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", line => {
  if (Buffer.byteLength(line) > 16384) return;
  const execute = async () => {
    let message;
    try {
      message = JSON.parse(line);
      if (!Number.isSafeInteger(message.id)) throw new Error("请求 ID 无效");
      const result = await dispatch(message.method, message.params || {});
      write(JSON.stringify({ id: message.id, result }) + "\n");
    } catch (error) {
      write(JSON.stringify({ id: message?.id, error: error instanceof Error ? error.message : "音源请求失败" }) + "\n");
    }
  };
  let resolving = false;
  try { resolving = JSON.parse(line).method === "resolve"; } catch (_) {}
  if (resolving) work = work.then(execute);
  else execute();
});
input.on("close", () => process.exit(0));
