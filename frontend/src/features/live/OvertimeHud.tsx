import styles from "./OverlayPlayer.module.css";
import type { OvertimeHudProps } from "./consoleTypes";

const COLD_TIER_LABELS: Record<number, string> = {
  0: "活跃互动",
  1: "暖场闲聊",
  2: "自娱整活",
  3: "深夜伴播",
};

const RUNDOWN_KIND_LABELS: Record<string, string> = {
  chat: "闲聊",
  draw_and_guess: "你画我猜",
  turtle_soup: "海龟汤",
  roast_radio: "锐评电台",
  song_party: "听歌会",
  stardew_valley: "星露谷",
  pvz: "植物大战僵尸",
};

function formatCountdown(seconds: number | undefined): string {
  const safe = Math.max(0, Math.floor(seconds ?? 0));
  const hrs = Math.floor(safe / 3600);
  const mins = Math.floor((safe % 3600) / 60);
  const secs = safe % 60;
  if (hrs > 0) {
    return `${String(hrs).padStart(2, "0")}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function OvertimeHud({
  shows,
  overtime,
  coldDirector,
  rundown,
  compact = false,
  className,
}: OvertimeHudProps) {
  const activeOvertime = overtime !== undefined ? overtime : shows?.overtime ?? null;
  const activeColdDirector =
    coldDirector !== undefined ? coldDirector : shows?.cold_director ?? null;
  const activeRundown = rundown !== undefined ? rundown : shows?.rundown ?? null;

  const threshold =
    activeOvertime?.threshold && activeOvertime.threshold > 0 ? activeOvertime.threshold : 100;
  const energy = Math.max(0, activeOvertime?.energy ?? 0);
  const energyPct = Math.min(100, Math.round((energy / threshold) * 100));
  const hasColdTier = typeof activeColdDirector?.tier === "number";
  const showRundownPill = Boolean(activeRundown?.active);

  if (!activeOvertime?.active && !hasColdTier && !showRundownPill) {
    return null;
  }

  const stackClass = [compact ? styles.hudStackCompact : styles.hudStack, className ?? ""]
    .filter(Boolean)
    .join(" ");

  const currentSeg = activeRundown?.current_segment;
  const segmentTitle =
    currentSeg?.title ||
    (currentSeg?.kind ? RUNDOWN_KIND_LABELS[currentSeg.kind] || currentSeg.kind : "节目环节");

  return (
    <aside className={stackClass} aria-label="直播剧场状态浮窗">
      {activeOvertime?.active && (
        <section id="overtime-hud" className={styles.overtimeHud}>
          <div className={styles.overtimeHeader}>
            <span>🍚 白饭加班能量条</span>
            <span id="overtime-countdown">
              下班倒计时 {formatCountdown(activeOvertime.remaining_seconds)}
            </span>
          </div>
          <div className={styles.overtimeMeta}>
            <span id="overtime-energy-pct">能量 {energyPct}%</span>
            <span id="overtime-minutes">
              已加班 +{activeOvertime.overtime_minutes ?? 0} 分钟
              {activeOvertime.max_overtime_minutes ? ` / ${activeOvertime.max_overtime_minutes}m` : ""}
            </span>
          </div>
          <div
            className={styles.overtimeTrack}
            role="progressbar"
            aria-valuenow={energyPct}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div className={styles.overtimeFill} style={{ width: `${energyPct}%` }} />
          </div>
          <p id="overtime-status-line" className={styles.overtimeLine}>
            {activeOvertime.last_event_line || "准点打卡干白饭中，别随便投喂续命啊！"}
          </p>
        </section>
      )}
      {hasColdTier && activeColdDirector && (
        <div
          id="cold-director-badge"
          data-cold-tier={activeColdDirector.tier}
          className={styles.coldTierBadge}
        >
          <span>🌙 冷场导演 Tier {activeColdDirector.tier}</span>
          <span>{COLD_TIER_LABELS[activeColdDirector.tier ?? 0] || "待命中"}</span>
        </div>
      )}
      {showRundownPill && activeRundown && (
        <div id="rundown-stage-pill" className={styles.rundownPill}>
          <span>🎬 {segmentTitle}</span>
          <span>
            {activeRundown.paused
              ? "已暂停"
              : activeRundown.waiting_graceful_finish
                ? "等待本局结束"
                : formatCountdown(activeRundown.remaining_seconds)}
          </span>
        </div>
      )}
    </aside>
  );
}
