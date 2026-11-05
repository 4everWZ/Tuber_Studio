/**
 * API Client for Tuber Studio
 */

export async function api(path, options = {}, onUnauthorized = null) {
  const { body, headers } = options;
  const config = { ...options, headers: new Headers(headers), credentials: "same-origin" };
  if (body !== undefined && typeof body !== "string" && !(body instanceof FormData) && !(body instanceof Blob)) {
    config.headers.set("Content-Type", "application/json");
    config.body = JSON.stringify(body);
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
