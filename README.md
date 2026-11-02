# Tuber Studio

<p align="center">
  <a href="https://github.com/4everWZ/Tuber_Studio"><img src="https://img.shields.io/badge/version-v0.1.0-blue.svg?style=flat-square" alt="Version"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.12+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="https://developer.nvidia.com/embedded/jetson-modules"><img src="https://img.shields.io/badge/NVIDIA%20Jetson-L4T%20R36.4-76B900.svg?style=flat-square&logo=nvidia&logoColor=white" alt="Jetson R36.4"></a>
  <a href="https://fastapi.tiangolo.com"><img src="https://img.shields.io/badge/FastAPI-Framework-009688.svg?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://github.com/4everWZ/Tuber_Studio/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Open%20Source-green.svg?style=flat-square" alt="License"></a>
</p>

<p align="center">
  <b>English</b> · <a href="README.zh-CN.md">简体中文</a>
</p>

---

<p align="center">
  <img src="assets/whale/talking.gif" width="120" alt="Da-Fei-Yu Talking" />
  &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <img src="assets/whale/cheerful.gif" width="120" alt="Da-Fei-Yu Cheerful" />
  &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <img src="assets/whale/sing_open.png" width="120" alt="Da-Fei-Yu Singing" />
</p>
<p align="center">
  <sub>Built-in character: Da-Fei-Yu (Chibi whale-girl, adapted from YunYueSama's open-source artwork)</sub>
</p>

Tuber Studio is a self-hosted AI character operations workbench for community engagement (e.g. Bilibili comments) and local virtual stage performances. It supports multimodal video and comment understanding, custom persona and voice cloning, experimental autonomous social posting, and an OBS browser overlay. Designed to run on consumer PCs or NVIDIA Jetson Orin edge devices.

---

## Features

- **Multimodal Comments & DM Workspace:** Video frame understanding and subtitle parsing; parses threaded comment discussions and analyzes sentiment. Drafts use human review by default, with editing, account-based publishing, and optional automatic browsing and replies. Supports routing sensitive topics to private direct messages while tracking user interaction history.
- **Character Profiles & Voice Cloning:** Customize persona rules, speaking style, and sticker galleries. Includes the built-in "Da-Fei-Yu" preset with versioning, and supports GPT-SoVITS and Genie-TTS reference audio bindings.
- **Autonomous Posts (Experimental):** Allows the character to decide when and what to post based on accumulated public memories. Generated content maintains provenance tracking to isolate fictional backstories from objective facts, accompanied by rate limiting and emergency stop controls.
- **Virtual Stage & Audio Lip-Sync:** Transparent browser overlay for OBS with draggable component staging. Drives mouth open/close animations using amplitude and formant analysis from an isolated vocal track; includes local music playback, request queues, and timed lyrics.
- **Edge Deployment & Dual-Node Setup:** Pre-configured for NVIDIA Jetson Orin (ARM64 L4T R36.4) via Docker; also supports running the Web UI and OBS on a primary PC while offloading model inference to a secondary computer over the local network.
- **Context & Storage:** Configurable 128K context window with domain-specific semantic compaction to preserve long-term interaction history. Data is stored locally in SQLite for full privacy.

---

## System Architecture

```mermaid
flowchart TB
    %% External Inputs
    subgraph INGRESS["External Ingress & Events"]
        direction TB
        IN_BILI["Bilibili Videos & Comment Threads"]
        IN_LIVE["Live Danmaku & Song Requests"]
        IN_MEM["Public Memories & Fan Affinity"]
    end

    %% Agent Orchestration Core
    subgraph AGENT["Agent Orchestration Hub"]
        direction TB
        VLM["Multimodal Video & Subtitle Engine<br/><sub>(MiniCPM-V / Gemma / Keyframes)</sub>"]
        PERSONA["Persona Alignment & Emotion FSM<br/><sub>(6-State Reactive FSM & Stickers)</sub>"]
        ROUTER["Channel Router & Circuit Breaker<br/><sub>(Draft Review / Rate Limit Safety)</sub>"]
        MOMENT["Autonomous Post Decision Engine<br/><sub>(Provenance & Memory Grounding)</sub>"]
    end

    %% Presentation & Egress
    subgraph OUTPUT["Presentation & Live Stage"]
        direction TB
        OUT_POST["Bilibili Moments / Thread Replies / DMs"]
        subgraph OBS["OBS Live Virtual Stage"]
            direction TB
            AUDIO["Isolated Vocal Track Analysis<br/><sub>(Formant & Amplitude Extraction)</sub>"]
            STAGE["Transparent Browser Overlay<br/><sub>(Reactive Lip-Sync & Scrolling Lyrics)</sub>"]
        end
    end

    %% Bottom: Infrastructure, Memory & Runtime
    subgraph INFRA["Infrastructure, Memory Engine & Edge Runtime"]
        direction LR
        subgraph MEM["Memory & Context"]
            direction TB
            CTX["128K Native Context Window"]
            COMPACT["Domain Semantic Compaction"]
            DB[("Local SQLite Database")]
        end
        subgraph RUNTIME["Local Inference Stack"]
            direction TB
            LLM["Edge Model Inference<br/><sub>llama.cpp / MiniCPM5-2B</sub>"]
            TTS["Low-Latency Neural Speech<br/><sub>Genie-TTS / GPT-SoVITS</sub>"]
        end
        subgraph HARDWARE["Edge & Distributed Hardware"]
            direction TB
            JETSON["NVIDIA Jetson Orin<br/><sub>(ARM64 L4T R36.4)</sub>"]
            DUAL["Host PC + Dedicated Inference Node<br/><sub>(Dual-Node LAN Mode)</sub>"]
        end
    end

    %% Data Flow Connections
    IN_BILI -->|Frames & Comments| VLM
    IN_MEM -->|Memory Recall| PERSONA
    IN_LIVE -->|Danmaku & Requests| TTS

    VLM -->|Visual Grounding| PERSONA
    PERSONA -->|Draft Reply| ROUTER
    PERSONA -->|Experience Logging| MOMENT
    ROUTER -->|Approved Dispatch| OUT_POST
    MOMENT -->|Scheduled Post| OUT_POST

    TTS -->|Vocal Audio Track| AUDIO
    AUDIO -->|Drive Mouth Open/Close| STAGE

    PERSONA <-->|Thread Context| CTX
    CTX --> COMPACT --> DB
    AGENT <-->|Model RPC| LLM
    HARDWARE -.->|Compute Support| RUNTIME
    HARDWARE -.->|Local Hosting| MEM

    %% Styling
    classDef ingress fill:#0F172A,stroke:#38BDF8,stroke-width:1.5px,color:#F8FAFC;
    classDef agent fill:#1E1B4B,stroke:#818CF8,stroke-width:1.5px,color:#EEF2FF;
    classDef output fill:#4A044E,stroke:#F472B6,stroke-width:1.5px,color:#FAF5FF;
    classDef obs fill:#1E293B,stroke:#FB7185,stroke-width:1.5px,color:#FFF1F2;
    classDef infra fill:#022C22,stroke:#34D399,stroke-width:1.5px,color:#ECFDF5;
    classDef mem fill:#064E3B,stroke:#10B981,stroke-width:1px,color:#D1FAE5;
    classDef runtime fill:#1E293B,stroke:#F59E0B,stroke-width:1px,color:#FEF3C7;
    classDef hardware fill:#0F172A,stroke:#64748B,stroke-width:1px,color:#CBD5E1;

    class IN_BILI,IN_LIVE,IN_MEM ingress;
    class VLM,PERSONA,ROUTER,MOMENT agent;
    class OUT_POST output;
    class AUDIO,STAGE obs;
    class CTX,COMPACT,DB mem;
    class LLM,TTS runtime;
    class JETSON,DUAL hardware;
```

---

## Quick Start

### Option A: NVIDIA Jetson Edge Device (Recommended ARM64 L4T R36.4)

Requires Docker with the NVIDIA Container Runtime. Run as a regular user with Docker privileges:

```bash
curl -fsSL https://raw.githubusercontent.com/4everWZ/Tuber_Studio/main/tools/jetson/run-ready.sh -o run-ready.sh
bash run-ready.sh --data ./tuber-data
```

The launcher will pull `ghcr.io/4everwz/tuber_studio:r36.4`, download the required models, and prepare the default voice.  
Open [http://127.0.0.1:9020](http://127.0.0.1:9020) and sign in using the administrator token in `tuber-data/admin-token`.

---

### Option B: Local Development (Windows / Linux / macOS)

Requires **Python 3.12** and **[uv](https://docs.astral.sh/uv/)**:

```bash
# 1. Install dependencies
uv sync --python 3.12

# 2. Start the service
uv run python -m tuber_studio
```

Open [http://127.0.0.1:8020](http://127.0.0.1:8020). The administrator token is located in `runtime/admin-token`. Configure local llama.cpp endpoints, TTS providers, or OpenAI-compatible APIs in the **Settings** menu.

---

### Option C: Dual-Node Deployment

For setups where inference requirements are heavy, the Web UI and OBS player can run on a primary PC, while model inference is handled by an idle GPU computer or Jetson over the local area network. See the [Deployment Guide](docs/guides/deployment.md) for network details.

---

## Character States

The built-in character "Da-Fei-Yu" includes 6 pre-configured animation states:
- **Idle**: Resting breath rhythms and blinking animations
- **Talking**: Lip movement during TTS speech or chat interactions
- **Cheerful**: Triggered by positive feedback or high affinity
- **Confused**: Complex reasoning or channel evaluation
- **Angry**: Triggered by criticism or playful banter
- **Gift**: Celebration state upon receiving gifts or donations

To create a custom character (OC), use the "Character Profiles" menu to customize personality rules, replace sprite images, and bind custom voice profiles. See the [Original Character Guide](docs/guides/characters.md).

---

## Documentation

- [Deployment, Updates, and Network Configuration](docs/guides/deployment.md)
- [Create an Original Character (OC)](docs/guides/characters.md)
- [Comments and Autonomous Posting](docs/guides/operations.md)
- [Live Workspace and OBS Setup](docs/guides/live.md)
- [Context and Compaction Specification](docs/specs/context-and-compaction.md)
- [CI and ARM64 Image Builds](https://github.com/4everWZ/Tuber_Studio/actions/workflows/ci.yml)

---

## Roadmap

- [ ] Real-time danmaku adapters for live streams (Bilibili / Douyin)
- [ ] Pluggable singing voice model interface (Singing Voice Conversion)
- [ ] Real-time gameplay screen capture and companion commentary
- [ ] Multi-character co-hosting mechanics

---

## Acknowledgements

Built with MiniCPM, llama.cpp, GPT-SoVITS, and Genie-TTS. Third-party terms are available in the [Genie-TTS license](third_party/Genie-TTS/LICENSE), [Codex license](third_party/openai-codex/LICENSE), and [Codex notices](third_party/openai-codex/NOTICE).

Q-style artwork by [YunYueSama](https://github.com/YunYueSama/codex-deepseek-pet), adapted into six GIF states under the [upstream asset terms](third_party/whale-avatar/UPSTREAM.md). Other artwork and voice asset sources are recorded in the [asset manifest](assets/whale/manifest.json) and [voice profiles](config/voices.json).
