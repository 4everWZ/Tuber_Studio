import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { OperationSchedulePanel } from "./OperationSchedulePanel";
import type { AutomationStatus } from "./types";

function makeStatus(overrides: Partial<AutomationStatus> = {}): AutomationStatus {
  return {
    state: "off",
    policy: { enabled: false, emergency: false },
    settings: { business_timezone: "Asia/Taipei" },
    active_schedule: null,
    schedules: [],
    ...overrides,
  };
}

describe("OperationSchedulePanel", () => {
  it("triggers 1-hour timed duration run and supports window creation and cancellation", async () => {
    const user = userEvent.setup();
    const onCreateSchedule = vi.fn().mockResolvedValue(null);
    const onUpdateSchedule = vi.fn().mockResolvedValue(null);
    const onDeleteSchedule = vi.fn().mockResolvedValue(null);

    render(
      <OperationSchedulePanel
        status={makeStatus({
          state: "running",
          policy: { enabled: true, emergency: false },
          active_schedule: {
            id: "sch-active1",
            kind: "duration",
            label: "Aqua 1 小时刷视频",
            status: "active",
            enabled: true,
            scope_discovery: true,
            scope_inbox: true,
            end_local_display: "2026-11-27 03:30",
            remaining_seconds: 3540,
            weekdays: [],
            weekday_labels: [],
            timezone: "Asia/Taipei",
            created_at: 100,
            updated_at: 100,
          },
          schedules: [
            {
              id: "sch-active1",
              kind: "duration",
              label: "Aqua 1 小时刷视频",
              status: "active",
              enabled: true,
              scope_discovery: true,
              scope_inbox: true,
              start_local_display: "2026-11-27 02:30",
              end_local_display: "2026-11-27 03:30",
              remaining_seconds: 3540,
              weekdays: [],
              weekday_labels: [],
              timezone: "Asia/Taipei",
              created_at: 100,
              updated_at: 100,
            },
            {
              id: "sch-daily1",
              kind: "daily_window",
              label: "晚间定时",
              status: "scheduled",
              enabled: true,
              scope_discovery: true,
              scope_inbox: false,
              start_time: "20:00",
              end_time: "22:00",
              next_start_local_display: "2026-11-27 20:00",
              weekdays: [0, 2, 4],
              weekday_labels: ["周一", "周三", "周五"],
              timezone: "Asia/Taipei",
              created_at: 90,
              updated_at: 90,
            },
          ],
        })}
        onCreateSchedule={onCreateSchedule}
        onUpdateSchedule={onUpdateSchedule}
        onDeleteSchedule={onDeleteSchedule}
      />
    );

    expect(screen.getByText(/预计 2026-11-27 03:30 自动停止/)).toBeInTheDocument();

    await user.click(document.getElementById("schedule-quick-1h") as HTMLElement);
    expect(onCreateSchedule).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "duration",
        duration_minutes: 60,
        scope_discovery: true,
        scope_inbox: true,
      })
    );

    await user.click(document.getElementById("cancel-active-schedule") as HTMLElement);
    expect(onUpdateSchedule).toHaveBeenCalledWith("sch-active1", { action: "cancel" });

    // Switch to daily_window creator and submit
    await user.click(screen.getByText("预约时间段／每日定时排班"));
    await user.click(screen.getByRole("tab", { name: "每日定时" }));
    await user.click(document.getElementById("schedule-window-submit") as HTMLElement);
    expect(onCreateSchedule).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "daily_window",
        start_time: "20:00",
        end_time: "22:00",
      })
    );
  });
});
