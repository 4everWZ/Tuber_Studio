"use strict";

(() => {
  const container = document.getElementById("draw-guess-container");
  const svg = document.getElementById("drawing-board");
  const catBadge = document.getElementById("category-badge");
  const timerBadge = document.getElementById("timer-badge");
  const triggerUser = document.getElementById("trigger-user");
  const clueText = document.getElementById("clue-text");
  const winnerBanner = document.getElementById("winner-banner");
  const bannerTitle = document.getElementById("banner-title");
  const bannerWinner = document.getElementById("banner-winner");
  const bannerAnswer = document.getElementById("banner-answer");

  let lastStrokesCount = 0;
  let hideTimer = null;

  function renderStrokes(strokes) {
    if (strokes.length === lastStrokesCount) return;
    lastStrokesCount = strokes.length;
    svg.replaceChildren();

    for (const s of strokes) {
      let el;
      if (s.type === "path") {
        el = document.createElementNS("http://www.w3.org/2000/svg", "path");
        el.setAttribute("d", s.d);
        el.setAttribute("fill", s.fill || "none");
        el.setAttribute("stroke", s.stroke || "#38bdf8");
        el.setAttribute("stroke-width", s.width || 4);
        el.setAttribute("stroke-linecap", "round");
        el.setAttribute("stroke-linejoin", "round");
      } else if (s.type === "circle") {
        el = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        el.setAttribute("cx", s.cx);
        el.setAttribute("cy", s.cy);
        el.setAttribute("r", s.r);
        el.setAttribute("fill", s.fill || "#38bdf8");
        if (s.stroke) {
          el.setAttribute("stroke", s.stroke);
          el.setAttribute("stroke-width", s.width || 2);
        }
      } else if (s.type === "ellipse") {
        el = document.createElementNS("http://www.w3.org/2000/svg", "ellipse");
        el.setAttribute("cx", s.cx);
        el.setAttribute("cy", s.cy);
        el.setAttribute("rx", s.rx);
        el.setAttribute("ry", s.ry);
        el.setAttribute("fill", s.fill || "#38bdf8");
      }
      if (el) svg.appendChild(el);
    }
  }

  async function poll() {
    try {
      const res = await fetch("/api/draw-and-guess/overlay", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();

      if (data.active || (data.state === "finished" && data.word)) {
        if (hideTimer) {
          clearTimeout(hideTimer);
          hideTimer = null;
        }
        container.hidden = false;

        catBadge.textContent = "分类: " + (data.category || "趣味猜猜看");
        timerBadge.textContent = Math.ceil(data.remaining) + "s";
        triggerUser.textContent = data.trigger_user || "观众";
        clueText.textContent = data.hint ? "提示: " + data.hint : "";

        renderStrokes(data.revealed_strokes || []);

        if (data.state === "finished") {
          winnerBanner.classList.remove("hidden");
          if (data.winner) {
            bannerTitle.textContent = "🎉 抢答成功！";
            bannerWinner.innerHTML = `恭喜 <strong>${data.winner}</strong> 猜对！`;
            bannerAnswer.textContent = `答案：【${data.word}】`;
          } else {
            bannerTitle.textContent = "⏰ 时间到！";
            bannerWinner.textContent = "没有人猜出来哦~";
            bannerAnswer.textContent = `答案其实是：【${data.word}】`;
          }
        } else {
          winnerBanner.classList.add("hidden");
        }
      } else {
        if (!container.hidden && !hideTimer) {
          hideTimer = setTimeout(() => {
            container.hidden = true;
            svg.replaceChildren();
            lastStrokesCount = 0;
          }, 4000);
        }
      }
    } catch (e) {
      // transient network error
    }
  }

  setInterval(poll, 800);
  poll();
})();
