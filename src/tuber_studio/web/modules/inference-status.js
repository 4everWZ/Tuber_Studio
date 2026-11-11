export function inferenceDisplay(status, settings) {
  if (status.providers?.llm === "demo") {
    return { connected: undefined, loaded: false, connection: "离线演示", modelState: "", text: "离线演示" };
  }
  if (status.providers?.llm_route === "direct") {
    const context = status.context || {};
    const connected = context.profile_error || status.error ? false : context.verified === true ? true : undefined;
    const loaded = connected === true && context.is_sleeping !== true;
    const connection = connected === true ? "已连接" : connected === false ? "连接异常" : "连接待确认";
    const modelState = loaded ? "模型已加载" : connected ? "模型未加载" : "";
    const model = loaded ? status.persona?.configured_model || settings?.llm?.model : "";
    return { connected, loaded, connection, modelState, text: [`独立推理 ${connection}`, modelState, model].filter(Boolean).join(" · ") };
  }
  const inference = status.inference || {};
  const connected = inference.error ? false : ["active", "idle", "cold"].includes(inference.state) ? true : undefined;
  const provider = inference.providers?.[inference.active_provider || inference.active || inference.selected_provider];
  const loaded = connected === true && provider?.running === true && provider?.is_sleeping !== true;
  const source = settings?.inference_broker?.deployment === "remote" ? "Jetson" : "推理节点";
  const connection = connected === true ? "已连接" : connected === false ? "连接异常" : "连接状态待确认";
  const modelState = loaded ? "模型已加载" : connected && (["idle", "cold"].includes(inference.state) || provider?.running === false || provider?.is_sleeping === true) ? "模型未加载" : "模型状态待确认";
  const model = loaded ? (status.persona?.source === "fine_tuned" ? "大肥鱼" : inference.model || "") : "";
  return { connected, loaded, connection, modelState, text: [source + " " + connection, modelState, model].filter(Boolean).join(" · ") };
}
