async profile => {
  const video = document.querySelector("#private-call-dialog video");
  const context = new AudioContext();
  await context.resume();
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  context.createMediaStreamSource(video.srcObject).connect(analyser);
  const wave = new Float32Array(analyser.fftSize);
  const canvas = document.createElement("canvas");
  canvas.width = 99;
  canvas.height = 85;
  const paint = canvas.getContext("2d", {willReadFrequently: true});
  const [cx, cy] = profile === "q" ? [347, 465] : [370, 418];

  // The masters have distinct lip positions on their common 640×720 canvas.
  function mouthArea() {
    const sx = video.videoWidth / 640, sy = video.videoHeight / 720;
    paint.drawImage(video, (cx - 49) * sx, (cy - 42) * sy, 99 * sx, 85 * sy, 0, 0, 99, 85);
    const {data} = paint.getImageData(0, 0, 99, 85);
    const mask = new Uint8Array(99 * 85);
    for (let i = 0; i < mask.length; i++) {
      const red = data[4 * i], green = data[4 * i + 1];
      mask[i] = Number(red - green > 50 && green < 207 && red > 136);
    }
    let largest = 0;
    for (let i = 0; i < mask.length; i++) {
      if (!mask[i]) continue;
      mask[i] = 0;
      const pixels = [i];
      let sumX = 0, sumY = 0;
      for (let n = 0; n < pixels.length; n++) {
        const pixel = pixels[n], x = pixel % 99;
        sumX += x;
        sumY += Math.floor(pixel / 99);
        const neighbors = [pixel - 99, pixel + 99];
        if (x > 0) neighbors.push(pixel - 1);
        if (x < 98) neighbors.push(pixel + 1);
        for (const neighbor of neighbors) {
          if (mask[neighbor]) {
            mask[neighbor] = 0;
            pixels.push(neighbor);
          }
        }
      }
      if (Math.abs(sumX / pixels.length - 49) < 36 && Math.abs(sumY / pixels.length - 42) < 32)
        largest = Math.max(largest, pixels.length);
    }
    return largest;
  }

  const points = [], sizes = new Set();
  let frame;
  function sample() {
    sizes.add(video.videoWidth + "x" + video.videoHeight);
    analyser.getFloatTimeDomainData(wave);
    const rms = Math.sqrt(wave.reduce((sum, x) => sum + x * x, 0) / wave.length);
    points.push([video.currentTime, rms, mouthArea()]);
    frame = video.requestVideoFrameCallback(sample);
  }
  frame = video.requestVideoFrameCallback(sample);

  const recorder = new MediaRecorder(video.srcObject, {mimeType: "video/webm;codecs=vp8,opus"});
  const chunks = [];
  recorder.ondataavailable = event => chunks.push(event.data);
  recorder.start();
  window.finishProbe = async () => {
    video.cancelVideoFrameCallback(frame);
    const ended = new Promise(resolve => { recorder.onstop = resolve; });
    recorder.stop();
    await ended;
    await context.close();
    const reader = new FileReader();
    const audio = new Promise((resolve, reject) => {
      reader.onload = () => resolve(reader.result.split(",")[1]);
      reader.onerror = () => reject(reader.error);
    });
    reader.readAsDataURL(new Blob(chunks));
    return {points, sizes: [...sizes], audio: await audio,
      playing: !video.paused && !video.muted && video.volume > 0, frames: video.getVideoPlaybackQuality().totalVideoFrames};
  };
}
