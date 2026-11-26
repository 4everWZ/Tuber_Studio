import React from "react";
import { evolvePath, getLength, getPointAtLength } from "@remotion/paths";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import {
  getAvatarRightClearance,
  getThemePalette,
  getToneAccent,
} from "../theme";
import {
  CJK_FONT_STACK,
  MONO_FONT_STACK,
  type DiagramEdge,
  type DiagramNode,
  type SceneTemplateProps,
} from "../types";
import { getRevealStep } from "../utils/beatSync";

interface PositionedNode {
  node: DiagramNode;
  cx: number;
  cy: number;
  w: number;
  h: number;
  col: number;
}

const SVG_W = 1360;
const SVG_H = 540;

function layoutNodes(nodes: DiagramNode[]): {
  positioned: PositionedNode[];
  laneHeaders: Array<{ lane: string; x: number }>;
} {
  if (nodes.length === 0) {
    return { positioned: [], laneHeaders: [] };
  }

  const explicitLanes: string[] = [];
  for (const n of nodes) {
    const lane = (n.lane || "").trim();
    if (lane && !explicitLanes.includes(lane)) {
      explicitLanes.push(lane);
    }
  }

  const positioned: PositionedNode[] = [];
  const laneHeaders: Array<{ lane: string; x: number }> = [];

  if (explicitLanes.length >= 2) {
    const colCount = explicitLanes.length;
    const marginX = colCount >= 5 ? 126 : 140;
    const usableW = SVG_W - marginX * 2;
    const nodeW = Math.min(264, Math.max(192, Math.floor((usableW / colCount) * 0.82)));

    explicitLanes.forEach((laneName, colIdx) => {
      const colX =
        colCount === 1
          ? SVG_W / 2
          : marginX + (colIdx / (colCount - 1)) * usableW;
      laneHeaders.push({ lane: laneName, x: colX });

      const colNodes = nodes.filter((n) => (n.lane || "").trim() === laneName);
      const count = colNodes.length;
      const nodeH = count >= 4 ? 86 : 104;
      const topPad = 74;
      const bottomPad = 42;
      const usableH = SVG_H - topPad - bottomPad;

      colNodes.forEach((node, rowIdx) => {
        const cy =
          count === 1
            ? topPad + usableH / 2
            : topPad + 58 + (rowIdx / (count - 1)) * (usableH - 116);
        positioned.push({ node, cx: colX, cy, w: nodeW, h: nodeH, col: colIdx });
      });
    });

    const unlaned = nodes.filter((n) => !(n.lane || "").trim());
    unlaned.forEach((node, idx) => {
      positioned.push({
        node,
        cx: marginX + ((idx + 0.5) / Math.max(1, unlaned.length)) * usableW,
        cy: SVG_H - 76,
        w: 200,
        h: 82,
        col: idx,
      });
    });

    return { positioned, laneHeaders };
  }

  const cols = Math.min(4, nodes.length);
  const rows = Math.ceil(nodes.length / cols);
  const marginX = 155;
  const usableW = SVG_W - marginX * 2;
  const nodeW = Math.min(256, Math.max(184, Math.floor((usableW / Math.max(1, cols)) * 0.78)));
  const nodeH = rows > 1 ? 104 : 118;

  nodes.forEach((node, idx) => {
    const col = idx % cols;
    const row = Math.floor(idx / cols);
    const rowItems = Math.min(cols, nodes.length - row * cols);
    const cx =
      rowItems === 1
        ? SVG_W / 2
        : marginX + (col / (rowItems - 1)) * usableW;
    const cy =
      rows === 1
        ? SVG_H / 2
        : 120 + (row / (rows - 1)) * (SVG_H - 240);
    positioned.push({ node, cx, cy, w: nodeW, h: nodeH, col });
  });

  return { positioned, laneHeaders };
}

