class PrivateCallCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(16000);
    this.offset = 0;
    this.quiet = 0;
    this.speaking = false;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input?.length) return true;
    let squares = 0;
    for (const sample of input) {
      squares += sample * sample;
      this.samples[this.offset++] = Math.round(Math.max(-1, Math.min(1, sample)) * 32767);
      if (this.offset === this.samples.length) {
        this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
        this.samples = new Int16Array(16000);
        this.offset = 0;
      }
    }
    this.quiet = squares / input.length > 0.012 ** 2 ? 0 : this.quiet + input.length;
    const speaking = this.quiet === 0 || (this.speaking && this.quiet < sampleRate / 4);
    if (speaking !== this.speaking) this.port.postMessage({ speaking });
    this.speaking = speaking;
    return true;
  }
}

registerProcessor("private-call-capture", PrivateCallCapture);
