"""Measure desktop browser playout from a deployed gateway using a registered audio-source Avatar."""

import argparse
import base64
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from evaluations.omni_chat_loop import ROOT, connection

OFFER = """async ({codec, bufferMs}) => {
    const pc = window.probePeer = new RTCPeerConnection({iceServers: []});
    const video = window.probeVideo = document.createElement('video');
    document.body.append(video);
    const stream = new MediaStream();
    pc.ontrack = ({track}) => stream.addTrack(track);
    const audio = pc.addTransceiver('audio', {direction: 'recvonly'}).receiver;
    const receiver = pc.addTransceiver('video', {direction: 'recvonly'});
    for (const {receiver} of pc.getTransceivers()) {
        if (bufferMs !== null) {
            if (!('jitterBufferTarget' in receiver)) throw Error('Receiver buffer control unavailable');
            receiver.jitterBufferTarget = bufferMs;
        }
    }
    if (codec) {
        const caps = RTCRtpReceiver.getCapabilities('video').codecs;
        const preferred = caps.filter(c => c.mimeType.toLowerCase() === 'video/' + codec);
        if (!preferred.length) throw Error('Requested video codec unavailable');
        const recovery = caps.filter(c => ['video/rtx', 'video/red', 'video/ulpfec'].includes(c.mimeType));
        receiver.setCodecPreferences([...preferred, ...recovery]);
    }
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(Error('ICE gathering timeout')), 8000);
        const complete = () => {
            if (pc.iceGatheringState === 'complete') { clearTimeout(timer); resolve(); }
        };
        pc.onicegatheringstatechange = complete;
        complete();
    });
    video.srcObject = stream;
    window.presented = 0;
    window.rtpPairs = [];
    function frame(_, metadata) {
        window.presented = metadata.presentedFrames;
        const source = audio.getSynchronizationSources()[0];
        if (source && Number.isFinite(source.rtpTimestamp) && Number.isFinite(metadata.rtpTimestamp)) {
            window.rtpPairs.push([metadata.expectedDisplayTime, metadata.rtpTimestamp, source.source, source.rtpTimestamp]);
        }
        video.requestVideoFrameCallback(frame);
    }
    video.requestVideoFrameCallback(frame);
    return pc.localDescription.sdp;
}"""

SAMPLE = """async () => {
    const pc = window.probePeer, v = window.probeVideo;
    const report = await pc.getStats();
    const sample = {ms: performance.now(), presented: window.presented, time: v.currentTime,
                    width: v.videoWidth, height: v.videoHeight, state: pc.connectionState};
    for (const stat of report.values()) {
        if (stat.type === 'inbound-rtp') {
            sample[stat.kind] = {ssrc: stat.ssrc, codec: report.get(stat.codecId)?.mimeType, packets: stat.packetsReceived,
                lost: stat.packetsLost, decoded: stat.framesDecoded, freezes: stat.freezeCount,
                energy: stat.totalAudioEnergy, nack: stat.nackCount, jitter: stat.jitter,
                bufferSeconds: stat.jitterBufferDelay, emitted: stat.jitterBufferEmittedCount,
                freezeSeconds: stat.totalFreezesDuration, dropped: stat.framesDropped};
        }
        if (stat.type === 'transport' && stat.selectedCandidatePairId) {
            const pair = report.get(stat.selectedCandidatePairId);
            const local = report.get(pair.localCandidateId), remote = report.get(pair.remoteCandidateId);
            sample.route = {local: local.candidateType, remote: remote.candidateType, protocol: remote.protocol,
                            remoteAddress: remote.address, rtt: pair.currentRoundTripTime};
        }
    }
    return sample;
}"""


