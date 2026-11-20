const prefix = "gateway.input_transcription.";

function voiceInBlock(buffer) {
  const samples = new Int16Array(buffer), frame = 320;
  for (let start = 0; start + frame <= samples.length; start += frame) {
    let squares = 0;
    for (let i = start; i < start + frame; i++) squares += samples[i] ** 2;
    if (squares / frame > (32768 * .012) ** 2) return true;
  }
  return false;
}

export function callTranscription(send, context, maximum, accept) {
  let capture = null, insideSpeech = false;
  const closeSlice = () => {
    if (!capture.id) return;
    send({type: prefix + (capture.boundary === "overlong" ? "cancel" : "finish"), capture_id: capture.id});
    if (capture.boundary === "overlong") capture = null;
    else capture.waiting = true;
  };
  return {
    // Called before forwarding the PCM block on the same socket. The gateway
    // collects the bounded slice without taking it out of the duplex stream.
    pcm(buffer) {
      const speaking = voiceInBlock(buffer);
      if (speaking && !insideSpeech && !capture) {
        capture = {frames: 0};
        send({type: prefix + "start"});
      }
      insideSpeech = speaking;
      if (!capture || capture.waiting || capture.boundary) return;
      if (!speaking || capture.frames >= maximum) {
        capture.boundary = speaking ? "overlong" : "quiet";
        closeSlice();
      } else capture.frames++;
    },
    event(event) {
      if (typeof event.type !== "string" || !event.type.startsWith(prefix) || !capture) return;
      if (event.type === prefix + "started" && !capture.id) {
        capture.id = event.capture_id;
        if (capture.boundary) closeSlice();
      } else if (event.capture_id === capture.id && event.type === prefix + "failed") {
        capture = null;
      } else if (event.capture_id === capture.id && capture.waiting && event.type === prefix + "completed") {
        capture = null;
        if (event.source !== "explicit_user_audio" || typeof event.transcript !== "string" ||
            !event.transcript.trim() || !/^[a-f0-9]{64}$/.test(event.receipt ?? "")) return;
        accept(event.transcript, {...context, capture_id: event.capture_id, machine_text: event.transcript,
          receipt: event.receipt, samples: event.samples, transcript_receipt: event.transcript_receipt});
      }
    },
    get pending() { return capture !== null; },
  };
}
