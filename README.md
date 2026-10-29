# For the Clip (Clippie)

Turn a long recording into short, captioned clips on your Mac. The app is currently an **early alpha**: the source and command still use the name Clippie. This repository contains the application and landing page, not project recordings or exports.

## Get started

The source installation targets an **Apple silicon Mac**. You need macOS, [Homebrew](https://brew.sh/), Git, internet access, a provider API key for clip generation, and several GB of free space for dependencies and local models. Model calls may incur provider charges. The macOS installer has a separate assisted-alpha release process; this repository does not provide a ready-to-download installer.

1. Install the prerequisites in Terminal:

   ```sh
   brew install uv ffmpeg
   git clone https://github.com/abinashkarki/for_the_clip.git
   cd for_the_clip
   ```

   The repository is private, so cloning requires GitHub access to it.

2. Install Python dependencies and the app. `uv` will use Python 3.12 or 3.13 and can install a suitable Python version if necessary:

   ```sh
   uv sync --extra local --extra semantic --extra vision
   ```

3. Prepare the speech, embedding, and face-detection models. The command displays the publishers' terms before downloading and can be rerun after an interruption:

   ```sh
   uv run --no-sync python scripts/setup_models.py
   ```

4. Start the app and open [http://127.0.0.1:8765](http://127.0.0.1:8765):

   ```sh
   uv run --no-sync clippie
   ```

   Keep Terminal open while using the app; press Ctrl-C to stop it. Use `uv run --no-sync clippie --port 8766` if the port is occupied.

5. Open **Settings**, add your own AI provider key and check model routing. Then add a video in **Library**, create a project, review the suggested clips, and export the ones you want. The configured model must be available to your provider account; choose another supported model in Settings if needed.

## Data and privacy

Your recordings, project database, model files and exports live in `data/` by default, which Git ignores. Set `CLIPPIE_DATA_DIR` to use another location. Keep your own backups; this alpha has incomplete source-cleanup and deletion features. Never commit `data/`, API keys, `.env` files, or exports.

The app processes media locally, but sends transcript text and selected video frames to the AI provider configured for clip generation. Optional cloud transcription sends audio to its provider; adding a video URL contacts its source. Check provider terms before use. Keys entered in Settings are stored in macOS Keychain. See [telemetry notes](docs/TELEMETRY.md).

## Development

The built editor is included so Node is not needed to run the app. To modify it:

```sh
cd web
npm ci
npm run build
npm test
```

For Python tests, run `uv sync --extra local --extra semantic --extra vision --extra dev` then `uv run --no-sync pytest -q`. Some tests may require additional system resources. The optional developer mock video is excluded from this repository; mock mode may show an unavailable preview.

The static landing page is in `website/`; preview it with `python3 -m http.server 8813 --directory website`. Its download links refer to the existing private-alpha host and are not an installer included in this repository.
