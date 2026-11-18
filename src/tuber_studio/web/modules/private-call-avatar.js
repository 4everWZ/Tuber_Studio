export class CallAvatar {
  constructor(canvas, label) {
    this.canvas = canvas;
    this.label = label;
    this.motion = matchMedia("(prefers-reduced-motion: reduce)");
    this.version = 0;
    this.reset();
  }

  reset(name = "语音通话") {
    this.version++;
    for (const image of [this.still, this.open]) image?.close();
    this.still = this.open = this.shown = null;
    this.canvas.hidden = true;
    this.canvas.dataset.mouth = "closed";
    this.canvas.setAttribute("aria-label", name);
    this.label.textContent = name;
    this.label.hidden = false;
  }

  async load(config, signal) {
    this.reset(config.character);
    const version = this.version;
    const decode = async path => {
      if (!path || !path.startsWith("/assets/")) return null;
      try {
        const response = await fetch(path, {signal, mode: "same-origin", redirect: "error"});
        if (response.ok) return await createImageBitmap(await response.blob());
      } catch {}
      return null;
    };
    const [still, open] = await Promise.all([decode(config.avatar?.still), decode(config.avatar?.speaking)]);
    const fallback = !still && config.avatar?.fallback !== config.avatar?.still ? await decode(config.avatar?.fallback) : null;
    if (signal.aborted || version !== this.version) {
      for (const image of [still, open, fallback]) image?.close();
      return;
    }
    this.still = still || fallback;
    if (still) this.open = open;
    else open?.close();
    this.draw(false);
  }

  draw(speaking) {
    const image = speaking && !this.motion.matches && this.open || this.still;
    if (!image || image === this.shown) return;
    const canvas = this.canvas, ctx = canvas.getContext("2d");
    if (!ctx) return;
    const scale = Math.min(canvas.width / image.width, canvas.height / image.height);
    const width = image.width * scale, height = image.height * scale;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, (canvas.width - width) / 2, (canvas.height - height) / 2, width, height);
    this.shown = image;
    canvas.dataset.mouth = image === this.open ? "open" : "closed";
    canvas.hidden = false;
    this.label.hidden = true;
  }
}
