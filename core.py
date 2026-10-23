"""Core (UI-free) download logic for KD Mobile.

Ported and simplified from KemonoDownloader (MIT, (c) 2025 Izeno):
domain handling, URL parsing, post/creator API access, file detection and
polite per-host download limiting. No Qt, no bs4 -> runs on Android.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import namedtuple
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import requests

APP_VERSION = "1.0.0"
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
IDENTIFIABLE_UA = f"KDMobile/{APP_VERSION}"
DOMAIN_LIST_URL = (
    "https://raw.githubusercontent.com/VoxDroid/KemonoDownloader"
    "/refs/heads/main/assets/config/domain"
)
FALLBACK_DOMAINS = ["kemono.cr", "coomer.st", "pawchive.pw"]

DEFAULT_EXTENSIONS = [
    ".jpg", ".jpeg", ".png", ".gif", ".zip", ".mp4", ".pdf", ".7z", ".mp3",
    ".wav", ".flac", ".var", ".rar", ".mov", ".docx", ".psd", ".clip",
    ".jpe", ".webp",
]

PAWCHIVE_MAX_ACTIVE = 5
PAWCHIVE_INTERVAL = 1.0

Target = namedtuple("Target", "config service creator_id post_id")
FileItem = namedtuple("FileItem", "name url post_id post_title")

_domains_cache: Optional[List[str]] = None
_domains_lock = threading.Lock()


# ---------------------------------------------------------------- domains
def get_domains() -> List[str]:
    global _domains_cache
    with _domains_lock:
        if _domains_cache is None:
            domains = None
            try:
                r = requests.get(DOMAIN_LIST_URL, timeout=5)
                r.raise_for_status()
                domains = [l.strip() for l in r.text.splitlines() if l.strip()]
            except Exception:
                pass
            _domains_cache = domains or list(FALLBACK_DOMAINS)
        return _domains_cache


def is_pawchive(domain_or_url: str) -> bool:
    host = (urlparse(domain_or_url).hostname or domain_or_url).lower()
    return host.startswith("pawchive.") or host.startswith("file.pawchive.") or (
        ".pawchive." in host
    )


def build_config(domain: str) -> Dict[str, str]:
    file_base = f"https://file.{domain}" if domain.startswith("pawchive.") else f"https://{domain}"
    return {
        "domain": domain,
        "base_url": f"https://{domain}",
        "api_base": f"https://{domain}/api/v1",
        "referer": f"https://{domain}/",
        "file_base_url": file_base,
    }


def get_domain_config(url: str, domains: Optional[List[str]] = None) -> Dict[str, str]:
    domains = domains or get_domains()
    for d in domains:
        if d in url:
            return build_config(d)
    return build_config(domains[0])


def clean_file_url(file_url: str, cfg: Dict[str, str]) -> str:
    file_url = file_url.strip()
    if file_url.startswith("//"):  # protocol-relative, e.g. //img.kemono.cr/...
        file_url = "https:" + file_url
    if "/thumbnail/" in file_url or "img." in file_url:
        return urljoin(cfg["base_url"] + "/", file_url)
    full = urljoin(cfg["file_base_url"] + "/", file_url)
    domain = cfg["domain"]
    p = urlparse(full)
    if domain.startswith("pawchive.") and p.netloc == domain:
        p = p._replace(netloc=f"file.{domain}")
    # API paths look like /07/df/<hash>.png; real files live under /data/
    if p.netloc.endswith(domain) and not p.path.startswith("/data/"):
        p = p._replace(path="/data/" + p.path.lstrip("/"))
    return urlunparse(p)


# ---------------------------------------------------------------- parsing
_PATH_RE = re.compile(
    r"^/(?:api/v1/)?(?P<service>[^/]+)/user/(?P<creator>[^/?#]+)"
    r"(?:/post/(?P<post>[^/?#]+))?/?$"
)


def parse_url(url: str, domains: Optional[List[str]] = None) -> Target:
    """Parse a creator or post URL. Raises ValueError if unsupported."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    path = urlparse(url).path
    m = _PATH_RE.match(path)
    if not m:
        raise ValueError(
            "Unsupported URL. Use a creator page "
            "(…/<service>/user/<id>) or a post (…/<service>/user/<id>/post/<id>)."
        )
    return Target(get_domain_config(url, domains), m["service"], m["creator"], m["post"])


def sanitize_filename(name: str, max_length: int = 100) -> str:
    if not name:
        return "unnamed"
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).replace(" ", "_")
    s = re.sub(r"_+", "_", s).strip("._ ")
    if len(s) > max_length:
        root, ext = os.path.splitext(s)
        s = root[: max_length - len(ext)] + ext
    return s or "unnamed"


