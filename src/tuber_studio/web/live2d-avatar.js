"use strict";

(() => {
  let scripts;
  function install(urls) {
    scripts ??= urls.reduce((chain, url) => chain.then(() => new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.onload = resolve;
      script.onerror = reject;
      document.head.append(script);
    })), Promise.resolve());
    return scripts;
  }

  window.Live2DAvatar = class {
    constructor(parent, manifest, onState) {
      this.parent = parent;
      this.manifest = manifest;
      this.onState = onState;
      this.canvas = document.createElement("canvas");
      this.canvas.className = "live2d-avatar";
      this.canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none";
      this.canvas.setAttribute("aria-hidden", "true");
      this.canvas.addEventListener("webglcontextlost", () => this.fallback());
      this.mouth = 0;
      this.load();
    }

    async load() {
      try {
        await install(this.manifest.scripts);
        if (this.disposed) return;
        const { Application, Ticker, live2d } = window.PIXI;
        const { Live2DModel } = live2d;
        Live2DModel.registerTicker(Ticker);
        this.app = new Application({view: this.canvas, backgroundAlpha: 0, autoDensity: true});
        this.app.ticker.maxFPS = 30;
        const model = await Live2DModel.from(this.manifest.model, {autoInteract: false});
        if (this.disposed) { model.destroy(); return; }
        this.app.stage.addChild(model);
        this.model = model;
        const internal = model.internalModel;
        const core = internal.coreModel;
        const index = core.getModel().parameters.ids.indexOf(this.manifest.mouth_parameter);
        if (index < 0) throw new Error("缺少嘴部参数");
        internal.on("beforeModelUpdate", () => core.setParameterValueByIndex(index, this.mouth));
        this.parent.append(this.canvas);
        this.observer = new ResizeObserver(() => this.fit());
        this.observer.observe(this.parent);
        this.fit();
        this.onState(true);
        this.update(this.lastPlayback);
      } catch (_) {
        this.fallback();
      }
    }

    fit() {
      const width = this.parent.clientWidth, height = this.parent.clientHeight;
      const model = this.model;
      if (!width || !height || !model) return;
      this.app.renderer.resize(width, height);
      const bounds = model.getLocalBounds();
      const scale = Math.min(width / bounds.width, height / bounds.height);
      if (!Number.isFinite(scale) || scale <= 0) return;
      model.scale.set(scale);
      model.position.set(
        (width - bounds.width * scale) / 2 - bounds.x * scale,
        (height - bounds.height * scale) / 2 - bounds.y * scale,
      );
    }

    update(playback) {
      this.lastPlayback = playback;
      this.mouth = Math.min(1, 5 * Math.max(0, playback?.mouth || 0));
      const animation = playback?.animation_id || "idle", emotion = playback?.emotion || "idle";
      if (!this.model) return;
      if (animation !== this.animation) {
        const motion = this.manifest.motions[animation];
        if (motion) this.model.motion(motion.group, motion.index, 3).catch(() => this.fallback());
        this.animation = animation;
      }
      if (emotion !== this.emotion) {
        const expression = this.manifest.expressions[emotion];
        if (expression) this.model.expression(expression).catch(() => this.fallback());
        else this.model.internalModel.motionManager.expressionManager?.resetExpression();
        this.emotion = emotion;
      }
    }

    fallback() {
      if (this.disposed) return;
      this.destroy();
      this.onState(false);
    }

    destroy() {
      this.disposed = true;
      this.observer?.disconnect();
      this.app?.destroy(false, {children: true});
      this.canvas.remove();
      this.model = null;
    }
  };
})();
