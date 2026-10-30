"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { select } = require("../matching.cjs");
const song = {name:"测试歌", artists:[{name:"歌手"}], album:{name:"专辑"}, duration:180000};
test("never substitutes the first result for a different recording", () => {
  for (const changes of [{name:"测试歌 (Live)"}, {artists:[{name:"翻唱者"}]}, {duration:200000}, {album:{name:"现场演唱会"}}]) {
    assert.throws(() => select([{...song, ...changes}], song), /没有/);
  }
  assert.equal(select([{...song, duration:100000}, {...song, id:2}], song).id, 2);
});
test("missing duration needs the later stream check; title and all artists stay strict", () => {
  assert.equal(select([{...song, duration:undefined, id:1}], song).id, 1);
  assert.throws(() => select([{...song, artists:[{name:"歌手"},{name:"其他人"}]}], song));
});
