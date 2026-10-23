# KD Mobile

A new Android app (Kivy) built on the download logic of KemonoDownloader v5.13.0
(MIT). The Qt desktop UI was replaced with a touch UI; `core.py` has no GUI code.

## What it does
Paste a creator URL (`…/<service>/user/<id>`) or a post URL
(`…/<service>/user/<id>/post/<id>`), tap **Scan**, then **Download**.
Supports kemono.cr, coomer.st, pawchive.pw. Files are saved per creator/post
with resume support and 3 parallel downloads.

Files go to the app's external storage folder
(`Android/data/org.kdmobile.kdmobile/files/KemonoDownloader`).

## Build the APK
**GitHub Actions (easiest):** push this folder to a GitHub repo, open the
Actions tab, run "Build APK", download the `kd-mobile-apk` artifact.

**Locally (Linux/WSL):**
```
pip install buildozer cython==0.29.36
buildozer android debug     # APK appears in bin/
```

## Tests
`pip install pytest requests && pytest tests` (offline, core logic only).

Note: the UI and APK build have not been run on a device; test on yours.
