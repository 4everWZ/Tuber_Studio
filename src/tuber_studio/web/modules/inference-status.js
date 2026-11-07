export function inferenceDisplay(status, settings) {
  const inference = status.inference || {};
  const connected = inference.error ? false : ["active", "idle"].includes(inference.state) ? true : undefined;
  const provider = inference.providers?.[inference.active_provider || inference.active];
  const loaded = connected === true && provider?.running === true && provider?.is_sleeping !== true;
  const source = settings?.inference_broker?.deployment === "remote" ? "Jetson" : "推理节点";
  const connection = connected === true ? "已连接" : connected === false ? "连接异常" : "连接状态待确认";
  const modelState = loaded ? "模型已加载" : connected && (inference.state === "idle" || provider?.running === false) ? "模型未加载" : "模型状态待确认";
  const model = loaded ? (status.persona?.source === "fine_tuned" ? "大肥鱼" : inference.model || "") : "";
  return { connected, loaded, connection, modelState, text: [source + " " + connection, modelState, model].filter(Boolean).join(" · ") };
}
