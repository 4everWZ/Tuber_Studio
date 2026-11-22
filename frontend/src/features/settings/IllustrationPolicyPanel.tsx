import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPatch } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import type { IllustrationPolicy } from "./types";

export function IllustrationPolicyPanel() {
  const [saved, setSaved] = useState<IllustrationPolicy | null>(null);
  const [pending, setPending] = useState(false);
  const [resultText, setResultText] = useState("");

  const refresh = useCallback(async () => {
    setPending(true);
    try {
      const value = await apiGet<"/api/v1/persona/illustrations/policy", IllustrationPolicy>(
        "/api/v1/persona/illustrations/policy"
      );
      setSaved(value);
      setResultText("");
    } catch (error) {
      setResultText(errorMessage(error));
    } finally {
      setPending(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleToggle(field: keyof IllustrationPolicy, checked: boolean) {
    if (pending || !saved) return;
    const previous = saved;
    setPending(true);
    setSaved({ ...saved, [field]: checked });
    try {
      const updated = await apiPatch<
        "/api/v1/persona/illustrations/policy",
        IllustrationPolicy,
        Partial<IllustrationPolicy>
      >("/api/v1/persona/illustrations/policy", {
        body: { [field]: checked },
      });
      setSaved(updated);
      setResultText("已保存");
    } catch (error) {
      setSaved(previous);
      setResultText(errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <fieldset className="win-group-box" id="illustration-policy" disabled={pending || !saved}>
      <legend>运营配图</legend>
      <div className="checks">
        <label>
          <input
            type="checkbox"
            name="generate_images"
            checked={Boolean(saved?.generate_images)}
            onChange={(event) => void handleToggle("generate_images", event.target.checked)}
          />
          {" 生图"}
        </label>
        <label>
          <input
            type="checkbox"
            name="use_memes"
            checked={Boolean(saved?.use_memes)}
            onChange={(event) => void handleToggle("use_memes", event.target.checked)}
          />
          {" 图库"}
        </label>
      </div>
      <p className="hint">视频主评、私信和动态共用。都不勾选时关闭自动配图。</p>
      <p className="hint" role="status" id="illustration-policy-result">
        {resultText}
      </p>
    </fieldset>
  );
}