# ---------------------------------------------------------------- http
class Http:
    def __init__(self) -> None:
        self._local = threading.local()

    def session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            self._local.s = s
        return s

    @staticmethod
    def headers(cfg_referer: str, url: str, accept: str = "*/*") -> Dict[str, str]:
        return {
            "User-Agent": IDENTIFIABLE_UA if is_pawchive(url) else BROWSER_UA,
            "Referer": cfg_referer,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        }

    def get_json(self, url: str, referer: str, retries: int = 3):
        for attempt in range(retries):
            try:
                r = self.session().get(
                    url, headers=self.headers(referer, url, "application/json"), timeout=15
                )
                if r.status_code == 403:  # same fallback the desktop app uses
                    r = self.session().get(
                        url, headers=self.headers(referer, url, "text/css"), timeout=15
                    )
                if r.status_code == 200:
                    return json.loads(r.content.decode("utf-8"))
                if r.status_code == 404:
                    return None
            except Exception:
                if attempt == retries - 1:
                    raise
            time.sleep(2 ** attempt)
        return None


class _Limiter:
    """pawchive politeness: <=5 active downloads, >=1s between requests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.next_at = 0.0

    def acquire(self, running: Callable[[], bool]) -> bool:
        while running():
            with self.lock:
                now = time.monotonic()
                if self.active < PAWCHIVE_MAX_ACTIVE and now >= self.next_at:
                    self.active += 1
                    self.next_at = now + PAWCHIVE_INTERVAL
                    return True
                wait = max(0.05, self.next_at - now)
            time.sleep(min(wait, 0.1))
        return False

    def release(self) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)


_limiter = _Limiter()


# ---------------------------------------------------------------- API
class _ImgParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.srcs: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            src = dict(attrs).get("src")
            if src:
                self.srcs.append(src)


def _eff_ext(path: str, name: str) -> str:
    n = os.path.splitext(name or "")[1].lower()
    return n if n else os.path.splitext(path or "")[1].lower()


def extract_files(post: dict, cfg: Dict[str, str], allowed: List[str]) -> List[Tuple[str, str]]:
    allowed = [a.lower() for a in allowed]
    out: List[Tuple[str, str]] = []

    def add(path: str, name: str) -> None:
        if _eff_ext(path, name) not in allowed:
            return
        url = clean_file_url(path, cfg)
        if "f=" not in url and name:
            url += f"?f={name}"
        out.append((name or os.path.basename(path), url))

    f = post.get("file")
    if isinstance(f, dict) and f.get("path"):
        add(f["path"], f.get("name", ""))
    for a in post.get("attachments") or []:
        if isinstance(a, dict) and a.get("path"):
            add(a["path"], a.get("name", ""))
    content = post.get("content")
    if content:
        p = _ImgParser()
        try:
            p.feed(content)
        except Exception:
            pass
        for src in p.srcs:
            url = clean_file_url(src, cfg)
            if os.path.splitext(urlparse(url).path)[1].lower() in allowed:
                out.append((os.path.basename(urlparse(url).path), url))
    return list(dict.fromkeys(out))


def _unwrap_post(data) -> Optional[dict]:
    if isinstance(data, dict):
        return data.get("post", data) if isinstance(data.get("post"), dict) else data
    return None


def fetch_post(http: Http, t: Target) -> Optional[dict]:
    url = f"{t.config['api_base']}/{t.service}/user/{t.creator_id}/post/{t.post_id}"
    post = _unwrap_post(http.get_json(url, t.config["referer"]))
    return post or None


def fetch_creator_name(http: Http, t: Target) -> str:
    url = f"{t.config['api_base']}/{t.service}/user/{t.creator_id}/profile"
    try:
        data = http.get_json(url, t.config["referer"], retries=1)
        if isinstance(data, dict) and data.get("name"):
            return data["name"]
    except Exception:
        pass
    return t.creator_id


def fetch_creator_posts(
    http: Http, t: Target, running: Callable[[], bool], on_page: Callable[[int], None] = lambda n: None,
    page_size: int = 50,
) -> List[dict]:
    posts: List[dict] = []
    offset = 0
    while running():
        url = f"{t.config['api_base']}/{t.service}/user/{t.creator_id}/posts?o={offset}"
        data = http.get_json(url, t.config["referer"])
        if isinstance(data, dict):
            data = data.get("posts") or data.get("results") or []
        if not data:
            break
        posts.extend(p for p in data if isinstance(p, dict))
        on_page(len(posts))
        if len(data) < page_size:
            break
        offset += page_size
    return posts


def collect_files(
    http: Http, t: Target, allowed: List[str], running: Callable[[], bool],
    status: Callable[[str], None] = lambda s: None,
) -> Tuple[str, List[FileItem]]:
    """Resolve a creator/post URL into (display_name, files)."""
    items: List[FileItem] = []
    if t.post_id:
        post = fetch_post(http, t)
        if not post:
            raise RuntimeError("Post not found or the site returned no data.")
        title = post.get("title") or f"Post_{t.post_id}"
        for name, url in extract_files(post, t.config, allowed):
            items.append(FileItem(name, url, t.post_id, title))
        return title, items

    name = fetch_creator_name(http, t)
    posts = fetch_creator_posts(http, t, running, lambda n: status(f"Fetched {n} posts…"))
    for p in posts:
        pid = str(p.get("id", ""))
        full = p
        # listing entries are summaries; fetch full post only if they lack file data
        if not (p.get("file") or p.get("attachments")) and pid:
            try:
                full = fetch_post(http, Target(t.config, t.service, t.creator_id, pid)) or p
            except Exception:
                full = p
        title = p.get("title") or f"Post_{pid}"
        for fname, url in extract_files(full, t.config, allowed):
            items.append(FileItem(fname, url, pid, title))
    return name, items


# ---------------------------------------------------------------- download
def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}_{i}{ext}"):
        i += 1
    return f"{root}_{i}{ext}"


def download_file(
    http: Http, item: FileItem, referer: str, dest_dir: str,
    running: Callable[[], bool], on_bytes: Callable[[int], None] = lambda n: None,
    retries: int = 3,
) -> Optional[str]:
    """Download one file. Returns saved path, or None on stop. Raises on failure."""
    os.makedirs(dest_dir, exist_ok=True)
    fname = sanitize_filename(item.name or os.path.basename(urlparse(item.url).path))
    final = os.path.join(dest_dir, fname)
    part = final + ".part"
    limited = is_pawchive(item.url)
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        if limited and not _limiter.acquire(running):
            return None
        try:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            h = http.headers(referer, item.url)
            if have:
                h["Range"] = f"bytes={have}-"
            with http.session().get(item.url, headers=h, stream=True, timeout=30) as r:
                if r.status_code == 416:  # already complete
                    os.replace(part, unique_path(final))
                    return final
                r.raise_for_status()
                mode = "ab" if (have and r.status_code == 206) else "wb"
                with open(part, mode) as fh:
                    for chunk in r.iter_content(64 * 1024):
                        if not running():
                            return None
                        if chunk:
                            fh.write(chunk)
                            on_bytes(len(chunk))
            final = unique_path(final)
            os.replace(part, final)
            return final
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 ** attempt)
        finally:
            if limited:
                _limiter.release()
    raise RuntimeError(f"{item.name}: {last_err}")


class DownloadManager:
    """Runs downloads on worker threads and reports via callbacks."""

    def __init__(self, http: Http, workers: int = 3) -> None:
        self.http = http
        self.workers = max(1, workers)
        self._stop = threading.Event()

    def running(self) -> bool:
        return not self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()

    def run(
        self, items: List[FileItem], referer: str, dest_root: str, group: str,
        on_done: Callable[[int, int, int], None],  # (finished, failed, total)
        on_log: Callable[[str], None] = lambda s: None,
        publish: Optional[Callable[[str, str], str]] = None,  # (file, rel_dir) -> shown path
    ) -> None:
        self._stop.clear()
        lock = threading.Lock()
        state = {"ok": 0, "fail": 0}
        queue = list(items)
        total = len(queue)
        base = os.path.join(dest_root, sanitize_filename(group))

        def worker() -> None:
            while self.running():
                with lock:
                    if not queue:
                        return
                    it = queue.pop(0)
                sub = os.path.join(base, sanitize_filename(it.post_title)) if it.post_id else base
                try:
                    saved = download_file(self.http, it, referer, sub, self.running)
                    if saved:
                        shown = saved
                        if publish:
                            try:
                                rel = os.path.relpath(os.path.dirname(saved), dest_root)
                                shown = publish(saved, rel)
                                os.remove(saved)
                            except Exception as pe:  # noqa: BLE001
                                on_log(f"! could not copy to Downloads ({pe}); kept in {saved}")
                        with lock:
                            state["ok"] += 1
                        on_log(f"✓ {shown}")
                except Exception as e:  # noqa: BLE001
                    with lock:
                        state["fail"] += 1
                    on_log(f"✗ {e}")
                with lock:
                    done = state["ok"] + state["fail"]
                on_done(done, state["fail"], total)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
