import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { operationsStore } from "@/shared/operations/operationsStore";
import type { OperationScheduleCloseoutRun, OperationScheduleItem } from "@/shared/operations/types";
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

const savedSchedule: OperationScheduleItem = {
  id: "schedule-a", kind: "daily_window", label: "晚间排班", status: "scheduled", enabled: true,
  scope_discovery: true, scope_inbox: true, start_time: "17:00", end_time: "19:00",
  weekdays: [], weekday_labels: [], timezone: "Asia/Taipei", created_at: 110, updated_at: 110,
};

const manualRun: OperationScheduleCloseoutRun = {
  id: "closeout-1", period_start_at: 1795762800, period_end_at: 1795770000,
  video_start_at: 1795773600, video_end_at: 1795777200, dynamic_status: "review", dynamic_id: 21, video_status: "manual",
};

function actions() {
  return {
    onCreateSchedule: vi.fn().mockResolvedValue(savedSchedule),
    onUpdateSchedule: vi.fn().mockResolvedValue(savedSchedule),
    onDeleteSchedule: vi.fn().mockResolvedValue(null),
    onPrepareVideo: vi.fn().mockResolvedValue(manualRun),
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
        onPrepareVideo={vi.fn()}
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
    expect(onCreateSchedule.mock.calls[0][0]).not.toHaveProperty("closeout");

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

  it("requires the independent video times and sends selected closeout modes for a window", async () => {
    const user = userEvent.setup();
    const handlers = actions();
    render(<OperationSchedulePanel status={makeStatus()} {...handlers} />);

    await user.click(screen.getByText("收尾设置（默认关闭）"));
    await user.selectOptions(screen.getByLabelText("收尾动态"), "review");
    await user.selectOptions(screen.getByLabelText("收尾视频"), "automatic_review");
    expect(screen.getByRole("button", { name: "添加时间段" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "刷 1 小时" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("制片开始"), { target: { value: "23:40" } });
    fireEvent.change(screen.getByLabelText("制片结束"), { target: { value: "23:40" } });
    expect(screen.getByText(/制片开始和结束时间必须不同/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("制片结束"), { target: { value: "00:20" } });
    await user.click(screen.getByRole("button", { name: "添加时间段" }));

    expect(handlers.onCreateSchedule).toHaveBeenCalledWith(expect.objectContaining({
      kind: "once_window",
      closeout: {
        dynamic_mode: "review", video_mode: "automatic_review",
        video_start: "23:40", video_end: "00:20", timezone: "Asia/Taipei",
      },
    }));
  });

  it("edits only the selected schedule and keeps manual follow-up on completed runs", async () => {
    const user = userEvent.setup();
    const handlers = actions();
    const endOfDay = {
      ...savedSchedule, id: "schedule-b", status: "completed",
      closeout: { dynamic_mode: "review", video_mode: "manual", video_start: "20:00", video_end: "21:00" } as const,
      closeout_runs: [manualRun],
    };
    const { rerender } = render(<OperationSchedulePanel status={makeStatus({ schedules: [savedSchedule, endOfDay] })} {...handlers} />);

    await user.click(screen.getByRole("button", { name: "编辑收尾" }));
    const savedRow = within(document.querySelector('[data-schedule-id="schedule-a"]') as HTMLElement);
    await user.selectOptions(savedRow.getByLabelText("收尾动态"), "automatic");
    await user.click(savedRow.getByRole("button", { name: "保存收尾设置" }));
    expect(handlers.onUpdateSchedule).toHaveBeenCalledWith("schedule-a", {
      closeout: { dynamic_mode: "automatic", video_mode: "off", video_start: null, video_end: null, timezone: "Asia/Taipei" },
    });
    expect(screen.getByRole("link", { name: "查看动态 #21" })).toHaveAttribute("href", "#dynamics");

    handlers.onPrepareVideo.mockRejectedValueOnce(new Error("当前制片时段尚未开始"));
    await user.click(screen.getByRole("button", { name: "准备视频" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("当前制片时段尚未开始");
    rerender(<OperationSchedulePanel status={makeStatus({ schedules: [savedSchedule, {
      ...endOfDay, closeout_runs: [{ ...manualRun, video_status: "render_failed", last_error: "渲染中断" }],
    }] })} {...handlers} />);
    expect(screen.getByText("渲染中断")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试制片" }));
    expect(handlers.onPrepareVideo).toHaveBeenLastCalledWith("schedule-b", "closeout-1");
    rerender(<OperationSchedulePanel status={makeStatus({ schedules: [savedSchedule, {
      ...endOfDay, closeout_runs: [{ ...manualRun, video_status: "ready", video_job_id: "video-42" }],
    }] })} {...handlers} />);
    expect(screen.getByRole("link", { name: "打开视频任务" })).toHaveAttribute("href", "#videos/video-42");
    expect(screen.queryByRole("button", { name: "准备视频" })).not.toBeInTheDocument();
  });

  it("posts to the bound run and updates the matching schedule from the returned run", async () => {
    const returnedRun = { ...manualRun, video_status: "ready", video_job_id: "video-42" };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ run: returnedRun }), {
      headers: { "Content-Type": "application/json" },
    }));
    operationsStore.applyAutomationStatus(makeStatus({ schedules: [{ ...savedSchedule, closeout_runs: [manualRun] }] }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(operationsStore.prepareOperationScheduleVideo("schedule-a", "closeout-1")).resolves.toEqual(returnedRun);
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/operations/automation/schedules/schedule-a/closeouts/closeout-1/prepare-video",
        expect.objectContaining({ method: "POST" }),
      );
      expect(operationsStore.getSnapshot().status?.schedules?.[0].closeout_runs).toEqual([returnedRun]);
    } finally {
      vi.unstubAllGlobals();
      operationsStore.reset();
    }
  });

  it("keeps an expired automatic window actionable only when no video job exists", async () => {
    const user = userEvent.setup();
    const handlers = actions();
    const missed = { ...manualRun, dynamic_id: null, dynamic_status: "off", video_status: "window_missed" };
    const endOfDay = {
      ...savedSchedule, id: "schedule-expired", status: "completed",
      closeout: { dynamic_mode: "off", video_mode: "automatic_publish", video_start: "20:00", video_end: "21:00" } as const,
      closeout_runs: [missed, { ...missed, id: "already-produced", video_job_id: "video-42" }],
    };
    render(<OperationSchedulePanel status={makeStatus({ schedules: [endOfDay] })} {...handlers} />);

    const recover = screen.getByRole("button", { name: "手动准备视频" });
    expect(recover).toBeVisible();
    expect(screen.getByRole("link", { name: "打开视频任务" })).toHaveAttribute("href", "#videos/video-42");
    await user.click(recover);
    expect(handlers.onPrepareVideo).toHaveBeenCalledExactlyOnceWith("schedule-expired", "closeout-1");
  });
});
