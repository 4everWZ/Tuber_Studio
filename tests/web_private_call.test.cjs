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

test("microphone slices request only a complete signed transcription", async () => {
  const {callTranscription} = await import("../src/tuber_studio/web/modules/private-call-transcription.js");
  const sent = [], accepted = [], p = "gateway.input_transcription.";
  const pcm = value => new Int16Array(16000).fill(value).buffer;
  const mic = callTranscription(event => sent.push(event), {ticket_hash: "call"}, 2,
    (text, origin) => accepted.push({text, origin}));
  const capture = "mic-1";
  const reply = {type: p + "completed", capture_id: capture, transcript: "整理私有便笺",
    source: "explicit_user_audio", samples: 32000, receipt: "a".repeat(64)};
  mic.pcm(pcm(4));
  assert.deepEqual(sent, []);
  mic.pcm(pcm(3000));
  mic.event({type: p + "started", capture_id: capture});
  mic.pcm(pcm(3000));
  mic.event(reply);
  assert.deepEqual(accepted, []);
  mic.pcm(pcm(0));
  assert.deepEqual(sent, [{type: p + "start"}, {type: p + "finish", capture_id: capture}]);
  mic.event({...reply, type: p + "delta"});
  mic.event({type: "response.output_audio_transcript.done", transcript: "助手的话"});
  mic.event({...reply, capture_id: "other"});
  mic.event(reply);
  mic.event(reply);
  assert.equal(accepted.length, 1);
  assert.equal(accepted[0].origin.capture_id, capture);
  mic.pcm(pcm(3000));
  mic.event({type: p + "started", capture_id: "overlong"});
  mic.pcm(pcm(3000));
  mic.pcm(pcm(3000));
  assert.deepEqual(sent.at(-1), {type: p + "cancel", capture_id: "overlong"});
  assert.equal(mic.pending, false);
});
