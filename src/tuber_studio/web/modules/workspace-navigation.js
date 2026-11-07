import { $, notice } from "./core.js?v=20261105.32";

const workspaceSections = {
  operations: ["automation", "comments", "messages", "dynamics", "automation-records", "automation-settings"],
  live: ["live", "live-scene", "live-music", "live-interaction"],
  characters: ["characters", "voices", "assets"],
  settings: ["settings", "accounts", "overview", "issues"],
};

export function workspaceSection(page) {
  return Object.keys(workspaceSections).find(section => workspaceSections[section].includes(page));
}

export function initWorkspaceNavigation(model, loadPage) {
  const scrollPositions = new Map();

  const sidebar = $("workspace-sidebar");
  const workspaceMenu = $("workspace-menu");
  const workspaceMain = document.querySelector(".main-shell");
  function closeWorkspaceMenu(restoreFocus = false) {
    const wasOpen = workspaceMenu.getAttribute("aria-expanded") === "true";
    $("workspace").classList.remove("nav-open");
    workspaceMenu.setAttribute("aria-expanded", "false");
    workspaceMain.inert = false;
    if (wasOpen && restoreFocus) workspaceMenu.focus();
  }
  workspaceMenu.onclick = () => {
    const open = $("workspace").classList.toggle("nav-open");
    workspaceMenu.setAttribute("aria-expanded", String(open));
    workspaceMain.inert = open;
    if (open) (sidebar.querySelector("[aria-current]") || sidebar.querySelector("a"))?.focus();
  };
  $("close-workspace-menu").onclick = () => closeWorkspaceMenu(true);
  $("workspace-backdrop").onclick = () => closeWorkspaceMenu(true);
  matchMedia("(max-width: 760px)").addEventListener("change", () => closeWorkspaceMenu());
  sidebar.onclick = event => {
    if (event.target.closest("a")) closeWorkspaceMenu(true);
  };
  document.addEventListener("keydown", event => { if (event.key === "Escape") closeWorkspaceMenu(true); });
  window.addEventListener("hashchange", changePage);

  async function changePage() {
    const requested = location.hash.slice(1) || "automation";
    const [root, routeKind, routeId, taskId] = requested.split("/");
    const legacyAnchors = {
      "voice-cache-card": "cache-panel", storage: "cache-panel", providers: "providers-panel",
      memory: "daily-memory-card",
    };
    const target = $(Object.hasOwn(legacyAnchors, root) ? legacyAnchors[root] : root);
    const anchor = target?.hasAttribute("data-workspace-anchor") ? target : null;
    const page = anchor?.closest("[data-section]")?.dataset.section ||
      (root === "automation" && routeKind === "round" ? "automation-records" : root);
    const pages = {
      comments: "评论与草稿", automation: "自主运营", messages: "私信", dynamics: "动态",
      "automation-records": "运行记录", "automation-settings": "刷视频与运营设置", issues: "告警与错误",
      live: "直播控制", "live-scene": "场景编排", "live-music": "曲库与点歌",
      "live-interaction": "互动与游戏",
      characters: "角色工作室", voices: "语音", assets: "素材与表情包",
      settings: "推理与模型", accounts: "账号与权限", overview: "存储与维护",
    };
    const previous = model.page;
    scrollPositions.set(previous, window.scrollY);
    model.page = Object.hasOwn(pages, page) ? page : "automation";
    document.querySelectorAll("[data-section]").forEach(section => {
      section.hidden = section.dataset.section !== model.page;
    });
    document.querySelectorAll("[data-page]").forEach(link => {
      const active = link.dataset.parentPage ? link.dataset.parentPage === workspaceSection(model.page) : link.dataset.page === model.page;
      link.classList.toggle("selected", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-workspace-navigation]").forEach(nav => {
      nav.hidden = nav.dataset.workspaceNavigation !== workspaceSection(model.page);
    });
    document.querySelectorAll("[data-workspace-tab]").forEach(link => {
      const active = link.dataset.workspaceTab === model.page;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    const selectedOperation = model.page === "automation" ? "overview" : model.page === "automation-records" ? "overview" : model.page;
    document.querySelectorAll("[data-operations-tab]").forEach(link => {
      const active = link.dataset.operationsTab === selectedOperation;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.title = `${pages[model.page]} · Tuber Studio`;
    closeWorkspaceMenu();
    if (!anchor && previous !== model.page) window.scrollTo(0, scrollPositions.get(model.page) || 0);

    try {
      await loadPage({ anchor, routeKind, routeId, taskId });
      if (model.authenticated && anchor && model.page === page) {
        for (let element = anchor; element; element = element.parentElement) {
          if (element instanceof HTMLDetailsElement) element.open = true;
        }
        anchor.scrollIntoView();
      }
    } catch (error) {
      notice(error.message, true);
    }
  }
  return changePage;
}
