import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useUnsavedGuard } from "@/shared";
import { apiGet, apiPut } from "@/shared/api/client";
import { emitNotice, errorMessage } from "@/shared/lib/errors";

export interface OperationRecoveryPolicyFormProps {
  onLoaded?: (maxRecoveries: number) => void;
}

export function OperationRecoveryPolicyForm({ onLoaded }: OperationRecoveryPolicyFormProps) {
  const [value, setValue] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState(false);
  const [stateText, setStateText] = useState("");

  useUnsavedGuard("operation-recovery-policy", dirty);

  const load = useCallback(async () => {
    setPending(true);
    try {
      const result = await apiGet<"/api/v1/operations/recovery-policy", { max_recoveries: number }>(
        "/api/v1/operations/recovery-policy"
      );
      if (
        !Number.isInteger(result.max_recoveries) ||
        result.max_recoveries < 0 ||
        result.max_recoveries > 64
      ) {
        throw new Error("修订次数返回值无效，请重新读取。");
      }
      setLoaded(true);
      if (!dirty) {
        setValue(String(result.max_recoveries));
        setStateText(`已保存：最多修订 ${result.max_recoveries} 次。`);
      } else {
        setStateText("已读取当前设置，保留未保存修改。");
      }
      onLoaded?.(result.max_recoveries);
    } catch (err) {
      setStateText(`读取失败：${errorMessage(err)}`);
    } finally {
      setPending(false);
    }
  }, [dirty, onLoaded]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || !loaded) return;
    const count = Number(value);
    if (value.trim() === "" || !Number.isInteger(count) || count < 0 || count > 64) {
      setStateText("最多修订次数须为 0 到 64 的整数。");
      return;
    }
    setPending(true);
    try {
      const result = await apiPut<
        "/api/v1/operations/recovery-policy",
        { max_recoveries: number },
        { max_recoveries: number }
      >("/api/v1/operations/recovery-policy", { body: { max_recoveries: count } });
      if (result.max_recoveries !== count) {
        throw new Error("保存结果未确认，请重新读取设置。");
      }
      setDirty(false);
      setStateText(
        count ? `已保存：最多修订 ${count} 次。` : "已关闭自动修订，现有处理记录保留。"
      );
    } catch (err) {
      const msg = errorMessage(err);
      emitNotice(msg, true);
      setStateText(msg);
    } finally {
      setPending(false);
    }
  }

  return (
    <div id="operations-recovery-settings">
      <form data-recovery-policy="" className="actions" onSubmit={handleSubmit}>
        <label>
          最多修订次数
          <input
            name="max_recoveries"
            type="number"
            min={0}
            max={64}
            step={1}
            required
            disabled={pending || !loaded}
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
              setDirty(true);
              setStateText("修订次数有未保存修改。");
            }}
          />
        </label>
        <button type="submit" disabled={pending || !loaded}>
          保存修订次数
        </button>
        <button
          type="button"
          data-recovery-refresh=""
          disabled={pending}
          onClick={() => void load()}
        >
          重新读取
        </button>
      </form>
      <p className="hint">仅用于平台明确拒绝后的正文修订；0 表示关闭自动修订，现有处理记录保留。</p>
      <p data-recovery-state="" className="hint" role="status">
        {stateText}
      </p>
    </div>
  );
}
