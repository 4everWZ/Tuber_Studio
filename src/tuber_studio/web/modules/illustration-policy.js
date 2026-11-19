import { api } from "./api.js";

export function initIllustrationPolicy() {
  const form = document.getElementById("illustration-policy");
  const result = document.getElementById("illustration-policy-result");
  const inputs = Array.from(form.querySelectorAll("input"));
  const path = "/api/settings/illustrations";
  let pending = false;
  let saved;

  function render(value) {
    saved = value;
    for (const input of inputs) input.checked = value[input.name];
  }

  async function refresh() {
    if (pending) return;
    pending = true;
    form.disabled = true;
    try {
      render(await api(path));
      result.textContent = "";
    } catch (error) {
      result.textContent = error.message;
    } finally {
      form.disabled = !saved;
      pending = false;
    }
  }

  form.onchange = async ({ target }) => {
    if (pending || !saved || !inputs.includes(target)) return;
    pending = true;
    form.disabled = true;
    try {
      render(await api(path, {method: "PATCH", body: {[target.name]: target.checked}}));
      result.textContent = "已保存";
    } catch (error) {
      render(saved);
      result.textContent = error.message;
    } finally {
      form.disabled = false;
      pending = false;
    }
  };
  return refresh;
}
