export type PageId =
  | "chat"
  | "automation"
  | "comments"
  | "messages"
  | "dynamics"
  | "automation-records"
  | "automation-settings"
  | "live"
  | "live-scene"
  | "live-music"
  | "live-settings"
  | "live-interaction"
  | "characters"
  | "voices"
  | "assets"
  | "settings"
  | "personalization"
  | "accounts"
  | "overview"
  | "issues";

export type WorkspaceSection = "operations" | "live" | "characters" | "settings";

export interface TopNavItem {
  page: PageId;
  parentPage?: WorkspaceSection;
  href: string;
  label: string;
}

export interface SubNavTab {
  tab: string;
  page: PageId;
  href: string;
  label: string;
  isOperationsTab?: boolean;
}

export interface ParsedRoute {
  page: PageId;
  section: WorkspaceSection | undefined;
  anchor: string | null;
  routeKind: string | undefined;
  routeId: string | undefined;
  taskId: string | undefined;
  hash: string;
}

export const WORKSPACE_SECTIONS: Record<WorkspaceSection, readonly PageId[]> = {
  operations: [
    "automation",
    "comments",
    "messages",
    "dynamics",
    "automation-records",
    "automation-settings",
  ],
  live: ["live", "live-scene", "live-music", "live-settings", "live-interaction"],
  characters: ["characters", "voices", "assets"],
  settings: ["settings", "personalization", "accounts", "overview", "issues"],
};

export const PAGE_TITLES: Record<PageId, string> = {
  chat: "对话与脉冲",
  comments: "评论与草稿",
  automation: "渠道活动",
  messages: "私信",
  dynamics: "动态",
  "automation-records": "运行记录",
  "automation-settings": "刷视频与运营设置",
  issues: "告警与错误",
  live: "直播台",
  "live-scene": "场景编排",
  "live-music": "曲库与点歌",
  "live-settings": "直播设置",
  "live-interaction": "互动与游戏",
  characters: "角色与声音",
  voices: "语音",
  assets: "素材与表情包",
  settings: "设置",
  personalization: "个性化",
  accounts: "账号与权限",
  overview: "存储与维护",
};

export const TOP_NAV_ITEMS: readonly TopNavItem[] = [
  { page: "chat", href: "#chat", label: "对话与脉冲" },
  { page: "automation", parentPage: "operations", href: "#automation", label: "渠道活动" },
  { page: "live", parentPage: "live", href: "#live", label: "直播台" },
  { page: "characters", parentPage: "characters", href: "#characters", label: "角色与声音" },
  { page: "settings", parentPage: "settings", href: "#settings", label: "设置" },
];

export const SUB_NAV_TABS: Record<WorkspaceSection, readonly SubNavTab[]> = {
  operations: [
    { tab: "overview", page: "automation", href: "#automation", label: "活动", isOperationsTab: true },
    { tab: "comments", page: "comments", href: "#comments", label: "评论处理", isOperationsTab: true },
    { tab: "messages", page: "messages", href: "#messages", label: "私信", isOperationsTab: true },
    { tab: "dynamics", page: "dynamics", href: "#dynamics", label: "动态", isOperationsTab: true },
    {
      tab: "automation-records",
      page: "automation-records",
      href: "#automation-records",
      label: "运行记录",
      isOperationsTab: true,
    },
    {
      tab: "automation-settings",
      page: "automation-settings",
      href: "#automation-settings",
      label: "运营设置",
      isOperationsTab: true,
    },
  ],
  live: [
    { tab: "live", page: "live", href: "#live", label: "直播台" },
    { tab: "live-scene", page: "live-scene", href: "#live-scene", label: "场景与编排" },
    { tab: "live-music", page: "live-music", href: "#live-music", label: "曲库与点歌" },
    { tab: "live-settings", page: "live-settings", href: "#live-settings", label: "直播设置" },
    { tab: "live-interaction", page: "live-interaction", href: "#live-interaction", label: "互动玩法与游戏" },
  ],
  characters: [
    { tab: "characters", page: "characters", href: "#characters", label: "角色卡" },
    { tab: "voices", page: "voices", href: "#voices", label: "语音与音色" },
    { tab: "assets", page: "assets", href: "#assets", label: "素材与表情包" },
  ],
  settings: [
    { tab: "settings", page: "settings", href: "#settings", label: "基础设置" },
    { tab: "personalization", page: "personalization", href: "#personalization", label: "个性化" },
    { tab: "accounts", page: "accounts", href: "#accounts", label: "账号与登录" },
    { tab: "overview", page: "overview", href: "#overview", label: "存储与维护" },
    { tab: "issues", page: "issues", href: "#issues", label: "告警与错误" },
  ],
};

const LEGACY_ANCHOR_ALIASES: Record<string, string> = {
  "voice-cache-card": "cache-panel",
  storage: "cache-panel",
  providers: "providers-panel",
  memory: "daily-memory-card",
};

const WORKSPACE_ANCHOR_PAGES: Record<string, PageId> = {
  "account-settings": "accounts",
  "comment-base-settings": "accounts",
  "operations-controls": "automation-settings",
  "discovery-source-form": "automation-settings",
  "operation-round-settings": "automation-settings",
  "operations-permission-matrix": "automation-settings",
  "dynamic-policy-settings": "automation-settings",
  "message-policy-settings": "automation-settings",
  "comment-acquisition": "comments",
  "comment-import": "comments",
  "comment-review": "comments",
  "live-game-panel": "live-interaction",
  "interactive-rules-card": "live-interaction",
  "cache-panel": "overview",
  "daily-memory-card": "personalization",
  "providers-panel": "settings",
};

export function workspaceSection(page: PageId): WorkspaceSection | undefined {
  return (Object.keys(WORKSPACE_SECTIONS) as WorkspaceSection[]).find((section) =>
    WORKSPACE_SECTIONS[section].includes(page)
  );
}

export function isKnownPage(value: string): value is PageId {
  return Object.prototype.hasOwnProperty.call(PAGE_TITLES, value);
}

export function parseHashRoute(rawHash: string): ParsedRoute {
  const normalizedHash = rawHash.startsWith("#") ? rawHash : `#${rawHash}`;
  const requested = normalizedHash.slice(1) || "chat";
  const [root, routeKind, routeId, taskId] = requested.split("/");

  const resolvedTarget = Object.prototype.hasOwnProperty.call(LEGACY_ANCHOR_ALIASES, root)
    ? LEGACY_ANCHOR_ALIASES[root]
    : root;

  const anchor = Object.prototype.hasOwnProperty.call(WORKSPACE_ANCHOR_PAGES, resolvedTarget)
    ? resolvedTarget
    : null;

  let candidatePage: string;
  if (anchor) {
    candidatePage = WORKSPACE_ANCHOR_PAGES[anchor];
  } else if (root === "automation" && routeKind === "round") {
    candidatePage = "automation-records";
  } else {
    candidatePage = root;
  }

  const page: PageId = isKnownPage(candidatePage) ? candidatePage : "automation";
  return {
    page,
    section: workspaceSection(page),
    anchor,
    routeKind,
    routeId,
    taskId,
    hash: normalizedHash,
  };
}
