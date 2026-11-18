const test = require("node:test");
const assert = require("node:assert/strict");

test("handoff requires a committed user audio part and a unique final transcript", async () => {
  const {FinalUtterances} = await import("../src/tuber_studio/web/modules/private-call-handoff.js");
  const utterances = new FinalUtterances();
  const final = {type: "conversation.item.input_audio_transcription.completed", item_id: "voice",
    content_index: 0, transcript: "准备提纲"};
  assert.equal(utterances.accept(final), undefined);
  const item = {type: "message", id: "assistant-voice", role: "assistant", content: [{type: "input_audio"}]};
  utterances.accept({type: "conversation.item.added", item});
  utterances.accept({type: "input_audio_buffer.committed", item_id: "voice"});
  assert.equal(utterances.accept(final), undefined);
  utterances.accept({type: "input_audio_buffer.committed", item_id: item.id});
  assert.equal(utterances.accept({...final, item_id: item.id}), undefined);
  utterances.accept({type: "conversation.item.created", item: {...item, id: "voice", role: "user"}});
  for (const type of ["response.output_audio_transcript.done", "conversation.item.input_audio_transcription.delta",
    "response.output_text.done"]) assert.equal(utterances.accept({...final, type}), undefined);
  assert.equal(utterances.accept({...final, content_index: 1}), undefined);
  assert.equal(utterances.accept(final), "准备提纲");
  assert.equal(utterances.accept(final), undefined);
});
