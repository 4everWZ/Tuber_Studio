# Tuber Studio

English · [简体中文](README.zh-CN.md)

Tuber Studio is a self-hosted creative partner for a channel owner. Ask Da-Fei-Yu to research material, keep chatting while she works, then open the private result and tell her which part to change. Studio keeps the goals, sources, editions and platform receipts. A separate workstation provides Omni inference and Avatar through an API. You can also use your own character, artwork and voice.

![Da-Fei-Yu chat pet](assets/whale/talking.gif)

> “Help me prep for my next stream. Watch part two of this video and pick two clips I could open with. Include timestamps and sources, and keep the brief private.”

This is a typical Studio assignment. Da-Fei-Yu can research in the background while you keep chatting. Come back on another device, open the private brief and play the excerpts to decide what fits your audience. Comments, direct messages and feed posts each have their own publishing permissions.

## What you can do

- **Prepare a private brief.** Research a particular video part and its public sources. Open the result from chat, play saved excerpts, review older editions or continue from another device. [Operator guide](docs/guides/operations.md)
- **Set a working preference.** See which owner statement supports it, change it or withdraw it. Text chat, private calls and background editing use preferences relevant to their work. [Memory design](docs/specs/foundation/runtime-and-characters.md)
- **Work through Bilibili comments.** Read or import comments, organize them by thread and generate replies in character. Review and edit drafts before authorizing a submission, then check the platform receipt.
- **Run community operations.** Start video browsing, comments, direct messages and posts from one workspace. Watching uses available captions and sampled frames. The app checks comment invitations against platform user IDs before preparing a direct message. Review and send permissions are configured per channel.
- **Edit the active character.** Operations, operator chat and live production use the selected character card. Create, edit or activate cards in the character workspace. Saving updates the selected card in place. Direct messages and posts share the sticker library.
- **Prepare a live show.** Arrange scenes, use the OBS browser source, synthesize speech and play song requests with lyrics and lip sync. The local mock flow lets you exercise the workspace before connecting a real stream.

## Quick start

Install [uv](https://docs.astral.sh/uv/) and use Python 3.12. Run these commands from the repository root.

```sh
uv sync --python 3.12
uv run python -m tuber_studio --port 8020
```

Open the [local workbench](http://127.0.0.1:8020). Direct local access opens without a management token. For remote access and gateway setup, follow the [deployment guide](docs/guides/deployment.md).

The character card selects the reading voice. New installations use the Da-Fei-Yu reference. A fixed preview plays its recorded line. To hear a new line, enter it in the workbench and play the generated audio.

For Bilibili operations, configure and verify the account under **Settings → Account and permissions**, then save the actions you want under operations settings. Starting autonomous operations uses those permissions. Video discovery, comment replies, private messages and dynamic posts share that one control. The activity view shows daily outcomes across channels. Reading or importing comments, selecting sources, generating and reviewing replies stay in the same comment workbench. See the [operations guide](docs/guides/operations.md) for review, recovery and emergency stop.

## Deployment

Core runs the workbench and Agent and holds platform credentials, business data, tasks and memory. Configure one external Omni gateway and its application credential for chat, background tasks, vision, reference speech and image jobs. Private calls use short-lived browser tickets. [API contract](docs/specs/foundation/omni-deployment.md)

## Project status

| Area | Status |
| --- | --- |
| Community operations | The workstation review covered all three image-enabled drafting channels. Delivery, real incoming private messages and owner replies require separate platform checks. |
| Live production | Images and GIFs are the default. OBS tests covered optional Live2D lip sync, mute and GIF fallback. Live platform integration, first-audio latency and sustained mixed load remain open. |
| Games | Stardew Valley and Plants vs. Zombies have offline simulations. Real-game integration follows the live production phase. |

The [documentation index](docs/README.md) links to the test results and remaining acceptance work.

## Development

The FastAPI backend and browser workspace live under `src/tuber_studio/`. Optional online song requests are in `components/song_requests/`. Use the locked project environment for tests.

```sh
uv sync --locked --group dev --python 3.12
uv run --locked python tools/ci/check_style.py
uv run --locked pytest tests/test_comments.py
```

Run the tests for the affected flow. The [documentation index](docs/README.md) covers the guides, specifications, implementation plans and project archives.

`uv run --locked pytest tests/test_account_ui.py` runs the workbench browser-module tests in Node when available. Use the project test directory to keep test backups and packaged releases out of discovery.

## Acknowledgements

Tuber Studio's original code and documentation are licensed under the [MIT License](LICENSE). Bundled third-party code, character artwork, and voice recordings retain the terms listed with their sources.

See [Codex notices](third_party/openai-codex/NOTICE) for bundled third-party materials.

The included Da-Fei-Yu character uses artwork by YunYueSama. Sources and terms are recorded in the [artwork notes](third_party/whale-avatar/UPSTREAM.md), [asset manifest](assets/whale/manifest.json) and [voice profiles](config/voices.json).
