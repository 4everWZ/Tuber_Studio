"""Observe only outgoing RTP origins on a designated Linux interface to one test receiver."""

import argparse
import ipaddress
import json
import socket
import struct
import time


def observe(interface, destinations, seconds):
    allowed = {ipaddress.ip_address(host).packed for host in destinations}
    seen = set()
    with socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(3)) as listener:
        if interface != "any":
            listener.bind((interface, 0))
        listener.settimeout(1)
        print(json.dumps({"ready": True}), flush=True)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                header, address = listener.recvfrom(80)
            except TimeoutError:
                continue
            if address[2] != socket.PACKET_OUTGOING or len(header) < 40:
                continue
            if header[0] >> 4 == 4 and header[9] == 17 and header[16:20] in allowed:
                offset = (header[0] & 15) * 4 + 8
            elif header[0] >> 4 == 6 and header[6] == 17 and header[24:40] in allowed:
                offset = 48
            else:
                continue
            rtp = header[offset:]
            if len(rtp) < 12 or rtp[0] >> 6 != 2 or 192 <= rtp[1] <= 223:
                continue
            stamp, source = struct.unpack_from("!II", rtp, 4)
            if source not in seen:
                seen.add(source)
                print(json.dumps({"ssrc": source, "timestamp": stamp, "utc": time.time()}), flush=True)
        print(json.dumps({"drops": struct.unpack("II", listener.getsockopt(263, 6, 8))[1]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interface")
    parser.add_argument("seconds", type=int)
    parser.add_argument("destinations", nargs="+")
    args = parser.parse_args()
    observe(args.interface, args.destinations, args.seconds)
