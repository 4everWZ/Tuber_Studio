(() => {
  window.addEventListener("studio-whep-diagnostic", ({detail}) => window.collectWhepDiagnostic(detail));
  const peers = [], frames = [], recordings = [];
  let active, lastBound, counter = 0, start, attachment = 0;
  const log = (stage, segment, fields = {}) => window.collectWhepDiagnostic({
    action: "oc_capture", stage, segment, ms: performance.now(), ...fields
  });
  const Native = RTCPeerConnection;
  window.RTCPeerConnection = class extends Native {
    constructor(...args) {
      super(...args);
      const segment = peers.push(this) - 1;
      log("peer_created", segment);
      this.addEventListener("track", ({track}) =>
        log("track_received", segment, {track: track.id, kind: track.kind}));
    }
  };
  function bind(video) {
    const stream = video.srcObject;
    const tracks = stream?.getVideoTracks?.() || [];
    const track = tracks.length === 1 ? tracks[0] : null;
    const peer = track && peers.find(candidate => candidate.getReceivers().some(rx => rx.track === track));
    if (active && active.stream === stream && active.track === track && active.peer === peer) return;
    if (active) {
      active.video.cancelVideoFrameCallback(active.callback);
      active.context?.close();
      active = null;
    }
    if (!peer) return;
    const segment = peers.indexOf(peer), binding = ++attachment;
    const record = {peer, segment, binding, video, track, stream, shown: false};
    active = lastBound = record;
    log("video_attached", segment, {binding, track: track.id});
    const picture = peer.getReceivers().find(rx => rx.track === track);
    let lastPresented;
    function frame(_, meta) {
      if (active !== record || video.srcObject !== stream || picture.track !== track ||
          track.readyState === "ended" || !stream.getVideoTracks().includes(track)) return;
      const show = meta.expectedDisplayTime;
      if (start === undefined) start = show;
      if (!record.shown) {
        record.shown = true;
        log("first_presented", segment, {binding, track: track.id, displayMs: show});
      }
      counter += lastPresented === undefined ? 1 : Math.max(0,meta.presentedFrames-lastPresented);
      lastPresented = meta.presentedFrames;
      window.presented = counter;
      const audio = peer.getReceivers().find(rx => stream.getAudioTracks().includes(rx.track));
      const a = audio?.getSynchronizationSources()[0], v = picture.getSynchronizationSources()[0];
      frames.push([show, segment, counter, v?.source, meta.rtpTimestamp, a?.source, a?.rtpTimestamp]);
      record.callback = video.requestVideoFrameCallback(frame);
    }
    record.callback = video.requestVideoFrameCallback(frame);
  }
  const media = HTMLMediaElement.prototype;
  const source = Object.getOwnPropertyDescriptor(media, "srcObject");
  const watches = new WeakMap();
  Object.defineProperty(media, "srcObject", {...source, set(stream) {
    source.set.call(this, stream);
    if (!this.matches("#private-call-dialog video")) return;
    watches.get(this)?.abort();
    if (stream?.getVideoTracks) {
      const watch = new AbortController();
      watches.set(this, watch);
      for (const event of ["addtrack", "removetrack"])
        stream.addEventListener(event, () => bind(this), {signal: watch.signal});
    }
    bind(this);
  }});
  document.addEventListener("playing", async event => {
    const record = active;
    if (!record || event.target !== record.video || record.recording) return;
    record.recording = true;
    const {stream, segment} = record;
    const rec = new MediaRecorder(stream, {mimeType:"video/webm;codecs=vp8,opus"});
    const chunks = [], done = new Promise(resolve => {rec.onstop=resolve;});
    rec.ondataavailable = e => chunks.push(e.data);
    rec.start(1000); recordings.push({rec, chunks, done, segment});
    const context = record.context = new AudioContext(); await context.resume();
    if (active !== record) return;
    const analyser = context.createAnalyser(); analyser.fftSize=1024;
    context.createMediaStreamSource(stream).connect(analyser);
    record.sound = () => { const buf = new Float32Array(1024); analyser.getFloatTimeDomainData(buf);
      return Math.sqrt(buf.reduce((acc,x)=>acc+x*x,0)/buf.length); };
  }, true);
  window.ocWindow = {
    status: () => {
      const sampled = active || lastBound;
      window.probePeer = sampled?.peer;
      window.probeVideo = sampled?.video;
      const attached = !!active && active.video.srcObject === active.stream && active.track.readyState !== "ended";
      const elementPlaying = attached && !active.video.paused && !active.video.muted;
      return {start, now:performance.now(), rms:active?.sound?.() || 0, segment:sampled?.segment ?? -1,
        binding:active?.binding ?? null, rmsAvailable:!!active?.sound, elementPlaying,
        playing:elementPlaying && active.shown};
    },
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
