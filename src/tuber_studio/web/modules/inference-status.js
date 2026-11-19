export function inferenceDisplay(status, settings) {
  const context = status.context || {};
  const configured = context.capacity_source === "model_card" || Boolean(settings?.omni);
  const connected = !configured ? undefined : context.profile_error ? false : context.verified === true ? true : undefined;
  const connection = connected === true ? "已连接" : connected === false ? "连接异常" : "待确认";
  return { connected, connection, text: "Omni API · " + connection };
}
