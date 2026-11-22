"""Exercise the full-window collector in Chromium using local canvas tracks, without signaling or ICE."""

import os

from playwright.sync_api import sync_playwright

from evaluations.live.oc_call_window import INIT
from evaluations.live.workstation_whep import SAMPLE


def main():
    with sync_playwright() as driver:
        browser = driver.chromium.launch(
            executable_path=os.getenv("CHROMIUM_EXECUTABLE") or driver.chromium.executable_path,
            args=["--disable-background-networking", "--autoplay-policy=no-user-gesture-required"],
        )
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.set_content('<div id="private-call-dialog"><video autoplay playsinline></video></div>')
        page.evaluate("""() => {
          window.rows = [];
          window.collectWhepDiagnostic = row => rows.push(row);
          window.video = document.querySelector('video');
          const register = video.requestVideoFrameCallback.bind(video);
          video.requestVideoFrameCallback = callback => {
            window.lastCallback = callback;
            return register(callback);
          };
          window.RTCPeerConnection = class extends EventTarget {
            getReceivers() { return this.rx; }
            async getStats() { return new Map([['v',{type:'inbound-rtp',kind:'video',ssrc:this.ssrc}]]); }
          };
          window.make = ssrc => {
            const canvas = document.createElement('canvas'), paint = canvas.getContext('2d');
            const stream = canvas.captureStream(25);
            const audio = new AudioContext();
            stream.addTrack(audio.createMediaStreamDestination().stream.getAudioTracks()[0]);
            let color = 0;
            setInterval(() => {paint.fillStyle = 'rgb(' + (++color % 255) + ',0,0)'; paint.fillRect(0,0,20,20)}, 40);
            const peer = new RTCPeerConnection();
            peer.ssrc = ssrc;
            peer.rx = stream.getTracks().map(track => ({track,getSynchronizationSources:()=>[{source:ssrc}]}));
            for (const {track} of peer.rx) peer.dispatchEvent(Object.assign(new Event('track'), {track}));
            return {peer,stream};
          };
        }""")
        page.add_script_tag(path=INIT)
        page.evaluate("() => { window.old = make(101); video.srcObject = old.stream }")
        page.wait_for_function("ocWindow.status().playing")
        page.evaluate("""() => {
          window.stale = lastCallback;
          window.unattached = make(202);
          window.next = make(303);
          window.poll = ocWindow.status();
          video.srcObject = next.stream;
        }""")
        assert page.evaluate("poll.segment") == 0
        assert page.evaluate(SAMPLE)["video"]["ssrc"] == 101
        page.evaluate("""() => {
          const count = ocWindow.frames.length;
          stale(0,{expectedDisplayTime:1,presentedFrames:100000});
          if (ocWindow.frames.length !== count) throw Error('detached track counted');
        }""")
        page.wait_for_function("ocWindow.status().segment === 2 && ocWindow.status().playing")
        assert page.evaluate(SAMPLE)["video"]["ssrc"] == 303
        assert page.evaluate("ocWindow.frames.filter(row=>row[1]===2).every(row=>row[3]===303)")
        rows = page.evaluate("rows")
        for segment in (0, 2):
            stages = [row for row in rows if row["segment"] == segment]
            assert [row["stage"] for row in stages] == [
                "peer_created",
                "track_received",
                "track_received",
                "video_attached",
                "first_presented",
            ]
            assert stages[-1]["binding"] == stages[-2]["binding"]
            assert stages[-1]["track"] == stages[-2]["track"]
        assert not any(row["segment"] == 1 and row["stage"] == "first_presented" for row in rows)
        page.evaluate("""() => {
          video.srcObject = null;
          video.srcObject = unattached.stream;
          const state = ocWindow.status();
          if (state.playing || state.segment !== 1) throw Error('attachment counted as presentation');
          video.srcObject = null;
        }""")
        assert not page.evaluate("ocWindow.status().playing")
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
