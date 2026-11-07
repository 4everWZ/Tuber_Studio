# Tuber Studio

English · [简体中文](README.zh-CN.md)

Tuber Studio is a self-hosted AI character workbench for Bilibili community operations and live production. It brings source material, character responses, draft review and platform receipts into one workspace. The included character is Da-Fei-Yu, a Q-style whale girl. You can create a character with your own voice, artwork and settings.

![Da-Fei-Yu talking](assets/whale/talking.gif)

## What you can do

- **Work through Bilibili comments.** Read or import comments, organize them by thread and generate replies in character. Review and edit drafts before authorizing a submission, then check the platform receipt.
- **Run a community workspace.** Use autonomous browsing to find videos and collect subtitles or admitted visual evidence. Direct messages and dynamic posts have their own policies and review queues.
- **Choose how the character is generated.** The project's fine-tuned Da-Fei-Yu model uses a short identity prompt. Generic local models and compatible APIs use the selected character card. Task material and conversation history remain separate from the character prompt.
- **Prepare a live show.** Arrange scenes, use the OBS browser source, synthesize speech and play song requests with lyrics and lip sync. The local mock flow lets you exercise the workspace before connecting a real stream.

## Quick start

Install [uv](https://docs.astral.sh/uv/) and use Python 3.12. Run these commands from the repository root.

```sh
uv sync --python 3.12
uv run python -m tuber_studio --port 8020
```

Open [the local workbench](http://127.0.0.1:8020). Direct local access opens without a management token; remote access follows the authentication setup in the [deployment guide](docs/guides/deployment.md). Use **Settings → Inference and models** to pair a Jetson or configure another endpoint. **Autonomous operations** contains activity, comment review, private messages, dynamic posts and operations settings. Live production and the character studio remain separate workspaces. The top mode switch controls inference resources.

Under **Character → Voice**, local Jetson speech is the default. An OpenAI-compatible speech endpoint can also supply previews and spoken livestream replies. Demo text and prerecorded audio are available for an offline tour.

For Bilibili operations, configure and verify the account under **Settings → Account and permissions**, then save the actions you want under operations settings. Starting autonomous operations uses those permissions. Video discovery, comment replies, private messages and dynamic posts share that one control. The activity view shows daily outcomes across channels. Reading or importing comments, selecting sources, generating and reviewing replies stay in the same comment workbench. See the [operations guide](docs/guides/operations.md) for review, recovery and emergency stop.

## Deployment

The default product setup keeps business data on a PC and uses a Jetson on the local network for inference. The PC owns the database, platform credentials, task queues, song requests and OBS playback. The Jetson owns model weights and inference caches. Pairing uses the node address and its own credential. Follow the [PC and Jetson guide](docs/guides/deployment.md#电脑业务主机与独立推理节点).

A standalone Jetson installation is also available. It runs the program bundle or a source checkout with a separate reusable environment image and a persistent data directory. See [standalone Jetson setup](docs/guides/deployment.md#全本地jetson可选部署).

## Project status

| Area | Status |
| --- | --- |
| Community operations | Current delivery priority. Draft workflows are available. Real-account permissions, model decisions and authorized writes have separate acceptance criteria. |
| Live production | Local workspace, mock events, speech and playback are available. OBS acceptance, real platform adapters and sustained mixed load remain tracked. |
| Games | Stardew Valley and Plants vs. Zombies have offline simulations. Real-game integration follows the live production phase. |

The [shared roadmap](docs/plans/current.md) records the implementation gaps, device evidence and acceptance criteria.

## Development

The FastAPI backend and browser workspace live under `src/tuber_studio/`. Deployment entry points are in `tools/deployment/` and `tools/jetson/`. Optional online song requests are in `components/song_requests/`. Use the locked project environment for tests.

```sh
uv sync --locked --group dev --python 3.12
uv run --locked python tools/ci/check_style.py
uv run --locked pytest tests/test_comments.py
```

For a change, run the tests that cover the affected flow. See the [documentation index](docs/README.md) for guides, specifications and the game implementation plan.

## Acknowledgements

Tuber Studio uses MiniCPM, llama.cpp, GPT-SoVITS and Genie-TTS. See the [Genie-TTS license](third_party/Genie-TTS/LICENSE) and [Codex notices](third_party/openai-codex/NOTICE).

The included Da-Fei-Yu character uses artwork by YunYueSama. Sources and terms are recorded in the [artwork notes](third_party/whale-avatar/UPSTREAM.md), [asset manifest](assets/whale/manifest.json) and [voice profiles](config/voices.json).
