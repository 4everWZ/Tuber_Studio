# /// script
# requires-python = ">=3.11"
# dependencies = ["Pillow>=11,<13", "numpy>=2,<3", "opencv-python-headless>=4.10,<5"]
# ///
"""Build the two private Inochi2D puppets from the owner's unmodified design sheets."""

import argparse
import io
import json
import math
from pathlib import Path
import struct

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps


SIZE = (640, 720)
CASES = {
    "regular": {
        "filename": "original.png",
        "crop": (910, 32, 1184, 295),
        "mouth": [
            (337, 414),
            (350, 403),
            (373, 395),
            (393, 391),
            (400, 397),
            (400, 416),
            (393, 433),
            (380, 442),
            (365, 445),
            (348, 437),
            (338, 422),
        ],
        "lips": [(340, 414), (351, 411), (370, 406), (386, 399), (394, 394)],
        "eyes": [
            (
                [
                    (238, 375),
                    (246, 366),
                    (249, 356),
                    (263, 347),
                    (283, 340),
                    (303, 343),
                    (314, 356),
                    (312, 368),
                    (296, 379),
                    (277, 389),
                    (255, 387),
                    (247, 383),
                    (239, 382),
                ],
                [
                    (271, 354),
                    (281, 348),
                    (295, 347),
                    (304, 354),
                    (309, 363),
                    (301, 374),
                    (288, 382),
                    (277, 377),
                    (271, 369),
                ],
                [(240, 375), (252, 377), (271, 377), (288, 372), (305, 363), (311, 358)],
            ),
            (
                [
                    (365, 323),
                    (373, 305),
                    (384, 297),
                    (401, 291),
                    (421, 290),
                    (437, 295),
                    (459, 300),
                    (463, 306),
                    (449, 312),
                    (444, 327),
                    (424, 338),
                    (391, 342),
                    (378, 338),
                ],
                [
                    (388, 308),
                    (398, 303),
                    (418, 302),
                    (430, 309),
                    (430, 322),
                    (423, 332),
                    (394, 338),
                    (389, 326),
                ],
                [(371, 322), (385, 328), (407, 327), (427, 322), (443, 310), (456, 305)],
            ),
        ],
        "head": [
            (0, 54),
            (640, 54),
            (640, 380),
            (592, 374),
            (548, 355),
            (525, 355),
            (495, 401),
            (443, 452),
            (429, 486),
            (367, 497),
            (355, 470),
            (312, 453),
            (287, 449),
            (313, 493),
            (324, 539),
            (311, 581),
            (285, 625),
            (261, 633),
            (246, 615),
            (268, 566),
            (261, 532),
            (242, 505),
            (176, 478),
            (107, 501),
            (66, 508),
            (105, 455),
            (120, 420),
            (47, 422),
            (0, 420),
        ],
        "fin": [
            (74, 497),
            (111, 456),
            (151, 402),
            (175, 423),
            (203, 447),
            (188, 465),
            (150, 486),
            (105, 502),
        ],
        "bow": [
            (496, 225),
            (511, 226),
            (530, 234),
            (551, 224),
            (565, 225),
            (569, 242),
            (565, 269),
            (552, 276),
            (534, 266),
            (518, 281),
            (507, 277),
        ],
        "fringe": [
            (307, 142),
            (335, 157),
            (358, 209),
            (357, 251),
            (352, 289),
            (369, 324),
            (356, 342),
            (329, 346),
            (303, 330),
            (289, 305),
            (282, 273),
        ],
    },
    "q": {
        "filename": "Q_original.png",
        "crop": (922, 32, 1202, 273),
        "mouth": [
            (313, 451),
            (326, 446),
            (350, 437),
            (365, 434),
            (372, 438),
            (379, 455),
            (380, 473),
            (373, 486),
            (358, 496),
            (339, 496),
            (324, 488),
            (316, 475),
        ],
        "lips": [(317, 454), (328, 453), (342, 448), (356, 442), (367, 438)],
        "eyes": [
            (
                [
                    (189, 412),
                    (202, 408),
                    (212, 390),
                    (229, 380),
                    (248, 375),
                    (266, 377),
                    (280, 387),
                    (292, 411),
                    (292, 439),
                    (276, 454),
                    (249, 460),
                    (223, 458),
                    (209, 447),
                    (195, 441),
                ],
                [
                    (230, 411),
                    (238, 395),
                    (252, 388),
                    (268, 390),
                    (280, 399),
                    (289, 415),
                    (288, 435),
                    (280, 448),
                    (263, 455),
                    (244, 448),
                    (233, 435),
                ],
                [(192, 436), (208, 442), (229, 444), (250, 440), (273, 430), (285, 415)],
            ),
            (
                [
                    (356, 367),
                    (363, 349),
                    (381, 334),
                    (402, 326),
                    (426, 328),
                    (447, 339),
                    (455, 350),
                    (469, 356),
                    (461, 369),
                    (459, 386),
                    (449, 408),
                    (428, 417),
                    (395, 421),
                    (374, 413),
                    (365, 394),
                ],
                [
                    (374, 358),
                    (384, 347),
                    (401, 343),
                    (416, 348),
                    (427, 359),
                    (434, 379),
                    (432, 397),
                    (418, 411),
                    (399, 416),
                    (385, 409),
                    (377, 397),
                ],
                [(359, 375), (375, 385), (396, 389), (415, 387), (438, 379), (455, 365), (464, 359)],
            ),
        ],
        "head": [
            (0, 86),
            (640, 86),
            (640, 414),
            (607, 405),
            (567, 391),
            (550, 403),
            (535, 435),
            (505, 467),
            (491, 502),
            (475, 528),
            (480, 557),
            (503, 587),
            (518, 615),
            (500, 628),
            (478, 619),
            (451, 584),
            (438, 548),
            (437, 506),
            (407, 508),
            (380, 523),
            (326, 525),
            (276, 520),
            (250, 510),
            (227, 499),
            (239, 528),
            (239, 557),
            (231, 588),
            (211, 615),
            (198, 628),
            (181, 616),
            (171, 592),
            (179, 557),
            (156, 503),
            (129, 476),
            (102, 480),
            (77, 493),
            (35, 490),
            (0, 476),
        ],
        "fin": [
            (1, 452),
            (30, 425),
            (66, 390),
            (82, 399),
            (93, 444),
            (113, 468),
            (81, 484),
            (52, 493),
            (24, 485),
            (1, 472),
        ],
        "bow": [
            (497, 247),
            (511, 244),
            (529, 252),
            (543, 251),
            (561, 239),
            (579, 235),
            (594, 240),
            (602, 256),
            (600, 286),
            (590, 293),
            (556, 281),
            (542, 303),
            (529, 313),
            (514, 300),
        ],
        "fringe": [
            (277, 158),
            (310, 165),
            (338, 204),
            (354, 246),
            (357, 278),
            (355, 319),
            (348, 344),
            (367, 372),
            (348, 386),
            (323, 389),
            (303, 385),
            (280, 367),
            (259, 331),
            (246, 278),
        ],
    },
}


