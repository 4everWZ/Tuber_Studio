import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CapabilityStrip, operationCapabilities } from "./CapabilityStrip";

describe("CapabilityStrip & operationCapabilities", () => {
  it("separates saved permissions from actual runtime and private polling", () => {
    const permissions = {
      video_enabled: true,
      top_level: "auto",
      replies: "review",
      private_poll: false,
      private_messages: "auto",
      dynamics: "review",
      video_like: "off",
      video_fav: "off",
      comment_like: "review",
      dynamic_like: "auto",
    };

    const off = operationCapabilities({ policy: { enabled: false }, state: "off", permissions });
    expect(off).toHaveLength(10);
    expect(off.every((item) => item.color === "gray")).toBe(true);
    expect(off.find((item) => item.id === "videos")?.state).toBe("待启动");
    expect(off.find((item) => item.id === "message_poll")?.state).toBe("关闭");

    const active = Object.fromEntries(
      operationCapabilities({ policy: { enabled: true }, state: "running", permissions }).map((item) => [
        item.id,
        item,
      ])
    );
    for (const key of ["videos", "top_level", "message_reply", "dynamic-like"]) {
      expect(active[key].color).toBe("green");
    }
    for (const key of ["comments", "dynamics", "comment-like"]) {
      expect(active[key].color).toBe("yellow");
    }
    for (const key of ["message_poll", "like", "favorite"]) {
      expect(active[key].color).toBe("gray");
    }
  });

  it("uses reported parent blockers without turning historical errors into channel failures", () => {
    const status = {
      policy: { enabled: true },
      state: "blocked",
      permissions: { video_enabled: true, private_poll: false, private_messages: "review" },
      blockers: ["账号身份核验失败：本地测试原因"],
    };

    const rows = operationCapabilities(status);
    expect(rows.find((item) => item.id === "videos")?.color).toBe("red");
    expect(rows.find((item) => item.id === "message_poll")?.color).toBe("gray");
    expect(rows.find((item) => item.id === "message_reply")?.detail).toBe(status.blockers[0]);

    render(<CapabilityStrip status={status} showLegend showBlocker />);
    const list = screen.getByRole("list", { name: "各功能运行状态" });
    const items = screen.getAllByRole("listitem");
    expect(list).toBeInTheDocument();
    expect(items).toHaveLength(10);
    expect(items[0]).toHaveAttribute("aria-label", expect.stringMatching(/视频，受阻/));
    expect(document.getElementById("op-capability-blocker")?.textContent).toMatch(/账号身份核验失败/);

    const recovered = operationCapabilities({
      ...status,
      state: "running",
      blockers: [],
      last_error: "上轮旧错误",
    });
    expect(recovered.every((item) => item.color !== "red")).toBe(true);
  });

  it("shows stopping state consistently in capability lights", () => {
    render(
      <CapabilityStrip
        status={{ policy: { enabled: true }, state: "stopping", permissions: { video_enabled: true } }}
      />
    );
    expect(screen.getAllByRole("listitem")[0]).toHaveTextContent(/停止中/);
  });
});
