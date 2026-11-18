const prefix = "gateway.input_transcription.";

// Start at a PCM boundary so the selection cannot include speech from before the click.
export function selectUtterance(button, feedback, send, handoff) {
  let state = "idle", id;
  function reset(message = "") {
    state = "idle"; id = null;
    button.textContent = "转认下一句";
    button.disabled = false;
    feedback.textContent = message;
  }
  reset();
  button.hidden = false;
  button.onclick = () => {
    if (state === "idle") {
      state = "arming";
      feedback.textContent = "准备录入，请稍候…";
    } else if (state === "recording") {
      state = "ending";
      feedback.textContent = "正在转认这句录音…";
    }
    button.disabled = true;
  };
  return {
    afterChunk() {
      if (state === "arming") {
        state = "starting";
        send({type: prefix + "start"});
      } else if (state === "ending") {
        state = "waiting";
        send({type: prefix + "finish", capture_id: id});
      }
    },
    event(e) {
      if (e.type === prefix + "started" && state === "starting" && typeof e.capture_id === "string" && e.capture_id) {
        id = e.capture_id; state = "recording";
        feedback.textContent = "请说这句话，说完点结束。";
        button.textContent = "结束并转认这句";
        button.disabled = false;
      } else if (id && id === e.capture_id) {
        if (e.type === prefix + "failed") reset(({capacity: "服务繁忙，请稍后重新录入。", budget: "本次转认超出服务预算。",
          audio_bounds: "这句超过录音范围，请缩短后重录。", empty_audio: "未收到有效录音。"}[e.reason]) || "未取得完整转写，请重新录入。");
        else if (state === "waiting" && e.type === prefix + "completed" && e.source === "explicit_user_audio" &&
            Number.isInteger(e.samples) && e.samples > 0 && e.samples <= 240000 &&
            typeof e.transcript === "string" && e.transcript.trim() && [...e.transcript].length <= 2000 &&
            /^[a-f0-9]{64}$/.test(e.receipt)) {
          reset();
          handoff(e.transcript, {machine_text:e.transcript, capture_id:e.capture_id, samples:e.samples, receipt:e.receipt});
        }
      }
    },
    close() { reset(); button.hidden = true; button.onclick = null; }
  };
}
