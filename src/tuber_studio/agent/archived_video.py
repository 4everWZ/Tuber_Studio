"""Read an operator-mounted archive of public source material inside a private Studio."""

import asyncio
import json
import math
import re
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

from ..infra.inference.video_vision import VideoPartText, frame_reading_context
from ..infra.platforms.bilibili.client import PlatformReadError
from ..infra.platforms.bilibili.frames import MAX_FRAME_BYTES, FfmpegTiles
from ..infra.platforms.bilibili.stream_frame import _timestamps


class ArchiveUnavailable(ValueError):
    pass


class MountedArchive:
    """Only private source tools open the configured mount. A repaired mount can be retried."""

    def __init__(self, manifest, url):
        self.manifest = manifest
        self.url = url
        target = re.fullmatch(
            r"https://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})/\?p=([1-9]\d*)", url or ""
        )
        self.bvid, self.part = (target[1], int(target[2])) if target else (None, None)
        self.loaded = None

    def matches(self, grant):
        return self.part in ((grant or {}).get(self.bvid) or {})

    def open(self):
        if self.loaded is None:
            try:
                package = ArchivedVideo(self.manifest)
                if package.source["url"] != self.url:
                    raise ValueError("归档与获准的分 P 不一致")
                self.loaded = package
            except (OSError, ValueError, LookupError, TypeError) as error:
                raise ArchiveUnavailable(
                    "已授权的归档来源包不可读取。请核对只读挂载、清单及其中的原件后继续。"
                ) from error
        return self.loaded

    def optional(self):
        try:
            return self.open()
        except ArchiveUnavailable:
            return None

    async def saved_frame_context(self, step, point, grant):
        clip = (step.get("result") or {}).get("clip") or {}
        if ((point.bvid, point.part) != (self.bvid, self.part) or not self.matches(grant)
                or clip.get("source_kind") != "archived_video"):
            return {}
        if (point.bvid, point.part, point.cid) != (clip["bvid"], clip["part"], str(clip["cid"])):
            return {}
        bounds = (clip["start"], clip["end"])
        for a, b in grant.get(point.bvid, {}).get(point.part) or [bounds]:
            if a <= point.seconds < b and (package := self.optional()):
                return await package.frame_context(point, clip.get("archive_url"), (max(a, bounds[0]), min(b, bounds[1])))
        return {}


