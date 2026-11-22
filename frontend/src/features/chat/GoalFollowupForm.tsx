import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiDelete, apiGet, apiPut } from "@/shared/api/client";
import { errorMessage } from "@/shared/lib/errors";
import type { CreativeGoal, GoalFollowupState } from "./types";

const STATE_CHOICES: ReadonlyArray<[string, string]> = [
  ["live_status", "直播状态"],
  ["game_status", "模拟游戏状态"],
  ["operation_status", "运营状态"],
  ["today_activity", "当天公开运营活动"],
];

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

export interface GoalFollowupFormProps {
  goal: CreativeGoal | null | undefined;
  onEditingChange: (editing: boolean) => void;
}

export function GoalFollowupForm({ goal, onEditingChange }: GoalFollowupFormProps) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState<GoalFollowupState | null>(null);
  const [notice, setNotice] = useState("");
  const [cadence, setCadence] = useState<"daily" | "weekly">("daily");
  const [weekday, setWeekday] = useState(0);
  const [localTime, setLocalTime] = useState("");
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [selectedStates, setSelectedStates] = useState<string[]>([]);
  const [pagesText, setPagesText] = useState("");
  const [busy, setBusy] = useState(false);

  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const cadenceRef = useRef<HTMLSelectElement | null>(null);

  useEffect(() => {
    setOpen(false);
    setCurrent(null);
  }, [goal?.id]);

  if (!goal || goal.closed) {
    return null;
  }

  const goalId = goal.id;

  async function openForm() {
    setOpen(true);
    onEditingChange(true);
    setNotice("正在读取跟进设置…");
    setCurrent(null);
    try {
      const data = await apiGet<"/api/v1/agent/tasks/{identity}/goal/followup", GoalFollowupState>(
        "/api/v1/agent/tasks/{identity}/goal/followup",
        { pathParams: { identity: goalId } }
      );
      setCurrent(data);
      const config = data.schedule || {};
      setCadence(config.cadence === "weekly" ? "weekly" : "daily");
      setWeekday(config.weekday ?? 0);
      setLocalTime(config.local_time || "");
      setTimezone(config.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone);
      setSelectedStates(config.states || []);
      setPagesText((config.pages || []).join("\n"));
      setNotice(
        data.enabled && data.next_due
          ? `下次跟进：${new Date(data.next_due * 1000).toLocaleString()}（设备时间）`
          : "当前未启用。仅重读所选来源，结果留在此目标内。"
      );
      setTimeout(() => cadenceRef.current?.focus(), 0);
    } catch (error) {
      setNotice(errorMessage(error));
    }
  }

  function closeForm() {
    setOpen(false);
    onEditingChange(false);
    setTimeout(() => triggerRef.current?.focus(), 0);
  }

  function toggleState(key: string, checked: boolean) {
    setSelectedStates((prev) =>
      checked ? (prev.includes(key) ? prev : [...prev, key]) : prev.filter((item) => item !== key)
    );
  }

  async function submitUpdate(
    method: "PUT" | "DELETE",
    body?: {
      epoch: number;
      cadence: "daily" | "weekly";
      weekday: number | null;
      local_time: string;
      timezone: string;
      states: string[];
      pages: string[];
    }
  ) {
    setBusy(true);
    try {
      const updated =
        method === "PUT" && body
          ? await apiPut<"/api/v1/agent/tasks/{identity}/goal/followup", GoalFollowupState, typeof body>(
              "/api/v1/agent/tasks/{identity}/goal/followup",
              { pathParams: { identity: goalId }, body }
            )
          : await apiDelete<"/api/v1/agent/tasks/{identity}/goal/followup", GoalFollowupState>(
              "/api/v1/agent/tasks/{identity}/goal/followup",
              { pathParams: { identity: goalId } }
            );
      setCurrent(updated);
      setNotice(
        updated.enabled && updated.next_due
          ? `已启用，下次跟进：${new Date(updated.next_due * 1000).toLocaleString()}（设备时间）`
          : "周期跟进已停用。"
      );
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!current) return;
    const sources = pagesText
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!selectedStates.length && !sources.length) {
      setNotice("请先选择或填写一个只读来源。");
      return;
    }
    void submitUpdate("PUT", {
      epoch: current.epoch || 0,
      cadence,
      weekday: cadence === "weekly" ? weekday : null,
      local_time: localTime,
      timezone: timezone.trim(),
      states: selectedStates,
      pages: sources,
    });
  }

  if (!open) {
    return (
      <button ref={triggerRef} type="button" onClick={() => void openForm()}>
        周期跟进设置
      </button>
    );
  }

  return (
    <form className="chat-task-steer" onSubmit={handleSubmit}>
      <p className="hint" role="status">
        {notice}
      </p>
      {current && (
        <>
          <label>
            频率
            <select
              ref={cadenceRef}
              value={cadence}
              onChange={(event) => setCadence(event.target.value as "daily" | "weekly")}
            >
              <option value="daily">每天</option>
              <option value="weekly">每周</option>
            </select>
          </label>
          <label hidden={cadence !== "weekly"}>
            星期
            <select value={weekday} onChange={(event) => setWeekday(Number(event.target.value))}>
              {WEEKDAYS.map((day, idx) => (
                <option key={day} value={idx}>
                  {day}
                </option>
              ))}
            </select>
          </label>
          <label>
            本地时间
            <input
              type="time"
              required
              value={localTime}
              onChange={(event) => setLocalTime(event.target.value)}
            />
          </label>
          <label>
            时区
            <input
              required
              value={timezone}
              onChange={(event) => setTimezone(event.target.value)}
            />
          </label>
          {STATE_CHOICES.map(([key, title]) => (
            <label key={key} className="check-field">
              <input
                type="checkbox"
                value={key}
                checked={selectedStates.includes(key)}
                onChange={(event) => toggleState(key, event.target.checked)}
              />
              <span>{title}</span>
            </label>
          ))}
          <label>
            公开 HTTPS 来源（每行一个，最多四个）
            <textarea
              value={pagesText}
              onChange={(event) => setPagesText(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            授权并保存
          </button>
          <button
            type="button"
            hidden={!current.enabled}
            disabled={busy}
            onClick={() => void submitUpdate("DELETE")}
          >
            停用跟进
          </button>
        </>
      )}
      <button type="button" onClick={closeForm}>
        返回目标
      </button>
    </form>
  );
}
