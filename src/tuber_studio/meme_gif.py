"""Decode one bounded GIF on a seekable buffer in the inspector's timed subprocess."""

from io import BytesIO
import json
import struct
import sys

from PIL import Image


def palette_size(flags):
    return 3 * (2 << (flags & 7)) if flags & 128 else 0


def frame_count(data, max_frames, max_pixels):
    if not data.endswith(b";"):
        raise ValueError("GIF 缺少文件结束标记，未登记")
    invalid = "GIF 数据块不完整或无效，未登记"
    if len(data) < 14:
        raise ValueError(invalid)
    width, height = struct.unpack_from("<HH", data, 6)
    if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= max_pixels):
        raise ValueError("图片尺寸超过限额")
    pos = 13 + palette_size(data[10])
    count = 0
    while pos < len(data):
        marker = data[pos]
        if marker == 0x3B and pos == len(data) - 1 and count:
            return count
        if marker == 0x2C and pos + 10 < len(data):  # image descriptor
            x, y, w, h = struct.unpack_from("<4H", data, pos + 1)
            if not w or not h or x + w > width or y + h > height:
                raise ValueError("GIF 图像帧超出画布，未登记")
            pos += 10 + palette_size(data[pos + 9]) + 1  # descriptor, local table and LZW code size
            count += 1
            if count > max_frames:
                raise ValueError("图片帧数超过限额")
        elif marker == 0x21:  # extension introducer and label
            pos += 2
        else:
            raise ValueError(invalid)
        while pos < len(data):
            size = data[pos]
            pos += 1 + size
            if not size:
                break
    raise ValueError(invalid)


def inspect(data, max_frames, max_pixels):
    frames = frame_count(data, max_frames, max_pixels)
    try:
        with Image.open(BytesIO(data), formats=["GIF"]) as image:
            for index in range(frames):
                image.seek(index)
                image.load()
            return {"width": image.width, "height": image.height, "frames": frames}
    except (OSError, EOFError, ValueError, SyntaxError):
        raise ValueError("GIF 图片不能完整解码，未登记") from None


if __name__ == "__main__":
    max_bytes, max_frames, max_pixels = map(int, sys.argv[1:])
    data = sys.stdin.buffer.read(max_bytes + 1)
    try:
        if len(data) > max_bytes:
            raise ValueError("表情文件超过8MiB")
        result = inspect(data, max_frames, max_pixels)
    except ValueError as error:
        result = {"error": str(error)}
    print(json.dumps(result))
