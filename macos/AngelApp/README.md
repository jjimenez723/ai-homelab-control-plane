# Angel macOS app

## Quick setup

On an Apple silicon Mac with macOS 14 or later, [Homebrew](https://brew.sh)
and Xcode installed:

```bash
git clone https://github.com/jjimenez723/ai-homelab-control-plane.git && cd ai-homelab-control-plane && ./scripts/setup-angel
```

`scripts/setup-angel` installs Python 3, Ollama and ffmpeg with Homebrew if
they are missing, starts Ollama, pulls the default local model (`ai.model` in
`config/ingest.example.yaml`), creates `.venv` with `requirements-ingest.txt`,
and builds `~/Desktop/Angel.app`. Then open Angel.app and, if it asks, choose
the folder you cloned.

It is safe to run again: it only fills in what is missing. It never formats a
disk, moves or deletes files, uses sudo, or sets up credentials. Run
`./scripts/setup-angel --check` to see what is present and what is missing
without installing anything.

## Details

This directory contains the native SwiftUI control surface for Angel. The app
intentionally delegates file operations to the existing, tested scripts in the
selected Angel project folder. The model action delegates to
`scripts/analyze-media`, which talks only to the local Ollama service.

The app does not silently move or delete files. It detects mounted removable
media, lets the owner explicitly register an SD card with a single button, and
provides this review flow:

1. Click **Refresh preview** to see the actual eligible source files.
2. Select files for analysis and click **Analyze selected**. Angel shows local
   thumbnails, visible people/animals, existing profile matches when a catalog
   is available, proposed filenames, and proposed review folders.
   Long selections run in resumable batches. Click **Open Analysis Monitor** to
   watch the current image, the latest completed interpretation, and the
   completed-result history in a separate window.
3. Tick **Approve this move** only for the rows you accept. Unknown people and
   ambiguous images stay in `needs-review` until you decide what to do.
   Duplicate model names are resolved with deterministic suffixes before
   approval.
4. Use **Approve & rename all** to stage every analyzed proposal, or approve
   rows individually. Then click **Move & rename approved files** and confirm.

The Python CLI remains the authority for the transfer transaction itself. It
copies, verifies, catalogs, and only then removes originals from the source.

The GitHub build is an arm64 app bundle intended for local distribution. The
app icon is built from `Resources/AppIcon.icns` and is included in both the
Swift Package build and the Xcode target.

`scripts/setup-angel` runs this step for you. To rebuild and reinstall only
the app on the current user's Desktop, run:

```bash
./scripts/install-desktop-app
```

The resulting ~/Desktop/Angel.app is a normal double-clickable macOS app.
Angel automatically selects a valid local workspace when it can find one. If
it cannot, use the visible Choose Project Folder… control and select the
folder containing scripts/organize. The app stores that selection as a
security-scoped bookmark. Angel still deliberately does not bundle a
machine-specific Python environment or model weights.

The App Store target has a sandbox entitlement file, but archive/export still
requires the owner's Apple Developer signing identity, team, provisioning
profile, and App Store Connect submission credentials.

## Spatial OS boundary

Spatial OS is a separate macOS app under `macos/SpatialOSApp`. Angel remains
focused on local media organization, analysis, and transfer safety; it does
not host the Spatial OS visualization.
