"use strict";

window.InteractiveGamesUI = class {
  constructor({ api, notify = () => {} } = {}) {
    this.api = api;
    this.notify = notify;
    this.rules = null;
    this.gameStatus = null;
    this.drawGuessStatus = null;
    this.rulesDirty = false;
    this.rulesRequest = 0;
    this.saving = false;
    this.bound = false;
    this.bindEvents();
  }

  async init() {
    this.bindEvents();
    await this.refresh();
  }

  bindEvents() {
    if (this.bound) return;
    this.bound = true;
    // Game switch buttons
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-game-switch]");
      if (btn) {
        const gameId = btn.dataset.gameSwitch;
        await this.runAction(btn, () => this.switchGame(gameId));
      }

      const actBtn = e.target.closest("[data-game-action]");
      if (actBtn) {
        const action = actBtn.dataset.gameAction;
        await this.runAction(actBtn, () => this.executeAction(action));
      }

      if (e.target.id === "dg-start-btn") {
        await this.runAction(e.target, () => this.startDrawGuess());
      }
      if (e.target.id === "dg-stop-btn") {
        await this.runAction(e.target, () => this.stopDrawGuess());
      }
    });

    // Save rules form
    const rulesForm = document.getElementById("interactive-rules-form");
    if (rulesForm) {
      rulesForm.addEventListener("input", () => { this.rulesDirty = true; });
      rulesForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        await this.saveRulesFromForm();
      });
    }
  }

  async runAction(button, work) {
    if (button.disabled) return;
    button.disabled = true;
    try { await work(); }
    finally { button.disabled = false; this.renderDrawGuessCard(); }
  }

  async refresh() {
    await Promise.all([
      this.fetchGameStatus(),
      this.fetchDrawGuessStatus(),
      this.fetchRules(),
    ]);
  }

  async fetchGameStatus() {
    try {
      this.gameStatus = await this.api("/api/games/status");
      this.renderGameCard();
    } catch (e) {
      const state = document.getElementById("live-game-summary");
      if (state) state.textContent = `游戏状态读取失败：${e.message}`;
    }
  }

  async fetchDrawGuessStatus() {
    try {
      this.drawGuessStatus = await this.api("/api/draw-and-guess/status");
      this.renderDrawGuessCard();
    } catch (e) { const state = document.getElementById("dg-live-status"); if (state) state.textContent = `读取失败：${e.message}`; }
  }

  async fetchRules() {
    const request = ++this.rulesRequest;
    try {
      const data = await this.api("/api/interactive-rules");
      if (request !== this.rulesRequest) return;
      this.rules = data;
      if (!this.rulesDirty && !this.saving) this.populateRulesForm();
      this.renderDrawGuessCard();
    } catch (e) { this.ruleFeedback(`规则读取失败：${e.message}`, true); }
  }

  renderGameCard() {
    const card = document.getElementById("live-game-panel");
    if (!card || !this.gameStatus) return;

    const st = this.gameStatus.active_status || {};
    const nameEl = document.getElementById("live-game-name");
    const summaryEl = document.getElementById("live-game-summary");
    const activityEl = document.getElementById("live-game-activity");
    const actionListEl = document.getElementById("live-game-actions");

    if (nameEl) nameEl.textContent = st.display_name || "未知游戏";
    if (summaryEl) summaryEl.textContent = st.summary || "准备中...";
    if (activityEl) activityEl.textContent = `当前行动: ${st.current_activity || "待命"}`;

    if (actionListEl) {
      actionListEl.replaceChildren();
      const actions =
        this.gameStatus.active_game_id === "stardew_valley"
          ? [
              { action: "water_crops", label: "💧 浇水" },
              { action: "chop_wood", label: "🪓 砍树" },
              { action: "go_fishing", label: "🎣 钓鱼" },
              { action: "harvest", label: "🌾 收获作物" },
              { action: "eat_snack", label: "🍪 吃点心补体力" },
            ]
          : [
              { action: "plant_sunflower", label: "🌻 种向日葵" },
              { action: "plant_peashooter", label: "🌱 种豌豆射手" },
              { action: "plant_wallnut", label: "🥜 种坚果墙" },
              { action: "use_cherry_bomb", label: "💣 樱桃炸弹" },
            ];

      for (const a of actions) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "button quiet small";
        btn.dataset.gameAction = a.action;
        btn.textContent = a.label;
        actionListEl.appendChild(btn);
      }
    }
  }

  renderDrawGuessCard() {
    const stEl = document.getElementById("dg-live-status");
    const wordEl = document.getElementById("dg-live-word");
    const btnStart = document.getElementById("dg-start-btn");
    const btnStop = document.getElementById("dg-stop-btn");

    if (!stEl || !this.drawGuessStatus) return;

    const active = this.drawGuessStatus.active;
    if (active) {
      stEl.textContent = `作画中 (${Math.ceil(this.drawGuessStatus.remaining)}s)`;
      stEl.className = "badge warn";
      if (wordEl) wordEl.textContent = `分类: ${this.drawGuessStatus.category || ""} | 提示: ${this.drawGuessStatus.hint || "逐步揭晓中"}`;
      if (btnStart) btnStart.disabled = true;
      if (btnStop) btnStop.disabled = false;
    } else {
      stEl.textContent = this.drawGuessStatus.state === "finished" ? "上一局结束" : "空闲中";
      stEl.className = "badge";
      if (wordEl) {
        wordEl.textContent = this.drawGuessStatus.word
          ? `上一题: 【${this.drawGuessStatus.word}】 ${this.drawGuessStatus.winner ? "胜者: " + this.drawGuessStatus.winner : "(无人猜出)"}`
          : "等待送礼触发或手动开启";
      }
      if (btnStart) btnStart.disabled = !this.rules;
      if (btnStop) btnStop.disabled = true;
    }
  }

  async switchGame(gameId) {
    try {
      const data = await this.api("/api/games/switch", {
        method: "POST",
        body: { game_id: gameId },
      });
      this.notify(data.spoken_text || `已切换游戏: ${gameId}`);
      await this.fetchGameStatus();
    } catch (e) {
      this.notify(e.message, true);
    }
  }

  async executeAction(action) {
    try {
      const data = await this.api("/api/games/action", {
        method: "POST",
        body: { action },
      });
      this.notify(`游戏指令【${action}】: ${data.message}`);
      await this.fetchGameStatus();
    } catch (e) {
      this.notify(e.message, true);
    }
  }

  async startDrawGuess() {
    try {
      const data = await this.api("/api/draw-and-guess/start", {
        method: "POST",
        body: { trigger_user: "主播", duration_sec: this.rules?.draw_and_guess?.round_duration_sec ?? 60 },
      });
      this.notify(data.spoken_text || "已开启你画我猜！");
      await this.fetchDrawGuessStatus();
    } catch (e) {
      this.notify(e.message, true);
    }
  }

  async stopDrawGuess() {
    try {
      const data = await this.api("/api/draw-and-guess/stop", {
        method: "POST",
      });
      this.notify(`已结束本轮你画我猜，答案是：【${data.word}】`);
      await this.fetchDrawGuessStatus();
    } catch (e) {
      this.notify(e.message, true);
    }
  }

  populateRulesForm() {
    if (!this.rules) return;
    const form = document.getElementById("interactive-rules-form");
    if (!form) return;

    // Song request
    const sr = this.rules.song_request || {};
    const songMode = document.getElementById("rule-song-mode");
    const songCoin = document.getElementById("rule-song-coin");
    const songGifts = document.getElementById("rule-song-gifts");
    if (songMode) songMode.value = sr.mode || "free";
    if (songCoin) songCoin.value = sr.min_coin ?? 0;
    if (songGifts) songGifts.value = (sr.gift_names || []).join(", ");

    // Draw and guess
    const dg = this.rules.draw_and_guess || {};
    const dgEnable = document.getElementById("rule-dg-enabled");
    const dgMode = document.getElementById("rule-dg-mode");
    const dgCoin = document.getElementById("rule-dg-coin");
    const dgGifts = document.getElementById("rule-dg-gifts");
    const dgDuration = document.getElementById("rule-dg-duration");
    if (dgEnable) dgEnable.checked = Boolean(dg.enabled);
    if (dgMode) dgMode.value = dg.trigger_mode || "gift_only";
    if (dgCoin) dgCoin.value = dg.min_coin ?? 10;
    if (dgGifts) dgGifts.value = (dg.gift_names || []).join(", ");
    if (dgDuration) dgDuration.value = dg.round_duration_sec || 60;
  }

  async saveRulesFromForm() {
    if (this.saving) return;
    const songMode = document.getElementById("rule-song-mode")?.value || "free";
    const songCoin = parseInt(document.getElementById("rule-song-coin")?.value || "0", 10);
    const songGifts = (document.getElementById("rule-song-gifts")?.value || "")
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);

    const dgEnable = document.getElementById("rule-dg-enabled")?.checked ?? true;
    const dgMode = document.getElementById("rule-dg-mode")?.value || "gift_only";
    const dgCoin = parseInt(document.getElementById("rule-dg-coin")?.value || "10", 10);
    const dgGifts = (document.getElementById("rule-dg-gifts")?.value || "")
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean);
    const dgDuration = parseInt(document.getElementById("rule-dg-duration")?.value || "60", 10);

    const payload = {
      ...(this.rules || {}),
      song_request: {
        ...(this.rules?.song_request || {}),
        mode: songMode,
        min_coin: songCoin,
        gift_names: songGifts,
      },
      draw_and_guess: {
        ...(this.rules?.draw_and_guess || {}),
        enabled: dgEnable,
        trigger_mode: dgMode,
        min_coin: dgCoin,
        gift_names: dgGifts,
        round_duration_sec: dgDuration,
      },
    };
    const controls = [...document.getElementById("interactive-rules-form").querySelectorAll("input, select, button")].map(control => [control, control.disabled]);

    try {
      this.saving = true;
      ++this.rulesRequest;
      for (const [control] of controls) control.disabled = true;
      this.ruleFeedback("正在保存…");
      const data = await this.api("/api/interactive-rules", {
        method: "PUT",
        body: payload,
      });
      this.rules = data;
      this.rulesDirty = false;
      this.populateRulesForm();
      this.ruleFeedback("互动规则已保存。");
      this.notify("互动与礼物门槛规则保存成功！");
    } catch (e) {
      this.ruleFeedback(`保存失败：${e.message}`, true);
      this.notify(e.message, true);
    } finally {
      this.saving = false;
      for (const [control, disabled] of controls) control.disabled = disabled;
    }
  }
  ruleFeedback(text, error = false) {
    const state = document.getElementById("interactive-rules-state");
    if (state) { state.textContent = text; state.classList.toggle("error-text", error); }
  }
};