def measure(origin, token, args):
    pcm = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(args.wav),
            "-t",
            "4",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-f",
            "s16le",
            "pipe:1",
        ]
    )
    child_env = {k: v for k, v in os.environ.items() if not k.startswith(("OMNI_", "TUBER_"))}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.getenv("CHROMIUM_EXECUTABLE"),
            env=child_env,
            args=["--no-proxy-server", "--autoplay-policy=no-user-gesture-required"],
        )
        try:
            page = browser.new_page()
            response = page.goto(origin + "/livez")
            if response.status != 200 or urlsplit(page.url) != urlsplit(origin + "/livez"):
                raise RuntimeError("Browser did not reach the specified gateway directly")

            def request(method, path, status, data=None, *, sdp=False):
                return page.evaluate(
                    """async ({method, path, status, data, sdp, token}) => {
                    const headers = {Authorization: "Bearer " + token};
                    if (data !== null) headers["Content-Type"] = sdp ? "application/sdp" : "application/json";
                    if (path.endsWith("/audio")) window.audioSubmission = performance.now();
                    const reply = await fetch(path, {method, headers, mode: "same-origin", redirect: "error",
                        body: data === null ? undefined : sdp ? data : JSON.stringify(data),
                        signal: AbortSignal.timeout(22000)});
                    if (reply.status !== status) throw Error(method + " returned HTTP " + reply.status);
                    if (status === 204) return;
                    return sdp ? await reply.text() : await reply.json();
                }""",
                    {
                        "method": method,
                        "path": path,
                        "status": status,
                        "data": data,
                        "sdp": sdp,
                        "token": token,
                    },
                )

            detail = request("GET", "/v1/avatar/avatars/" + args.rig, 200)
            if (detail.get("driver"), detail.get("state")) != ("inochi2d", "ready"):
                raise RuntimeError("Expected a ready registered Inochi2D rig")
            session = request(
                "POST",
                "/v1/avatar/sessions",
                201,
                {
                    "avatar_id": args.rig,
                    "source": {"type": "audio"},
                },
            )
            path = "/v1/avatar/sessions/" + session["id"]
            try:
                offer = page.evaluate(OFFER, {"codec": args.codec, "bufferMs": args.buffer_ms})
                answer = request("POST", path + "/whep", 201, offer, sdp=True)
                page.evaluate("sdp => probePeer.setRemoteDescription({type:'answer', sdp})", answer)
                page.evaluate("() => { probeVideo.play().catch(() => {}); }")
                page.wait_for_function("presented > 0", timeout=20000)
                samples = [page.evaluate(SAMPLE)]
                page.evaluate("""async () => {
                    const ctx = new AudioContext();
                    await ctx.resume();
                    const meter = ctx.createAnalyser(), silent = ctx.createGain();
                    silent.gain.value = 0;
                    ctx.createMediaStreamSource(probeVideo.srcObject).connect(meter).connect(silent).connect(ctx.destination);
                    const wave = new Float32Array(meter.fftSize);
                    window.firstSound = null;
                    const timer = setInterval(() => {
                        meter.getFloatTimeDomainData(wave);
                        const rms = Math.sqrt(wave.reduce((sum, v) => sum + v * v, 0) / wave.length);
                        if (rms > 0.012) { window.firstSound = performance.now(); clearInterval(timer); }
                    }, 20);
                }""")
                request(
                    "POST",
                    path + "/audio",
                    200,
                    {
                        "generation": session["generation"],
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    },
                )
                for _ in range(args.seconds):
                    page.wait_for_timeout(1000)
                    samples.append(page.evaluate(SAMPLE))
                pairs = page.evaluate("rtpPairs")
                sound_delay = page.evaluate(
                    "firstSound === null ? null : firstSound - window.audioSubmission"
                )
            finally:
                request("DELETE", path, 204)
            return {
                "driver": detail["driver"],
                "browser": browser.version,
                "whep": 201,
                "buffer_ms": args.buffer_ms,
                "secure_context": page.evaluate("isSecureContext"),
                "samples": samples,
                "rtp_pairs": pairs,
                "submission_to_first_sound_ms": sound_delay,
            }
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rig", default="regular")
    parser.add_argument("--wav", type=Path, default=ROOT / "assets/whale/sample1.wav")
    parser.add_argument("--seconds", type=int, default=15, choices=range(5, 121), metavar="5..120")
    parser.add_argument("--codec", choices=["vp8", "h264"])
    parser.add_argument("--buffer-ms", type=int, choices=range(0, 4001), metavar="0..4000")
    parser.add_argument(
        "--output", required=True, type=Path, help="Private JSON including the selected ICE address"
    )
    args = parser.parse_args()
    service = connection(os.environ)
    if not service:
        parser.error("Set OMNI_GATEWAY_URL and the ordinary Studio credential.")
    result = measure(*service, args)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    first, last = result["samples"][0], result["samples"][-1]
    if (
        last["state"] != "connected"
        or last["presented"] <= first["presented"]
        or last["video"]["decoded"] <= first["video"]["decoded"]
        or last["audio"]["energy"] <= first["audio"]["energy"]
    ):
        raise SystemExit("Receiver failed to play the submitted media")
