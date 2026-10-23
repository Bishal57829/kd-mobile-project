"""KD Mobile - Kivy front-end. Core logic lives in core.py."""
import mimetypes
import os
import shutil
import threading

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

import core


def downloads_dir() -> str:
    """App-specific external storage (no permission needed on any Android)."""
    try:
        from jnius import autoclass  # type: ignore

        ctx = autoclass("org.kivy.android.PythonActivity").mActivity
        d = ctx.getExternalFilesDir(None)
        if d is not None:
            return os.path.join(d.getAbsolutePath(), "KemonoDownloader")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "KemonoDownloader")


PUBLIC_SUBDIR = "KemonoDownloader"


def publish_to_downloads(src: str, rel_dir: str) -> str:
    """Copy a finished file into the public Download/KemonoDownloader folder."""
    from jnius import autoclass  # type: ignore

    name = os.path.basename(src)
    rel_dir = "" if rel_dir in (".", "") else rel_dir.replace(os.sep, "/")
    rel_path = "/".join(p for p in ("Download", PUBLIC_SUBDIR, rel_dir) if p)
    sdk = autoclass("android.os.Build$VERSION").SDK_INT

    if sdk >= 29:  # MediaStore: works without any storage permission
        ctx = autoclass("org.kivy.android.PythonActivity").mActivity
        resolver = ctx.getContentResolver()
        ContentValues = autoclass("android.content.ContentValues")
        Downloads = autoclass("android.provider.MediaStore$Downloads")
        cv = ContentValues()
        cv.put("_display_name", name)
        cv.put("mime_type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        cv.put("relative_path", rel_path)
        uri = resolver.insert(Downloads.EXTERNAL_CONTENT_URI, cv)
        if uri is None:
            raise RuntimeError("MediaStore refused the file")
        out = resolver.openOutputStream(uri)
        try:
            with open(src, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        finally:
            out.close()
        return f"{rel_path}/{name}"

    # Android 7-9: plain file copy (needs WRITE_EXTERNAL_STORAGE, requested at start)
    Env = autoclass("android.os.Environment")
    root = Env.getExternalStoragePublicDirectory(Env.DIRECTORY_DOWNLOADS).getAbsolutePath()
    dest = os.path.join(root, PUBLIC_SUBDIR, rel_dir)
    os.makedirs(dest, exist_ok=True)
    final = core.unique_path(os.path.join(dest, name))
    shutil.copyfile(src, final)
    return final


def on_android() -> bool:
    try:
        from jnius import autoclass  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


class Root(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(8), **kw)
        self.http = core.Http()
        self.manager = core.DownloadManager(self.http, workers=3)
        self.items = []
        self.referer = ""
        self.group = "download"
        self.busy = False

        self.url = TextInput(
            hint_text="Paste creator or post URL", multiline=False,
            size_hint_y=None, height=dp(48),
        )
        self.exts = TextInput(
            text=", ".join(core.DEFAULT_EXTENSIONS), multiline=True,
            size_hint_y=None, height=dp(80),
        )
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.scan_btn = Button(text="Scan", on_release=lambda *_: self.scan())
        self.dl_btn = Button(text="Download", disabled=True, on_release=lambda *_: self.download())
        self.stop_btn = Button(text="Stop", disabled=True, on_release=lambda *_: self.manager.stop())
        for b in (self.scan_btn, self.dl_btn, self.stop_btn):
            row.add_widget(b)
        self.bar = ProgressBar(max=1, value=0, size_hint_y=None, height=dp(12))
        self.status = Label(text="Ready", size_hint_y=None, height=dp(28))
        self.log = Label(text="", size_hint_y=None, halign="left", valign="top", markup=False)
        self.log.bind(width=lambda *_: setattr(self.log, "text_size", (self.log.width, None)))
        self.log.bind(texture_size=lambda *_: setattr(self.log, "height", self.log.texture_size[1]))
        scroll = ScrollView()
        scroll.add_widget(self.log)

        for w in (Label(text="File types", size_hint_y=None, height=dp(24)), ):
            self.add_widget(self.url)
            self.add_widget(w)
            self.add_widget(self.exts)
        for w in (row, self.bar, self.status, scroll):
            self.add_widget(w)

    # ---- helpers (thread-safe UI updates)
    def ui(self, fn):
        Clock.schedule_once(lambda dt: fn(), 0)

    def say(self, text):
        def f():
            self.log.text = (self.log.text + text + "\n")[-20000:]
        self.ui(f)

    def set_status(self, text):
        self.ui(lambda: setattr(self.status, "text", text))

    def set_busy(self, busy, can_download=None):
        def f():
            self.busy = busy
            self.scan_btn.disabled = busy
            self.stop_btn.disabled = not busy
            self.dl_btn.disabled = busy or not (self.items if can_download is None else can_download)
        self.ui(f)

    def allowed(self):
        raw = [e.strip().lower() for e in self.exts.text.replace("\n", ",").split(",")]
        return [e if e.startswith(".") else "." + e for e in raw if e]

    # ---- actions
    def scan(self):
        if self.busy:
            return
        try:
            target = core.parse_url(self.url.text)
        except ValueError as e:
            self.set_status(str(e))
            return
        self.items = []
        self.manager._stop.clear()
        self.set_busy(True, False)
        self.set_status("Scanning…")
        allowed = self.allowed()

        def work():
            try:
                name, items = core.collect_files(
                    self.http, target, allowed, self.manager.running, self.set_status
                )
                self.items, self.group = items, name
                self.referer = target.config["referer"]
                posts = len({i.post_id for i in items})
                self.set_status(f"{name}: {len(items)} files in {posts} posts")
            except Exception as e:  # noqa: BLE001
                self.set_status(f"Scan failed: {e}")
            finally:
                self.set_busy(False)

        threading.Thread(target=work, daemon=True).start()

    def download(self):
        if self.busy or not self.items:
            return
        self.set_busy(True)
        items, dest = list(self.items), downloads_dir()
        self.say(f"Saving to Download/{PUBLIC_SUBDIR}" if on_android() else f"Saving to {dest}")

        def progress(done, failed, total):
            def f():
                self.bar.max = max(total, 1)
                self.bar.value = done
                self.status.text = f"{done}/{total} done, {failed} failed"
            self.ui(f)

        def work():
            try:
                self.manager.run(
                    items, self.referer, dest, self.group, progress, self.say,
                    publish=publish_to_downloads if on_android() else None,
                )
                self.set_status("Finished" if self.manager.running() else "Stopped")
            finally:
                self.set_busy(False)

        threading.Thread(target=work, daemon=True).start()


class KDMobileApp(App):
    title = "KD Mobile"

    def build(self):
        Window.softinput_mode = "below_target"
        return Root()

    def on_start(self):
        # Android 7-9 need a runtime storage permission; 10+ use MediaStore (none needed)
        try:
            from jnius import autoclass  # type: ignore
            if autoclass("android.os.Build$VERSION").SDK_INT < 29:
                from android.permissions import Permission, request_permissions  # type: ignore
                request_permissions([Permission.WRITE_EXTERNAL_STORAGE])
        except Exception:
            pass


if __name__ == "__main__":
    KDMobileApp().run()