def mask(points):
    image = Image.new("L", SIZE)
    ImageDraw.Draw(image).polygon(points, fill=255)
    return image.filter(ImageFilter.GaussianBlur(0.7))


def cut(image, area):
    layer = image.convert("RGBA")
    layer.putalpha(area)
    return layer


def restore(image, areas, radius=6):
    region = np.maximum.reduce([np.asarray(area) for area in areas])
    expanded = cv2.dilate(np.uint8(region > 12), np.ones((3, 3), np.uint8))
    return Image.fromarray(cv2.inpaint(np.asarray(image.convert("RGB")), expanded, radius, cv2.INPAINT_TELEA))


def restore_eyelids(skin, master, eye_masks):
    # Large anime irises border the fringe. Extend nearby skin colours into
    # the eyelid, without sampling blue hair or black lashes across that edge.
    rgb = np.asarray(master, dtype=np.float32)
    red, green, blue = rgb.transpose(2, 0, 1)
    sample = ((red > 185) & (red > green + 5) & (green > blue - 8)).astype(np.float32)
    for area in eye_masks:
        sample[np.asarray(area) > 0] = 0
    weights = cv2.GaussianBlur(sample, (0, 0), 18)
    colors = cv2.GaussianBlur(rgb * sample[:, :, None], (0, 0), 18)
    extension = Image.fromarray(np.uint8(np.clip(colors / np.maximum(weights[:, :, None], 1e-6), 0, 255)))
    for area in eye_masks:
        skin.paste(extension, mask=area.filter(ImageFilter.GaussianBlur(1.5)))


