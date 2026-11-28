import { Fragment, createElement } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";
import { SubNav } from "./SubNav";
import { TopStatusBar } from "./TopStatusBar";
import {
  SUB_NAV_TABS,
  TOP_NAV_ITEMS,
  parseHashRoute,
  workspaceSection,
} from "./navigation";

describe("shell workspace navigation", () => {
  it("offers chat and four parent workspaces with refined Aqua labels, reachable local tabs, and issues link", () => {
    expect(TOP_NAV_ITEMS).toHaveLength(5);
    expect(TOP_NAV_ITEMS.map((item) => item.label)).toEqual([
      "对话与脉冲",
      "渠道活动",
      "直播台",
      "角色与声音",
      "设置",
    ]);
    expect(TOP_NAV_ITEMS[0].href).toBe("#chat");
    expect(TOP_NAV_ITEMS.map((item) => item.parentPage).filter(Boolean)).toEqual([
      "operations",
      "live",
      "characters",
      "settings",
    ]);

    const allSubTabs = Object.values(SUB_NAV_TABS).flat();
    for (const expectedRoute of [
      "live",
      "live-interaction",
      "live-scene",
      "live-music",
      "live-settings",
      "characters",
      "voices",
      "assets",
      "settings",
      "personalization",
      "accounts",
      "overview",
      "issues",
    ]) {
      expect(allSubTabs.some((t) => t.page === expectedRoute)).toBe(true);
    }

    expect(SUB_NAV_TABS.live.map((tab) => tab.label)).toEqual([
      "导播台",
      "排班与规则",
      "场景编排",
      "曲库与点歌",
      "直播设置",
    ]);
    expect(SUB_NAV_TABS.live.map((tab) => tab.page)).toEqual([
      "live",
      "live-interaction",
      "live-scene",
      "live-music",
      "live-settings",
    ]);

    const onToggleCollapse = vi.fn();
    render(
      createElement(
        Fragment,
        null,
        createElement(Sidebar, {
          currentPage: "settings",
          currentSection: "settings",
          localAccess: false,
          menuOpen: false,
          sidebarCollapsed: false,
          onToggleCollapse,
          onCloseMenu: vi.fn(),
          onLogout: vi.fn(),
        }),
        createElement(TopStatusBar, {
          menuOpen: false,
          onToggleMenu: vi.fn(),
          status: null,
          settings: null,
          automation: null,
          account: null,
          unreadIssuesCount: 0,
        })
      )
    );

    const sidebarEl = document.getElementById("workspace-sidebar");
    expect(sidebarEl).not.toBeNull();
    expect(sidebarEl?.className).not.toContain("win-explorer-tree");
    expect(screen.getByText("Aqua Studio")).toBeInTheDocument();
    expect(screen.getByText("Aqua · Personal Agent")).toBeInTheDocument();

    const collapseBtn = document.getElementById("collapse-workspace-sidebar");
    expect(collapseBtn).not.toBeNull();
    expect(collapseBtn?.getAttribute("aria-expanded")).toBe("true");

    const issuesLink = document.getElementById("workspace-issues");
    expect(issuesLink).not.toBeNull();
    expect(issuesLink?.getAttribute("href")).toBe("#issues");
  });

  it("parses deep links and legacy anchors to their parent workspace and local tabs", () => {
    const cases: Array<[string, string, string | undefined, Partial<ReturnType<typeof parseHashRoute>>]> = [
      ["", "chat", undefined, {}],
      [
        "#comments/review/comments/draft",
        "comments",
        "operations",
        { routeKind: "review", routeId: "comments", taskId: "draft" },
      ],
      [
        "#automation/round/round/task",
        "automation-records",
        "operations",
        { routeKind: "round", routeId: "round", taskId: "task" },
      ],
      ["#live", "live", "live", {}],
      ["#live-interaction", "live-interaction", "live", {}],
      ["#live-game-panel", "live-interaction", "live", { anchor: "live-game-panel" }],
      ["#live-scene", "live-scene", "live", {}],
      ["#live-music", "live-music", "live", {}],
      ["#live-settings", "live-settings", "live", {}],
      ["#voices", "voices", "characters", {}],
      ["#accounts", "accounts", "settings", {}],
      ["#issues", "issues", "settings", {}],
      ["#providers", "settings", "settings", { anchor: "providers-panel" }],
      ["#storage", "overview", "settings", { anchor: "cache-panel" }],
      ["#memory", "personalization", "settings", { anchor: "daily-memory-card" }],
      ["#unknown-route-xyz", "automation", "operations", {}],
    ];

    for (const [hash, expectedPage, expectedSection, expectedFields] of cases) {
      const parsed = parseHashRoute(hash);
      expect(parsed.page).toBe(expectedPage);
      expect(parsed.section).toBe(expectedSection);
      expect(workspaceSection(parsed.page)).toBe(expectedSection);
      for (const [key, val] of Object.entries(expectedFields)) {
        expect(parsed[key as keyof typeof parsed]).toBe(val);
      }
    }
  });

  it("highlights only the active parent workspace and sub-tab, and toggles aria-expanded on menu button", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    const onClose = vi.fn();

    const renderShell = (menuOpen: boolean) =>
      createElement(
        Fragment,
        null,
        createElement(Sidebar, {
          currentPage: "comments",
          currentSection: "operations",
          localAccess: false,
          menuOpen,
          onCloseMenu: onClose,
          onLogout: vi.fn(),
        }),
        createElement(TopStatusBar, {
          menuOpen,
          onToggleMenu: onToggle,
          status: null,
          settings: null,
          automation: { state: "running", policy: { enabled: true } },
          account: null,
          unreadIssuesCount: 2,
        }),
        createElement(SubNav, { currentPage: "comments", section: "operations" })
      );

    const { rerender } = render(renderShell(false));

    const activeParentLinks = document.querySelectorAll('[data-parent-page][aria-current="page"]');
    expect(activeParentLinks).toHaveLength(1);
    expect(activeParentLinks[0].getAttribute("data-parent-page")).toBe("operations");

    const subNavs = document.querySelectorAll("[data-workspace-navigation]");
    expect(subNavs).toHaveLength(1);
    expect(subNavs[0].getAttribute("data-workspace-navigation")).toBe("operations");

    const activeTabs = document.querySelectorAll('[data-operations-tab][aria-current="page"]');
    expect(activeTabs).toHaveLength(1);
    expect(activeTabs[0].getAttribute("data-operations-tab")).toBe("comments");

    const menuButton = screen.getByRole("button", { name: /导航/ });
    expect(menuButton).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("告警与错误 · 2")).toBeInTheDocument();

    await user.click(menuButton);
    expect(onToggle).toHaveBeenCalledTimes(1);

    rerender(renderShell(true));
    expect(screen.getByRole("button", { name: /导航/ })).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledWith(true);
  });
});