function buildEdgePath(src: PositionedNode, dst: PositionedNode): string {
  const dx = dst.cx - src.cx;
  const dy = dst.cy - src.cy;

  if (Math.abs(dx) < 36) {
    // Same column vertical connection
    const sy = dy >= 0 ? src.cy + src.h / 2 : src.cy - src.h / 2;
    const ty = dy >= 0 ? dst.cy - dst.h / 2 : dst.cy + dst.h / 2;
    const bend = Math.max(40, Math.abs(ty - sy) * 0.45);
    return `M ${src.cx} ${sy} C ${src.cx + 38} ${sy + (dy >= 0 ? bend : -bend)}, ${dst.cx + 38} ${ty - (dy >= 0 ? bend : -bend)}, ${dst.cx} ${ty}`;
  }

  const forward = dx > 0;
  const sx = forward ? src.cx + src.w / 2 : src.cx - src.w / 2;
  const tx = forward ? dst.cx - dst.w / 2 : dst.cx + dst.w / 2;
  const ctrlOffset = Math.max(60, Math.abs(tx - sx) * 0.48);
  const c1x = forward ? sx + ctrlOffset : sx - ctrlOffset;
  const c2x = forward ? tx - ctrlOffset : tx + ctrlOffset;
  return `M ${sx} ${src.cy} C ${c1x} ${src.cy}, ${c2x} ${dst.cy}, ${tx} ${dst.cy}`;
}

