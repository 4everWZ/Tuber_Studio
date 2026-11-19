(() => {
  const peers = [], frames = [], recordings = [];
  let previous, lastPresented, counter = 0, raf, start, sound;
  const Native = RTCPeerConnection;
  window.RTCPeerConnection = class extends Native {
    constructor(...args) { super(...args); peers.push(this); }
  };
  document.addEventListener("playing", async event => {
    const video = event.target, stream = video.srcObject;
    if (video.tagName !== "VIDEO" || !stream || stream === previous) return;
    previous = stream;
    const peer = peers.find(p => p.getReceivers().some(r => stream.getTracks().includes(r.track)));
    if (!peer) return;
    const segment = peers.indexOf(peer);
    window.probePeer = peer; window.probeVideo = video;
    if (raf) video.cancelVideoFrameCallback(raf);
    lastPresented = null;
    const audio = peer.getReceivers().find(r => r.track.kind === "audio");
    const picture = peer.getReceivers().find(r => r.track.kind === "video");
    function frame(_, meta) {
      const show = meta.expectedDisplayTime;
      if (start === undefined) start = show;
      counter += lastPresented === null ? 1 : Math.max(0,meta.presentedFrames-lastPresented);
      lastPresented = meta.presentedFrames;
      window.presented = counter;
      const a = audio.getSynchronizationSources()[0], v = picture.getSynchronizationSources()[0];
      frames.push([show, segment, counter, v?.source, meta.rtpTimestamp, a?.source, a?.rtpTimestamp]);
      raf = video.requestVideoFrameCallback(frame);
    }
    raf = video.requestVideoFrameCallback(frame);
    const rec = new MediaRecorder(stream, {mimeType:"video/webm;codecs=vp8,opus"});
    const chunks = [], done = new Promise(resolve => {rec.onstop=resolve;});
    rec.ondataavailable = e => chunks.push(e.data);
    rec.start(1000); recordings.push({rec, chunks, done, segment});
    const context = new AudioContext(); await context.resume();
    const analyser = context.createAnalyser(); analyser.fftSize=1024;
    context.createMediaStreamSource(stream).connect(analyser);
    sound = () => { const buf = new Float32Array(1024); analyser.getFloatTimeDomainData(buf);
      return Math.sqrt(buf.reduce((acc,x)=>acc+x*x,0)/buf.length); };
  }, true);
  window.ocWindow = {
    status: () => ({start, now:performance.now(), rms:sound?.() || 0, segment:peers.length-1,
      playing: !!window.probeVideo && !probeVideo.paused && !probeVideo.muted && !!probeVideo.srcObject}),
    frames,
    closeMedia: async () => {
      for (const {rec} of recordings) if(rec.state!=="inactive") rec.stop();
      await Promise.all(recordings.map(item=>item.done)); return recordings.length;
    },
    media: index => new Promise(resolve => {const reader=new FileReader();
      reader.onload=()=>resolve(reader.result.split(",")[1]);
      reader.readAsDataURL(new Blob(recordings[index].chunks));})
  };
})();
