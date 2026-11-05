/**
 * API Client for Tuber Studio
 */

export async function api(path, options = {}, onUnauthorized = null) {
  const config = { ...options, headers: { ...options.headers }, credentials: "same-origin" };
  if (options.body !== undefined && typeof options.body !== "string" && !(options.body instanceof FormData)) {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, config);
  const text = await response.text();
  let result;
  try {
    result = text ? JSON.parse(text) : {};
  } catch {
    result = { detail: text };
  }
  if (!response.ok) {
    if (response.status === 401 && typeof onUnauthorized === "function") {
      onUnauthorized();
    }
    const errMsg = (result && (result.detail || result.error || result.message)) || `${response.status} ${response.statusText}`;
    const error = new Error(typeof errMsg === "object" ? JSON.stringify(errMsg) : String(errMsg));
    error.status = response.status;
    error.body = result;
    throw error;
  }
  return result;
}