def line(points, color, width):
    # Supersample tapered strokes so a fully closed eyelid still has its lash.
    image = Image.new("RGBA", (SIZE[0] * 3, SIZE[1] * 3))
    draw = ImageDraw.Draw(image)
    for i, (a, b) in enumerate(zip(points, points[1:])):
        n = 24
        for step in range(n):
            t = step / n
            x, y = [3 * (p + (q - p) * t) for p, q in zip(a, b)]
            phase = (i + t) / (len(points) - 1)
            r = 1.5 * width * (0.16 + 0.84 * math.sin(math.pi * phase) ** 0.6)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
    return image.resize(SIZE, Image.Resampling.LANCZOS)


def transform(x, y):
    return {"trans": [x, y, 0], "rot": [0, 0, 0], "scale": [1, 1]}


class Puppet:
    def __init__(self):
        self.textures = []
        self.params = {}
        self.count = 0
        self.root = self.node("Root", None)

    def node(self, name, parent, xy=(0, 0), z=0):
        self.count += 1
        item = {
            "uuid": self.count,
            "name": name,
            "type": "Node",
            "enabled": True,
            "zsort": -z,
            "transform": transform(*xy),
            "lockToRoot": False,
            "children": [],
        }
        if parent is not None:
            parent["children"].append(item)
        return item

    def part(self, name, parent, layer, z, parent_center=(320, 360)):
        box = layer.getbbox()
        if not box:
            raise ValueError(f"empty layer {name}")
        cropped = layer.crop(box)
        w, h = cropped.size
        node = self.node(
            name, parent, (box[0] + w / 2 - parent_center[0], box[1] + h / 2 - parent_center[1]), z
        )
        node.update(
            type="Part",
            textures=[len(self.textures), 0xFFFFFFFF, 0xFFFFFFFF],
            blend_mode="Normal",
            opacity=1.0,
            mesh={
                "verts": [-w / 2, -h / 2, w / 2, -h / 2, -w / 2, h / 2, w / 2, h / 2],
                "uvs": [0, 0, 1, 0, 0, 1, 1, 1],
                "indices": [0, 1, 2, 2, 1, 3],
                "origin": [0, 0],
            },
        )
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        self.textures.append(buf.getvalue())
        return node

    def bind(self, param, node, field, values):
        signed = param in {"GazeX", "GazeY", "HeadX", "HeadY", "MouthForm"}
        axes = [0, 0.5, 1] if signed else [0, 1]
        if param == "MouthOpen":
            axes = [0, 0.22, 1]
        if len(values) != len(axes):
            raise ValueError(param)
        entry = self.params.setdefault(
            param,
            {
                "uuid": 1000 + len(self.params),
                "name": param,
                "is_vec2": False,
                "min": [-1 if signed else 0, 0],
                "max": [1, 1],
                "defaults": [0, 0],
                "axis_points": [axes, [0]],
                "bindings": [],
            },
        )
        entry["bindings"].append(
            {
                "node": node["uuid"],
                "param_name": field,
                "isSet": [[True] for _ in axes],
                "values": [[v] for v in values],
                "interpolate_mode": "Linear",
            }
        )

    def save(self, folder, name):
        folder.mkdir(parents=True, exist_ok=True)
        model = {
            "meta": {
                "name": name,
                "version": "1.0-alpha",
                "rigger": "Tuber Studio",
                "preservePixels": False,
                "artist": None,
                "copyright": None,
                "licenseURL": None,
                "contact": None,
                "reference": None,
            },
            "physics": {"pixelsPerMeter": 1000, "gravity": 9.81},
            "nodes": self.root,
            "param": list(self.params.values()),
            "automation": [],
        }
        raw = json.dumps(model, ensure_ascii=False, separators=(",", ":")).encode()
        with (folder / "model.inp").open("wb") as out:
            out.write(
                b"TRNSRTS\0"
                + struct.pack(">I", len(raw))
                + raw
                + b"TEX_SECT"
                + struct.pack(">I", len(self.textures))
            )
            for texture in self.textures:
                out.write(struct.pack(">IB", len(texture), 0) + texture)


