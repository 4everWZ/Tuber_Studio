# Genie-TTS v2 conversion subset

Source: [High-Logic/Genie-TTS](https://github.com/High-Logic/Genie-TTS/tree/d347fd0f8683e9a362b69f59fa0a4799ddb5e828), revision `d347fd0f8683e9a362b69f59fa0a4799ddb5e828`, MIT; see [LICENSE](LICENSE).

The 17 upstream files are copied without changes: v2 conversion code, its checkpoint loader/compatibility module/constants, four graph templates and two key maps. The root package initializer is present for resource lookup but is not executed by the fixed converter namespace. No voice checkpoints, public server, playback system or additional language frontend is bundled.

Project adapters live in `tools/jetson/tts/`. They restore the original GPT-SoVITS sampling/EOS and audio postprocessing, prepare INT8 artifacts offline, and verify voice-specific bundles before serving. The upstream graph templates retain their original bytes; runtime modifications create separate artifacts.