class ArchivedVideo:
    def __init__(self, manifest):
        path = Path(manifest).resolve(strict=True)
        package = json.loads(path.read_text(encoding="utf-8"))
        self.source = package["source"]
        for key in ("bvid", "cid", "part_title", "upload_title", "uploader", "url"):
            if not isinstance(self.source[key], str) or not self.source[key]:
                raise ValueError("归档元数据缺少来源标识")
        if (
            self.source["url"]
            != f"https://www.bilibili.com/video/{self.source['bvid']}/?p={self.source['part']}"
        ):
            raise ValueError("归档分 P 与原始网址不一致")
        self.pages = {row["url"]: row for row in package["archived_source_pages"]}
        for url, row in self.pages.items():
            field = "archived_public_metadata" if url == self.source["url"] else "archived_programme_credits"
            if not all(isinstance(row[key], str) and row[key] for key in ("title", field)):
                raise ValueError("归档来源页缺少可核实的正文")
        self.clips = []
        self.subtitle_segments = self.source.get("subtitle_segments", [])
        if not isinstance(self.source.get("description", ""), str) or not isinstance(self.subtitle_segments, list):
            raise ValueError("归档简介或字幕格式无效")
        for row in self.subtitle_segments:
            if (not isinstance(row, dict) or not isinstance(row.get("text"), str) or not row["text"].strip()
                    or not all(type(row.get(k)) in (int, float) for k in ("start", "end"))
                    or not 0 <= row["start"] < row["end"] <= self.source["part_duration_seconds"]):
                raise ValueError("归档字幕必须有本分 P 的起止时间及原文")
        for row in package["clips"]:
            file = (path.parent / row["file"]).resolve(strict=True)
            start, end = row["source_start_seconds"], row["source_end_seconds_exclusive"]
            if (
                not file.is_relative_to(path.parent)
                or not file.is_file()
                or file.suffix.lower() != ".mp4"
                or not 0 <= start < end <= self.source["part_duration_seconds"]
                or row["source_url"] != f"{self.source['url']}&t={int(start)}"
                or type(row["frames"]) is not int
                or row["frames"] < 2
                or not 0 < row["fps"]
                or not math.isfinite(row["fps"])
                or not 0 <= row["file_first_video_pts_seconds"] <= row["file_last_video_pts_seconds"]
            ):
                raise ValueError("归档片段路径、出处或时窗无效")
            self.clips.append((start, end, file, row))
        self.windows = [[a, b] for a, b, _, _ in self.clips]

    def page(self, url):
        row = self.pages.get(url)
        if not row:
            return {"error": "归档来源包未收录此网页。", "source_kind": "archived_page", "url": url}
        credits = row.get("archived_programme_credits")
        kind = "bilibili_public_metadata" if url == self.source["url"] else "archived_page"
        return {
            "url": url,
            "title": row["title"],
            "source_kind": kind,
            "retrieval": "archive",
            "message": "已读取归档来源页。",
            "excerpt": credits or row["archived_public_metadata"],
            "truncated": False,
            **({"programme_credits": credits} if credits else {}),
        }

    def search(self, query, catalog=None):
        # A search for the package's exact video ID also finds its associated source pages.
        if self.source["bvid"] in query:
            links = list(self.pages)
        else:
            links = [
                url
                for url, row in self.pages.items()
                if row["title"].split("_")[0] in query or query.strip() and query.strip() in row["title"]
            ]
        if catalog == "央视节目库":
            links = [url for url in links if urlsplit(url).hostname == "tv.cctv.com"]
        return {
            "links": links,
            "source_kind": "archived_page_index",
            "catalog": "归档来源目录",
            "message": f"在归档来源目录找到 {len(links)} 个候选出处。",
        }

    async def video_metadata(self, bvid):
        s = self.source
        if bvid != s["bvid"]:
            raise ArchiveUnavailable("视频不在当前归档授权内")
        return {
            "bvid": s["bvid"],
            "title": s["upload_title"],
            "description": s.get("description", ""),
            "description_complete": "description" in s,
            "owner": s["uploader"],
            "retrieval": "archive",
            "source_url": s["url"],
            "archived_programme_sources": [
                {"url": row["url"], "credits": row["archived_programme_credits"]}
                for row in self.pages.values() if row.get("archived_programme_credits") and row["url"] != s["url"]
            ],
            "parts": [
                {
                    "page": s["part"],
                    "cid": s["cid"],
                    "duration": s["part_duration_seconds"],
                    "title": s["part_title"],
                }
            ],
        }

    async def subtitles(self, bvid, cid):
        if bvid != self.source["bvid"] or str(cid) != self.source["cid"]:
            raise ArchiveUnavailable("字幕不属于当前归档分 P")
        return {"bvid": bvid, "cid": self.source["cid"], "segments": self.subtitle_segments,
                "source_url": self.source["url"], "retrieval": "archive"}

    async def frame_context(self, point, archive_url, bounds):
        """Resolve a saved frame against its own mounted source and current read window."""
        s = self.source
        if (point.bvid, point.part, point.cid) != (s["bvid"], s["part"], s["cid"]):
            return {}
        for start, end, _, row in self.clips:
            if row["source_url"] == archive_url and start <= point.seconds < end:
                break
        else:
            return {}
        lo, hi = max(start, bounds[0]), min(end, bounds[1])
        if not lo <= point.seconds < hi:
            return {}
        track = await self.subtitles(point.bvid, point.cid)
        track["segments"] = [r for r in track["segments"] if lo <= r["start"] <= point.seconds < r["end"] <= hi]
        metadata = await self.video_metadata(point.bvid)
        part = VideoPartText(metadata, point.cid, [track])
        return frame_reading_context({point.cid: part}, [{"cid": point.cid, "actual_time": point.seconds}])

    async def close(self):
        pass

    def ranges(self, start, end):
        return [(max(start, a), min(end, b)) for a, b in self.windows if max(start, a) < min(end, b)]

    async def _media(self, cid, start, end=None):
        for left, right, file, row in self.clips:
            if str(cid) == self.source["cid"] and left <= start < right and (end is None or end <= right):
                break
        else:
            raise PlatformReadError("archive_window", "请求时窗未落在一支已归档原件内。")
        decoder = FfmpegTiles()
        try:
            media = await asyncio.to_thread(file.read_bytes)
        except OSError as error:
            raise ArchiveUnavailable("归档清单中的原件已不可读，请检查只读挂载后继续。") from error
        frames, tick = await _timestamps(decoder, media)
        first = frames[0][0]
        # Fragmented MP4 may omit per-frame duration. Verify the declared cadence
        # against every PTS and the exclusive end in the manifest.
        period = 1 / (Fraction(str(row["fps"])) * tick)
        frames = [(pts, duration or period) for pts, duration in frames]
        # Verify the mapping against decoded presentation timestamps, including the last frame.
        if (
            len(frames) != row["frames"]
            or first * tick != Fraction(str(row["file_first_video_pts_seconds"]))
            or frames[-1][0] * tick != Fraction(str(row["file_last_video_pts_seconds"]))
            or (sum(frames[-1]) - first) * tick != Fraction(str(right)) - Fraction(str(left))
            or any(d <= 0 or p + d != q for (p, d), (q, _) in zip(frames, frames[1:]))
        ):
            raise PlatformReadError("archive_window", "归档原件的实际呈现时标与来源清单不符。")
        mapped = [
            (Fraction(str(left)) + (p - first) * tick, Fraction(str(left)) + (p + d - first) * tick)
            for p, d in frames
        ]
        command = [
            decoder.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            "-protocol_whitelist",
            "pipe",
            "-f",
            "mov",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
        ]
        return decoder, command, media, mapped, row

    async def video_stream_frame(self, metadata, *, cid, timestamp):
        decoder, command, media, times, row = await self._media(cid, timestamp)
        target = Fraction(str(timestamp))
        index = next(i for i, (a, b) in enumerate(times) if a <= target < b)
        image = await decoder._run(
            command
            + [
                "-vf",
                f"select=eq(n\\,{index})",
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-c:v",
                "mjpeg",
                "-f",
                "image2pipe",
                "pipe:1",
            ],
            media,
            MAX_FRAME_BYTES,
        )
        width, height = await decoder.dimensions(image)
        a, b = times[index]
        return {
            "id": f"{cid}:archive:{int(a * 1000000)}",
            "cid": str(cid),
            "actual_time": float(a),
            "presentation_end": float(b),
            "image": image,
            "width": width,
            "height": height,
            "archive_url": row["source_url"],
            "kind": "archive",
        }

    async def video_stream_clip(self, metadata, *, cid, start, end):
        decoder, command, media, times, row = await self._media(cid, start, end)
        kept = [i for i, (a, b) in enumerate(times) if Fraction(str(start)) <= a and b <= Fraction(str(end))]
        if len(kept) < 2 or end - start > 10:
            raise PlatformReadError("archive_window", "归档短片需包含两个以上连续原帧且限十秒以内。")
        first, last = kept[0], kept[-1]
        if len(kept) == len(times):
            movie = media
        else:
            movie = await decoder._run(
                command
                + [
                    "-vf",
                    f"trim=start_frame={first}:end_frame={last + 1},setpts=PTS-STARTPTS",
                    "-fps_mode",
                    "passthrough",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-crf",
                    "16",
                    "-movflags",
                    "+empty_moov+frag_keyframe+default_base_moof",
                    "-f",
                    "mp4",
                    "pipe:1",
                ],
                media,
                8 * 1024 * 1024,
            )
        return {
            "cid": str(cid),
            "start": float(times[first][0]),
            "end": float(times[last][1]),
            "frames": len(kept),
            "audio": "visual_only",
            "movie": movie,
            "source_kind": "archived_video",
            "archive_url": row["source_url"],
        }
