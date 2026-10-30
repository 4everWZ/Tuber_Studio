# Tuber Studio

[English](README.md) · [简体中文](README.zh-CN.md)

A self-hosted AI character workspace for Bilibili comments and local voice interaction. Create original characters, draft replies in their voice, and bring conversations to a browser overlay.

## Features

- **Comment workspace:** browse videos or import comments, organize conversation threads, classify feedback, and generate replies. Drafts use manual review by default, with editing, copying, account-based publishing, and optional automatic browsing and replies.
- **Character profiles:** clone and customize personality, speaking style, rules, artwork, and voice bindings, with version history.
- **Experimental Bilibili posts:** let the character decide when to post from public memories, with publishing limits, source tracking, optional review, and a stop control.
- **Local models and APIs:** MiniCPM5-2B with a configurable 128K default context, Genie speech, prerecorded audio, and OpenAI-compatible LLM and TTS providers.
- **Context and memory:** thread-scoped conversations, source-grounded recall, automatic compaction, and configurable data retention.
- **Voice interaction:** text/live resource modes, short voice previews, Mock events, and a browser overlay for audio, subtitles, and character expressions.

Bilibili and Douyin live-stream adapters are on the roadmap.

## Jetson quick start

Requires an ARM64 Jetson running **L4T R36.4**, Docker with the NVIDIA container runtime, Bash, and curl. Run as a regular user with Docker access. Internet access is needed for GHCR/CDN image downloads and the initial model download.

```bash
curl -fsSL https://raw.githubusercontent.com/4everWZ/Tuber_Studio/main/tools/jetson/run-ready.sh -o run-ready.sh
bash run-ready.sh --data ./tuber-data
```

The launcher pulls `ghcr.io/4everwz/tuber_studio:r36.4`, downloads the models, and prepares the default voice on first launch. Models, settings, credentials, and the database stay in `tuber-data` and are reused on subsequent starts.

Open [http://127.0.0.1:9020](http://127.0.0.1:9020) and sign in with the administrator token in `tuber-data/admin-token`. Keep that file private. For access from another computer, see the [deployment guide](docs/guides/deployment.md).

Start in the comment workspace: fetch or import comments, select a thread, and generate a draft for review. Automatic browsing and replies are off by default. For voice interaction, use live resource mode, open the overlay, start a Mock session, and inject a chat event.

## Local development

Use Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --python 3.12
uv run python -m tuber_studio
```

Open [http://127.0.0.1:8020](http://127.0.0.1:8020); the administrator token is in `runtime/admin-token`. Configure model and speech endpoints in **Settings**. The source configuration starts in text mode.

## Documentation

- [Deployment, updates, and network configuration](docs/guides/deployment.md)
- [Create an original character](docs/guides/characters.md)
- [Comments and autonomous posts](docs/guides/operations.md)
- [Documentation index](docs/README.md) · [Context and compaction](docs/specs/context-and-compaction.md)
- [CI and ARM64 image builds](https://github.com/4everWZ/Tuber_Studio/actions/workflows/ci.yml)

## Acknowledgements

Built with MiniCPM, llama.cpp, GPT-SoVITS, and Genie-TTS. Third-party terms are included in the [Genie-TTS license](third_party/Genie-TTS/LICENSE), [Codex license](third_party/openai-codex/LICENSE), and [Codex notices](third_party/openai-codex/NOTICE).

Q-style artwork by [YunYueSama](https://github.com/YunYueSama/codex-deepseek-pet), adapted into six GIF states under the [upstream asset terms](third_party/whale-avatar/UPSTREAM.md). Artwork and voice sources are recorded in the [asset manifest](assets/whale/manifest.json) and [voice profiles](config/voices.json).
