"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../../..");
test("installed upstream libraries expose one private stdio process without starting servers", {timeout:10000}, async () => {
  const parent = path.join(root, "tmp"); fs.mkdirSync(parent, {recursive:true});
  const temporary = fs.mkdtempSync(path.join(parent, "song-node-test-"));
  const child = spawn(process.execPath, [path.join(__dirname,"../helper.cjs")], {
    env:{...process.env, SELECT_MAX_BR:"false", TUBER_SONG_TEMP:temporary}, windowsHide:true, stdio:["pipe","pipe","pipe"],
  });
  try {
    const line = new Promise((resolve, reject) => {
      let output = "", errors = "";
      child.stderr.on("data", data => errors += data);
      child.stdout.on("data", data => { output += data; if (output.includes("\n")) resolve(JSON.parse(output.split("\n")[0])); });
      child.on("exit", code => reject(new Error(`helper exited ${code}: ${errors}`)));
    });
    child.stdin.write(JSON.stringify({id:1,method:"ping"})+"\n");
    const result = await line;
    assert.equal(result.id,1); assert.equal(result.result.protocol,1);
    assert.deepEqual(result.result.sources,["bodian","migu"]);
    assert.ok(fs.existsSync(path.join(temporary,"anonymous_token")));
  } finally {
    const exited = new Promise(resolve => child.once("exit", resolve)); child.kill(); await exited;
    if (temporary.startsWith(parent + path.sep)) fs.rmSync(temporary, {recursive:true,force:true});
  }
});
