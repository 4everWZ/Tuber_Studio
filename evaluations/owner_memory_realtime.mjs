/* Probe the actual Realtime wire with a one-use ticket on stdin. Never log connection URLs or audio. */
import {readFileSync} from "node:fs";
import {setTimeout as sleep} from "node:timers/promises";

const {socket, pcm} = JSON.parse(readFileSync(0, "utf8"));
const audio = Buffer.from(pcm, "base64");
const committed = new Set(), parts = new Set(), rejected = new Set(), transcripts = new Map();
let sent = 0, session = false, native = false, serviceError = false;
const ws = new WebSocket(socket);
const send = event => {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(event));
};
ws.onopen = () => send({type:"session.update", session:{instructions:"请听清号主原话。", modalities:["audio","text"],
  input_audio_format:"pcm16",output_audio_format:"pcm16",playback_commit_policy:"ack_only",extra_body:{auto_response:true}}});
let done;
const terminal = new Promise(resolve => {done = resolve;});
ws.onerror = ws.onclose = done;
ws.onmessage = async ({data}) => {
  const e = JSON.parse(data);
  if (e.type === "error") { serviceError = true; done(); }
  if (e.type === "input_audio_buffer.committed") committed.add(e.item_id);
  if (["conversation.item.added","conversation.item.created","conversation.item.done"].includes(e.type)) {
    const item = e.item;
    if (item?.role !== "user") rejected.add(item?.id);
    else if (item.type === "message") item.content?.forEach((p, i) => {
      if (p.type === "input_audio") parts.add(JSON.stringify([item.id,i]));
    });
  }
  if (e.type === "conversation.item.input_audio_transcription.completed" && e.transcript?.trim() &&
      Number.isInteger(e.content_index)) transcripts.set(JSON.stringify([e.item_id,e.content_index]), e.transcript.trim());
  if (e.type !== "session.created" || session) return;
  session = true;
  native = e.session?.capabilities?.implementation_level === "model_native_duplex";
  if (!native) { done(); return; }
  const chunk = 32000;
  for (let offset = 0; offset < audio.length + 6*chunk && ws.readyState === WebSocket.OPEN; offset += chunk) {
    const block = Buffer.alloc(chunk);
    if (offset < audio.length) audio.copy(block, 0, offset, offset + chunk);
    send({type:"input_audio_buffer.append",format:"pcm16",sample_rate_hz:16000,audio:block.toString("base64")});
    sent++;
    if (offset < audio.length && offset + chunk >= audio.length)
      send({type:"input_audio_buffer.commit",final:true,create_response:false});
    await sleep(1000);
  }
  await sleep(4000); done();
};
const deadline = setTimeout(done, 50000);
await terminal;
clearTimeout(deadline);
send({type:"session.close"});
ws.close();
const bound = [...transcripts].filter(([key]) => {
  const [id] = JSON.parse(key);
  return parts.has(key) && committed.has(id) && !rejected.has(id);
}).map(([,text]) => text);
console.log(JSON.stringify({status:bound.length ? "observed":"unverified",session_created:session,native_duplex:native,
  sent_pcm_seconds:sent,committed_items:committed.size,final_events:transcripts.size,bound_final_user_transcripts:bound,
  service_error:serviceError}));
