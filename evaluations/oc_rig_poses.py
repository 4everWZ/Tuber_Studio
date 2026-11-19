"""Check the two OC models using the workstation's isolated Inochi renderer."""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat
from services.avatar.inochi import InochiRenderer
from services.avatar.speech_mouth import HOP, mouth_controls


def difference(first, second):
    return ImageChops.difference(first, second)


def check(rigs, sources, pcm_file, output):
    pcm = pcm_file.read_bytes()
    packets = [pcm[start : start + 2 * HOP] for start in range(0, len(pcm) - 2 * HOP + 1, 2 * HOP)]
    voiced = max(packets, key=lambda packet: mouth_controls(packet)[0])
    opening, form = mouth_controls(voiced)
    if opening <= 0.3:
        raise ValueError("No clearly voiced PCM packet")
    output.mkdir(parents=True, exist_ok=True)
    for variant, mouth_box in [("regular", (300, 400, 370, 447)), ("q", (300, 445, 360, 496))]:
        renderer = InochiRenderer(rigs / variant)
        with Image.open(sources / (variant + ".png")) as source:
            reference = ImageOps.pad(source.convert("RGB"), (640, 720), method=Image.Resampling.LANCZOS)
        neutral = renderer.render_pose({})
        neutral_error = ImageStat.Stat(difference(reference, neutral)).mean
        if max(neutral_error) > 2:
            raise ValueError(f"{variant}: neutral pose does not reproduce source: {neutral_error}")
        still = renderer.render_pcm(0, bytes(2 * HOP))
        speaking = renderer.render_pcm(0, voiced)
        speech_delta = difference(still, speaking)
        bounds = speech_delta.getbbox()
        if not bounds or any(
            (
                bounds[0] < mouth_box[0],
                bounds[1] < mouth_box[1],
                bounds[2] > mouth_box[2],
                bounds[3] > mouth_box[3],
            )
        ):
            raise ValueError(f"{variant}: wrong speech motion: {bounds}")
        rounded = renderer.render_pose({"MouthOpen": 1, "MouthForm": -1})
        spread = renderer.render_pose({"MouthOpen": 1, "MouthForm": 1})
        if not difference(rounded, spread).getbbox():
            raise ValueError(f"{variant}: MouthForm has no effect")
        closed_eyes = renderer.render_pose({"BlinkL": 1, "BlinkR": 1})
        gaze = renderer.render_pose({"GazeX": 1, "GazeY": -1})
        head = renderer.render_pose({"HeadX": 1, "HeadY": -1, "Breath": 1})
        poses = (reference, neutral, head, gaze, closed_eyes, still, speaking, rounded, spread)
        review = Image.new("RGB", (3 * 640, 3 * 720))
        for index, pose in enumerate(poses):
            review.paste(pose, (index % 3 * 640, index // 3 * 720))
        review.save(output / (variant + "-poses.png"))
        print(
            json.dumps(
                dict(
                    variant=variant,
                    textures=len(renderer.layers),
                    neutral_mae=neutral_error,
                    pcm_open=opening,
                    pcm_form=form,
                    speech_bounds=bounds,
                )
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("rigs", "sources", "pcm_file", "output"):
        parser.add_argument(argument, type=Path)
    check(**vars(parser.parse_args()))