def build(source, output, case):
    spec = CASES[case]
    with Image.open(source / spec["filename"]) as sheet:
        if sheet.size != (1448, 1086):
            raise ValueError("Expected the 1448x1086 master design sheet")
        crop = sheet.convert("RGBA").crop(spec["crop"])
        portrait = Image.new("RGB", crop.size, "#ece7df")
        portrait.paste(crop, mask=crop.getchannel("A"))
    master = ImageOps.pad(portrait, SIZE, method=Image.Resampling.LANCZOS, color="#ece7df")
    model = Puppet()
    body = model.node("Upper body", model.root)
    head = model.node("Head", body)
    regions = [mask(points) for points in [spec["mouth"], spec["fin"], spec["bow"], spec["fringe"]]]
    eye_masks = [mask(eye[0]) for eye in spec["eyes"]]
    skin = restore(master, regions + eye_masks)
    restore_eyelids(skin, master, eye_masks)
    head_mask = mask(spec["head"])
    # Extend the original pixels underneath the moving head by an edge band.
    edge = np.asarray(head_mask)
    band = edge - cv2.erode(edge, np.ones((15, 15), np.uint8))
    body_image = restore(master, [Image.fromarray(band)])
    hole = Image.fromarray(cv2.erode(edge, np.ones((15, 15), np.uint8)))
    body_image.paste("#ffffff", mask=hole)
    model.part("Back hair and shoulders", body, body_image.convert("RGBA"), 0)
    model.part("Face and front hair", head, cut(skin, head_mask), 1)
    for name, region, z, delta in zip(
        ("Fin ear", "Ribbon", "Forelock"), regions[1:], (3, 4, 8), (1.6, 1.1, 0.7)
    ):
        feature = model.part(name, head, cut(master, region), z)
        model.bind("HeadX", feature, "transform.t.x", [-delta, 0, delta])
    mouth = model.part("Mouth interior and lip", head, cut(master, regions[0]), 7)
    model.bind("MouthOpen", mouth, "transform.s.y", [0.06, 0.23, 1])
    model.bind("MouthOpen", mouth, "transform.t.y", [-8, -6, 0])
    model.bind("MouthOpen", mouth, "opacity", [0, 1, 1])
    model.bind("MouthForm", mouth, "transform.s.x", [0.74, 1, 1.15])
    lips = model.part("Closed lip seam", head, line(spec["lips"], (163, 99, 94, 255), 2.4), 8)
    model.bind("MouthOpen", lips, "opacity", [1, 0, 0])
    for side, (outline, iris_points, lid_points), area in zip(("L", "R"), spec["eyes"], eye_masks):
        eye_center = tuple(sum(x) / len(x) for x in zip(*outline))
        eye = model.node("Eye " + side, head, (eye_center[0] - 320, eye_center[1] - 360))
        iris_mask = mask(iris_points)
        sclera = master.copy()
        sclera.paste("#f4edf4", mask=iris_mask)
        model.part("Socket and lashes " + side, eye, cut(sclera, area), 5, eye_center)
        iris = model.part("Iris " + side, eye, cut(master, iris_mask), 6, eye_center)
        model.bind("GazeX", iris, "transform.t.x", [-2.2, 0, 2.2])
        model.bind("GazeY", iris, "transform.t.y", [-1.6, 0, 1.6])
        model.bind("Blink" + side, eye, "transform.s.y", [1, 0.04])
        for part in eye["children"]:
            model.bind("Blink" + side, part, "opacity", [1, 0])
        lash = model.part("Closed lid " + side, head, line(lid_points, (53, 29, 43, 255), 4), 7)
        model.bind("Blink" + side, lash, "opacity", [0, 1])
    model.bind("HeadX", head, "transform.t.x", [-2.8, 0, 2.8])
    model.bind("HeadX", head, "transform.r.z", [-0.009, 0, 0.009])
    model.bind("HeadX", head, "transform.s.x", [0.995, 1, 0.995])
    model.bind("HeadY", head, "transform.t.y", [-2.2, 0, 2.2])
    model.bind("Breath", body, "transform.s.y", [1, 1.002])
    model.save(output / case, "大肥鱼 · " + ("常规" if case == "regular" else "Q"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    for variant in CASES:
        build(args.source, args.output, variant)
