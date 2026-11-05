"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

const client = import("../src/tuber_studio/web/modules/api.js");

test("shared API sends JSON, multipart forms and WAV files in their original formats", async t => {
  const { api } = await client;
  const sent = [];
  t.mock.method(globalThis, "fetch", async (path, options) => {
    sent.push(new Request(new URL(path, "http://localhost"), options));
    return Response.json({ saved: true });
  });

  const json = { model: "demo" };
  const form = new FormData();
  form.set("title", "new song");
  const wav = new File(["RIFF"], "preview.wav", { type: "audio/wav" });
  await api("/settings", { method: "PUT", body: json });
  await api("/import", { method: "POST", body: form });
  await api("/uploads", { method: "POST", body: wav, headers: { "Content-Type": "audio/wav" } });

  assert.deepEqual(await sent[0].json(), json);
  assert.equal((await sent[1].formData()).get("title"), "new song");
  assert.equal(sent[2].headers.get("content-type"), "audio/wav");
  assert.equal(await sent[2].text(), "RIFF");
  assert.equal(sent[2].credentials, "same-origin");
});

test("shared API reports HTTP detail and calls the page's re-login handler on 401", async t => {
  const { api } = await client;
  t.mock.method(globalThis, "fetch", async () => Response.json({ detail: "Session expired" }, { status: 401 }));
  let loginCount = 0;
  await assert.rejects(api("/status", {}, () => { loginCount += 1; }), {
    status: 401,
    message: "Session expired",
  });
  assert.equal(loginCount, 1);
});
