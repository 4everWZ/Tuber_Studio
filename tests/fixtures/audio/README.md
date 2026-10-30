The FLAC and 320 kbps MP3 fixtures encode a generated 440 Hz sine wave (3 seconds,
44.1 kHz stereo). They contain no recording or lyrics from an external work.
Generated with FFmpeg 7.1 (`sine=frequency=440:duration=3`, `flac` / `libmp3lame -b:a 320k`).
They exercise actual format detection, unchanged serving, browser decoding and cache integrity.