export const ArchitectureFlow: React.FC<SceneTemplateProps> = ({
  scene,
  activeCue,
  sceneCues = [],
  activeLineIndex = 0,
  avatarLayout = "compact",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const palette = getThemePalette(scene.theme);
  const vp = scene.visual_props || {};

  const fallbackNodes: DiagramNode[] = (vp.bullets || []).slice(0, 6).map((b, idx) => ({
    id: `step_${idx + 1}`,
    label: b,
    tone: (["sky", "mint", "amber", "coral"] as const)[idx % 4],
  }));

  const nodes =
    vp.diagram_nodes && vp.diagram_nodes.length > 0
      ? vp.diagram_nodes
      : fallbackNodes.length > 0
        ? fallbackNodes
        : [
            { id: "input", label: scene.heading, tone: "sky" as const },
            { id: "process", label: vp.subtitle || "核心处理流水线", tone: "mint" as const },
          ];

  const edges: DiagramEdge[] =
    vp.diagram_edges && vp.diagram_edges.length > 0
      ? vp.diagram_edges
      : nodes.slice(0, -1).map((n, idx) => ({
          source: n.id,
          target: nodes[idx + 1].id,
        }));

  const { positioned, laneHeaders } = layoutNodes(nodes);
  const nodeMap = new Map(positioned.map((p) => [p.node.id, p]));

  const revealStep = getRevealStep(
    activeCue,
    activeLineIndex,
    nodes.length,
    frame,
    scene.duration_frames,
    sceneCues.length
  );

  const focusQuery = (activeCue?.visual_focus || "").trim().toLowerCase();
  let focusedIdx = revealStep;
  if (focusQuery) {
    const matched = nodes.findIndex(
      (n) =>
        n.id.toLowerCase() === focusQuery ||
        n.label.toLowerCase().includes(focusQuery)
    );
    if (matched >= 0) {
      focusedIdx = matched;
    }
  }
  const focusedNode = nodes[focusedIdx];
  const rightClearance = getAvatarRightClearance(avatarLayout);

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        gap: 14,
        padding: `20px ${rightClearance}px 128px 44px`,
        fontFamily: CJK_FONT_STACK,
      }}
    >
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 16 }}>
        <div>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "4px 14px",
              borderRadius: 999,
              backgroundColor: palette.accentSoft,
              border: `1px solid ${palette.borderStrong}`,
              fontSize: 14,
              fontWeight: 800,
              color: palette.pillText,
              letterSpacing: 1,
              marginBottom: 8,
            }}
          >
            <span>ARCHITECTURE FLOW</span>
          </div>
          <div style={{ fontSize: 42, fontWeight: 900, color: palette.ink, lineHeight: 1.15 }}>
            {scene.heading}
          </div>
          {vp.subtitle ? (
            <div style={{ fontSize: 20, fontWeight: 600, color: palette.inkSoft, marginTop: 4 }}>
              {vp.subtitle}
            </div>
          ) : null}
        </div>
        {vp.citation_label ? (
          <div
            style={{
              padding: "6px 14px",
              borderRadius: 10,
              backgroundColor: palette.surface,
              border: `1px solid ${palette.border}`,
              fontFamily: MONO_FONT_STACK,
              fontSize: 15,
              fontWeight: 700,
              color: palette.muted,
            }}
          >
            {vp.citation_label}
          </div>
        ) : null}
      </div>

      {/* Multi-lane SVG diagram surface */}
      <div
        style={{
          flex: 1,
          minHeight: 0,
          borderRadius: 24,
          backgroundColor: palette.surface,
          border: `1px solid ${palette.border}`,
          boxShadow: palette.cardShadow,
          backdropFilter: "blur(10px)",
          position: "relative",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <svg
          viewBox={`0 0 ${SVG_W} ${SVG_H}`}
          style={{ width: "100%", height: "100%", flex: 1 }}
        >
          <defs>
            <marker
              id="arch-arrow"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill={palette.accent} />
            </marker>
          </defs>

          {/* Lane headers & column separators */}
          {laneHeaders.map((lh, idx) => (
            <g key={lh.lane}>
              <rect
                x={lh.x - 132}
                y={16}
                width={264}
                height={34}
                rx={17}
                fill={palette.surfaceMuted}
                stroke={palette.border}
                strokeWidth={1}
              />
              <text
                x={lh.x}
                y={38}
                textAnchor="middle"
                fill={palette.inkSoft}
                fontSize={16}
                fontWeight={800}
                fontFamily={MONO_FONT_STACK}
              >
                {lh.lane}
              </text>
              {idx > 0 ? (
                <line
                  x1={(laneHeaders[idx - 1].x + lh.x) / 2}
                  y1={24}
                  x2={(laneHeaders[idx - 1].x + lh.x) / 2}
                  y2={SVG_H - 24}
                  stroke={palette.border}
                  strokeDasharray="6 8"
                  strokeWidth={1}
                />
              ) : null}
            </g>
          ))}

          {/* Progressive edges + traveling luminous data pulses */}
          {edges.map((edge, idx) => {
            const src = nodeMap.get(edge.source);
            const dst = nodeMap.get(edge.target);
            if (!src || !dst) return null;

            const pathD = buildEdgePath(src, dst);
            const edgeDelay = 8 + idx * 5;
            const edgeProgress = interpolate(frame, [edgeDelay, edgeDelay + 20], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            });
            const evolved = evolvePath(edgeProgress, pathD);
            const pathLen = Math.max(1, getLength(pathD));

            const isEdgeActive =
              focusedNode &&
              (edge.source === focusedNode.id || edge.target === focusedNode.id);

            const pulseCycle = ((frame * 0.018 + idx * 0.27) % 1 + 1) % 1;
            const pulsePt =
              edgeProgress > 0.65
                ? getPointAtLength(pathD, pulseCycle * pathLen)
                : null;
            const midPt = getPointAtLength(pathD, pathLen * 0.5);

            return (
              <g key={`${edge.source}-${edge.target}-${idx}`}>
                {/* Base subtle track */}
                <path
                  d={pathD}
                  fill="none"
                  stroke={palette.border}
                  strokeWidth={2}
                  strokeOpacity={0.35}
                />
                {/* Evolved animated path */}
                <path
                  d={pathD}
                  fill="none"
                  stroke={isEdgeActive ? palette.highlight : palette.accent}
                  strokeWidth={isEdgeActive ? 3.5 : 2.5}
                  strokeLinecap="round"
                  strokeDasharray={evolved.strokeDasharray}
                  strokeDashoffset={evolved.strokeDashoffset}
                  markerEnd={edgeProgress > 0.92 ? "url(#arch-arrow)" : undefined}
                />
                {/* Traveling luminous pulse dot */}
                {pulsePt ? (
                  <g>
                    <circle
                      cx={pulsePt.x}
                      cy={pulsePt.y}
                      r={isEdgeActive ? 9 : 7}
                      fill={isEdgeActive ? palette.highlight : palette.accent}
                      fillOpacity={0.28}
                    />
                    <circle
                      cx={pulsePt.x}
                      cy={pulsePt.y}
                      r={isEdgeActive ? 4.5 : 3.5}
                      fill={isEdgeActive ? palette.highlight : "#ffffff"}
                    />
                  </g>
                ) : null}
                {/* Edge label */}
                {edge.label && midPt && edgeProgress > 0.7 ? (
                  <g transform={`translate(${midPt.x}, ${midPt.y - 12})`}>
                    <rect
                      x={-54}
                      y={-13}
                      width={108}
                      height={24}
                      rx={8}
                      fill={palette.surfaceElevated}
                      stroke={palette.border}
                      strokeWidth={1}
                    />
                    <text
                      x={0}
                      y={3}
                      textAnchor="middle"
                      fill={palette.inkSoft}
                      fontSize={13}
                      fontWeight={700}
                      fontFamily={CJK_FONT_STACK}
                    >
                      {edge.label.slice(0, 14)}
                    </text>
                  </g>
                ) : null}
              </g>
            );
          })}

          {/* Diagram nodes */}
          {positioned.map((p, idx) => {
            const nodeEnter = spring({
              frame: Math.max(0, frame - idx * 4),
              fps,
              config: { damping: 15, stiffness: 150 },
            });
            const isFocused = focusedNode?.id === p.node.id;
            const toneStyle = getToneAccent(p.node.tone, palette);
            const focusScale = isFocused
              ? 1.04 + Math.sin(frame * 0.14) * 0.012
              : 1;
            const nodeOpacity = focusedNode && !isFocused && frame > 18 ? 0.72 : 1;

            return (
              <g
                key={p.node.id}
                transform={`translate(${p.cx}, ${p.cy}) scale(${(interpolate(nodeEnter, [0, 1], [0.85, 1]) * focusScale).toFixed(3)})`}
                opacity={nodeEnter * nodeOpacity}
              >
                {isFocused ? (
                  <rect
                    x={-p.w / 2 - 6}
                    y={-p.h / 2 - 6}
                    width={p.w + 12}
                    height={p.h + 12}
                    rx={20}
                    fill="none"
                    stroke={toneStyle.stroke}
                    strokeWidth={2}
                    strokeOpacity={0.55}
                  />
                ) : null}
                <rect
                  x={-p.w / 2}
                  y={-p.h / 2}
                  width={p.w}
                  height={p.h}
                  rx={16}
                  fill={isFocused ? palette.surfaceElevated : palette.surface}
                  stroke={isFocused ? toneStyle.stroke : palette.border}
                  strokeWidth={isFocused ? 2.5 : 1.5}
                />
                <rect
                  x={-p.w / 2}
                  y={-p.h / 2}
                  width={8}
                  height={p.h}
                  rx={4}
                  fill={toneStyle.stroke}
                />
                <text
                  x={4}
                  y={p.node.sublabel ? -6 : 6}
                  textAnchor="middle"
                  fill={palette.ink}
                  fontSize={19}
                  fontWeight={800}
                  fontFamily={CJK_FONT_STACK}
                >
                  {p.node.label.slice(0, 14)}
                </text>
                {p.node.sublabel ? (
                  <text
                    x={4}
                    y={19}
                    textAnchor="middle"
                    fill={toneStyle.text}
                    fontSize={12.5}
                    fontWeight={600}
                    fontFamily={MONO_FONT_STACK}
                  >
                    {p.node.sublabel.length > 22
                      ? `${p.node.sublabel.slice(0, 21)}…`
                      : p.node.sublabel}
                  </text>
                ) : null}
              </g>
            );
          })}
        </svg>

        {/* Active explanation callout strip */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 16,
            padding: "12px 22px",
            borderTop: `1px solid ${palette.border}`,
            backgroundColor: palette.surfaceElevated,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span
              style={{
                padding: "3px 12px",
                borderRadius: 999,
                backgroundColor: palette.accentSoft,
                border: `1px solid ${palette.borderStrong}`,
                fontSize: 14,
                fontWeight: 800,
                color: palette.pillText,
              }}
            >
              焦点节点：{focusedNode?.label || scene.heading}
            </span>
            <span style={{ fontSize: 18, fontWeight: 600, color: palette.inkSoft }}>
              {focusedNode?.sublabel || activeCue?.text || vp.subtitle || ""}
            </span>
          </div>
          <span style={{ fontFamily: MONO_FONT_STACK, fontSize: 14, fontWeight: 700, color: palette.muted }}>
            {focusedIdx + 1} / {nodes.length}
          </span>
        </div>
      </div>
    </div>
  );
};
