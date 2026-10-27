#!/usr/bin/env python3
"""
auto_unpack.py — watch a folder and auto-extract new archives.

Commands:
  watch  [--config PATH] [--dry-run]   # daemon
  scan   [--config PATH] [--dry-run]   # process existing once
  retry  [--config PATH]               # prompt for missing passwords
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

try:
    import py7zr
except ImportError:
    py7zr = None

try:
    import rarfile
except ImportError:
    rarfile = None


log = logging.getLogger("auto-unpack")

DEFAULT_FORMATS = [
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz", ".7z", ".rar",
]
JUNK_PATTERNS = [
    "__MACOSX", ".DS_Store", "._*", "Thumbs.db", "desktop.ini",
    ".Spotlight-V100", ".Trashes", ".fseventsd",
]


# ---------- config / state ----------

@dataclass
class Config:
    watch_dir: Path
    extract_root: Path | None = None
    formats: list[str] = field(default_factory=lambda: list(DEFAULT_FORMATS))
    remove_after: str = "keep"       # keep | delete | move
    move_to: Path | None = None
    ignore_globs: list[str] = field(default_factory=list)
    stability_seconds: int = 5
    stability_timeout: int = 300
    recursive_nested: bool = False
    max_depth: int = 2
    clean_junk: bool = True
    notify: bool = True
    log_file: Path | None = None
    log_level: str = "INFO"
    passwords: list[str] = field(default_factory=list)
    state_file: Path | None = None


@dataclass
class State:
    passwords: dict[str, str] = field(default_factory=dict)
    needs_password: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "State":
        if path.exists():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                return cls(
                    passwords=d.get("passwords", {}),
                    needs_password=d.get("needs_password", []),
                )
            except Exception:
                log.warning("Could not read state, starting fresh")
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"passwords": self.passwords, "needs_password": self.needs_password},
            indent=2, ensure_ascii=False), encoding="utf-8")


def default_state_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "auto-unpack" / "state.json"


def _expand(p) -> Path | None:
    if p is None or p == "":
        return None
    return Path(os.path.expanduser(str(p)))


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        d = tomllib.load(f)

    cfg = Config(
        watch_dir=_expand(d.get("watch_dir")),
        extract_root=_expand(d.get("extract_root")),
        formats=list(d.get("formats", DEFAULT_FORMATS)),
        remove_after=str(d.get("remove_after", "keep")),
        move_to=_expand(d.get("move_to")),
        ignore_globs=list(d.get("ignore_globs", [])),
        stability_seconds=int(d.get("stability_seconds", 5)),
        stability_timeout=int(d.get("stability_timeout", 300)),
        recursive_nested=bool(d.get("recursive_nested", False)),
        max_depth=int(d.get("max_depth", 2)),
        clean_junk=bool(d.get("clean_junk", True)),
        notify=bool(d.get("notify", True)),
        log_file=_expand(d.get("log_file")),
        log_level=str(d.get("log_level", "INFO")),
        passwords=list(d.get("passwords", [])),
        state_file=_expand(d.get("state_file")),
    )
    if cfg.watch_dir is None:
        raise ValueError("watch_dir is required")
    return cfg


def setup_logging(cfg: Config) -> None:
    root = logging.getLogger()
    root.setLevel(cfg.log_level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    if cfg.log_file is None and not sys.stderr.isatty():
        cfg.log_file = default_state_path().parent / "auto-unpack.log"

    if sys.stderr.isatty():
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    if cfg.log_file is not None:
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(cfg.log_file, maxBytes=5_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    if not root.handlers:
        root.addHandler(logging.NullHandler())


# ---------- helpers ----------

class PasswordRequired(Exception):
    def __init__(self, archive: Path):
        self.archive = archive
        super().__init__(f"Password required for {archive}")


def detect_format(name: str, formats: list[str]) -> str | None:
    lower = name.lower()
    for ext in sorted(formats, key=len, reverse=True):
        e = ext.lower()
        if not lower.endswith(e):
            continue
        if e == ".zip":
            return "zip"
        if e == ".7z":
            return "7z"
        if e == ".rar":
            return "rar"
        if e in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"):
            return "tar"
    return None


def is_archive(p: Path, formats: list[str]) -> bool:
    return detect_format(p.name, formats) is not None


def strip_format_suffix(name: str, formats: list[str]) -> str:
    lower = name.lower()
    for ext in sorted(formats, key=len, reverse=True):
        if lower.endswith(ext.lower()):
            return name[: -len(ext)]
    return Path(name).stem


def unique_dir_path(base: Path) -> Path:
    if not base.exists():
        return base
    i = 1
    while True:
        cand = base.with_name(f"{base.name} ({i})")
        if not cand.exists():
            return cand
        i += 1


def unique_file_path(base: Path) -> Path:
    if not base.exists():
        return base
    suffixes = "".join(base.suffixes)
    stem = base.name[: -len(suffixes)] if suffixes else base.name
    i = 1
    while True:
        cand = base.with_name(f"{stem} ({i}){suffixes}")
        if not cand.exists():
            return cand
        i += 1


def match_any(name: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in globs)


def is_junk(name: str) -> bool:
    for part in name.replace("\\", "/").split("/"):
        if not part:
            continue
        for pat in JUNK_PATTERNS:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def is_safe_member(name: str, target_dir: Path) -> bool:
    """Zip-slip protection."""
    try:
        target = target_dir.resolve()
        candidate = (target / name).resolve()
        candidate.relative_to(target)
        return True
    except (OSError, ValueError):
        return False


def wait_until_stable(path: Path, seconds: int, timeout: int) -> bool:
    last = -1
    stable_since: float | None = None
    start = time.time()
    while time.time() - start < timeout:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last and size > 0:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= seconds:
                return True
        else:
            last = size
            stable_since = None
        time.sleep(0.5)
    return False


def cleanup_junk(target: Path) -> None:
    for root, dirs, files in os.walk(target, topdown=False):
        rp = Path(root)
        for f in files:
            if is_junk(f):
                try:
                    (rp / f).unlink()
                except OSError:
                    pass
        for d in dirs:
            if is_junk(d):
                shutil.rmtree(rp / d, ignore_errors=True)


# ---------- notifications ----------

def _applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _notify_windows(title: str, message: str) -> None:
    def psq(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        f"$n.BalloonTipTitle={psq(title)};"
        f"$n.BalloonTipText={psq(message)};"
        "$n.Visible=$true;"
        "$n.ShowBalloonTip(5000);"
        "Start-Sleep -Milliseconds 5500;"
        "$n.Dispose();"
    )
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
        check=False, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def notify(title: str, message: str) -> None:
    try:
        if sys.platform.startswith("linux"):
            subprocess.run(["notify-send", "-a", "auto-unpack", title, message],
                           check=False, timeout=5)
        elif sys.platform == "darwin":
            script = f"display notification {_applescript_quote(message)} with title {_applescript_quote(title)}"
            subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        elif sys.platform == "win32":
            _notify_windows(title, message)
    except Exception as e:
        log.debug("notify failed: %s", e)


# ---------- extraction ----------

def _verify_zip_password(zf: zipfile.ZipFile, pwd: str) -> bool:
    for info in zf.infolist():
        if (info.flag_bits & 0x1) and not info.is_dir():
            try:
                with zf.open(info, pwd=pwd.encode()) as f:
                    f.read(1)
                return True
            except (RuntimeError, zipfile.BadZipFile):
                return False
    return True


def _find_zip_password(zf, archive: Path, cfg: Config, state: State) -> str | None:
    candidates = list(cfg.passwords)
    cached = state.passwords.get(str(archive.resolve()))
    if cached and cached not in candidates:
        candidates.append(cached)
    for pwd in candidates:
        if _verify_zip_password(zf, pwd):
            state.passwords[str(archive.resolve())] = pwd
            return pwd
    return None


def _extract_zip(archive, target, cfg, state, state_path) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        encrypted = any(i.flag_bits & 0x1 for i in infos)
        pwd_bytes = None
        if encrypted:
            pwd = _find_zip_password(zf, archive, cfg, state)
            if pwd is None:
                raise PasswordRequired(archive)
            pwd_bytes = pwd.encode()
        else:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"Corrupted zip member: {bad}")

        for info in infos:
            if cfg.clean_junk and is_junk(info.filename):
                continue
            if not is_safe_member(info.filename, target):
                log.warning("Unsafe zip path skipped: %s", info.filename)
                continue
            try:
                zf.extract(info, target, pwd=pwd_bytes)
            except RuntimeError as e:
                if "password" in str(e).lower() or "encrypted" in str(e).lower():
                    raise PasswordRequired(archive)
                raise


def _extract_tar(archive, target, cfg, state, state_path) -> None:
    with tarfile.open(archive, "r:*") as tf:
        for m in tf.getmembers():
            if cfg.clean_junk and is_junk(m.name):
                continue
            if not is_safe_member(m.name, target):
                log.warning("Unsafe tar path skipped: %s", m.name)
                continue
            if m.issym() or m.islnk():
                link = Path(m.name).parent / m.linkname
                if not is_safe_member(str(link), target):
                    log.warning("Unsafe tar link skipped: %s", m.name)
                    continue
            try:
                if sys.version_info >= (3, 12):
                    tf.extract(m, target, filter="data")
                else:
                    tf.extract(m, target)
            except (tarfile.TarError, OSError) as e:
                log.warning("tar extract failed for %s: %s", m.name, e)


def _extract_7z(archive, target, cfg, state, state_path) -> None:
    if py7zr is None:
        raise RuntimeError("py7zr not installed (pip install py7zr)")
    candidates = [None] + list(cfg.passwords)
    cached = state.passwords.get(str(archive.resolve()))
    if cached and cached not in candidates:
        candidates.append(cached)

    last_err = None
    for pwd in candidates:
        try:
            with py7zr.SevenZipFile(archive, mode="r", password=pwd) as zf:
                for name in zf.getnames():
                    if not is_safe_member(name, target):
                        raise RuntimeError(f"Unsafe path in 7z: {name}")
                zf.extractall(path=target)
            if pwd:
                state.passwords[str(archive.resolve())] = pwd
            return
        except py7zr.PasswordRequired:
            last_err = "password"
            continue
        except py7zr.Bad7zFile:
            raise
        except Exception as e:
            last_err = e
            if "password" in str(e).lower():
                continue
            raise
    raise PasswordRequired(archive)


def _extract_rar(archive, target, cfg, state, state_path) -> None:
    if rarfile is None:
        raise RuntimeError("rarfile not installed (pip install rarfile; needs unrar/unar/7z)")
    candidates = [None] + list(cfg.passwords)
    cached = state.passwords.get(str(archive.resolve()))
    if cached and cached not in candidates:
        candidates.append(cached)

    for pwd in candidates:
        try:
            with rarfile.RarFile(archive) as rf:
                if pwd:
                    rf.setpassword(pwd)
                if rf.needs_password() and not pwd:
                    continue
                for name in rf.namelist():
                    if not is_safe_member(name, target):
                        raise RuntimeError(f"Unsafe path in rar: {name}")
                rf.extractall(path=target)
            if pwd:
                state.passwords[str(archive.resolve())] = pwd
            return
        except rarfile.PasswordRequired:
            continue
        except rarfile.BadRarFile:
            raise
        except rarfile.Error as e:
            if "password" in str(e).lower():
                continue
            raise
    raise PasswordRequired(archive)


def extract_archive(archive: Path, cfg: Config, state: State,
                    state_path: Path, depth: int = 0,
                    out_base: Path | None = None) -> Path | None:
    if out_base is None:
        out_base = cfg.extract_root or cfg.watch_dir
    out_base.mkdir(parents=True, exist_ok=True)

    stem = strip_format_suffix(archive.name, cfg.formats)
    target = unique_dir_path(out_base / stem)
    target.mkdir(parents=True)

    fmt = detect_format(archive.name, cfg.formats)
    if fmt is None:
        log.warning("Unknown format: %s", archive)
        shutil.rmtree(target, ignore_errors=True)
        return None

    log.info("Extracting %s -> %s [%s]", archive.name, target, fmt)
    try:
        if fmt == "zip":
            _extract_zip(archive, target, cfg, state, state_path)
        elif fmt == "tar":
            _extract_tar(archive, target, cfg, state, state_path)
        elif fmt == "7z":
            _extract_7z(archive, target, cfg, state, state_path)
        elif fmt == "rar":
            _extract_rar(archive, target, cfg, state, state_path)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    if cfg.clean_junk:
        cleanup_junk(target)

    if cfg.recursive_nested and depth < cfg.max_depth:
        for entry in list(target.iterdir()):
            if entry.is_file() and is_archive(entry, cfg.formats):
                try:
                    extract_archive(entry, cfg, state, state_path,
                                    depth=depth + 1, out_base=entry.parent)
                except Exception:
                    log.exception("Nested extraction failed: %s", entry)

    return target


def process_archive(archive: Path, cfg: Config, state: State, state_path: Path) -> None:
    log.info("Processing: %s", archive)
    key = str(archive.resolve())
    try:
        target = extract_archive(archive, cfg, state, state_path)
    except PasswordRequired:
        log.warning("Password required: %s", archive)
        if key not in state.needs_password:
            state.needs_password.append(key)
            state.save(state_path)
        if cfg.notify:
            notify("auto-unpack", f"Password required: {archive.name}")
        return
    except Exception:
        log.exception("Extraction failed: %s", archive)
        if cfg.notify:
            notify("auto-unpack", f"Failed: {archive.name}")
        return

    if key in state.needs_password:
        state.needs_password.remove(key)
        state.save(state_path)

    if target is None:
        return

    if cfg.notify:
        notify("auto-unpack", f"Extracted: {archive.name} → {target.name}")

    if cfg.remove_after == "delete":
        try:
            archive.unlink()
            log.info("Deleted archive: %s", archive)
        except OSError as e:
            log.warning("Could not delete %s: %s", archive, e)
    elif cfg.remove_after == "move" and cfg.move_to is not None:
        try:
            cfg.move_to.mkdir(parents=True, exist_ok=True)
            dest = unique_file_path(cfg.move_to / archive.name)
            shutil.move(str(archive), str(dest))
            log.info("Moved archive -> %s", dest)
        except OSError as e:
            log.warning("Could not move %s: %s", archive, e)


# ---------- watcher ----------

class _App:
    def __init__(self, cfg: Config, state: State, state_path: Path):
        self.cfg = cfg
        self.state = state
        self.state_path = state_path
        self.q: queue.Queue = queue.Queue()
        self.seen: set[Path] = set()
        self.seen_lock = threading.Lock()
        self.running = True
        self.worker = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.running = False
        self.q.put(None)
        self.worker.join(timeout=10)

    def enqueue(self, path: Path) -> None:
        if not is_archive(path, self.cfg.formats):
            return
        if match_any(path.name, self.cfg.ignore_globs):
            log.info("Ignoring: %s", path.name)
            return
        rp = path.resolve()
        with self.seen_lock:
            if rp in self.seen:
                return
            self.seen.add(rp)
        self.q.put(rp)

    def _loop(self) -> None:
        while self.running:
            try:
                item = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._process(item)
            except Exception:
                log.exception("Worker error on %s", item)

    def _process(self, path: Path) -> None:
        if not path.exists():
            log.warning("Gone before processing: %s", path)
            return
        if not wait_until_stable(path, self.cfg.stability_seconds, self.cfg.stability_timeout):
            log.warning("File never stabilized: %s", path)
            return
        process_archive(path, self.cfg, self.state, self.state_path)


class _Handler(FileSystemEventHandler):
    def __init__(self, app: _App):
        self.app = app
        self.watch_dir = app.cfg.watch_dir.resolve()

    def _maybe(self, path_str: str) -> None:
        p = Path(path_str)
        try:
            if p.parent.resolve() != self.watch_dir:
                return  # only direct children
        except OSError:
            return
        self.app.enqueue(p)

    def on_created(self, event):
        if not event.is_directory:
            self._maybe(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._maybe(event.dest_path)


def run_watch(cfg: Config, state: State, state_path: Path) -> None:
    cfg.watch_dir.mkdir(parents=True, exist_ok=True)
    app = _App(cfg, state, state_path)
    app.start()

    observer = Observer()
    observer.schedule(_Handler(app), str(cfg.watch_dir), recursive=False)
    observer.start()
    log.info("Watching %s", cfg.watch_dir)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        observer.stop()
        observer.join()
        app.stop()


# ---------- scan / retry ----------

def run_scan(cfg: Config, state: State, state_path: Path, dry_run: bool) -> None:
    if not cfg.watch_dir.exists():
        log.error("Watch dir does not exist: %s", cfg.watch_dir)
        return
    log.info("Scanning %s", cfg.watch_dir)
    for entry in sorted(cfg.watch_dir.iterdir()):
        if not entry.is_file():
            continue
        if not is_archive(entry, cfg.formats):
            continue
        if match_any(entry.name, cfg.ignore_globs):
            log.info("Ignoring: %s", entry.name)
            continue
        if dry_run:
            log.info("[dry-run] would process: %s", entry.name)
            continue
        if not wait_until_stable(entry, cfg.stability_seconds, cfg.stability_timeout):
            log.warning("Skipping unstable: %s", entry.name)
            continue
        process_archive(entry, cfg, state, state_path)


def run_retry(cfg: Config, state: State, state_path: Path) -> None:
    if not state.needs_password:
        log.info("Nothing to retry")
        return
    for key in list(state.needs_password):
        path = Path(key)
        if not path.exists():
            log.warning("Missing file, dropping from queue: %s", key)
            state.needs_password.remove(key)
            state.save(state_path)
            continue
        print(f"\nArchive: {path}")
        pwd = input("Password (empty to skip): ").strip()
        if not pwd:
            continue
        state.passwords[key] = pwd
        state.save(state_path)
        try:
            target = extract_archive(path, cfg, state, state_path)
            if target is not None:
                state.needs_password.remove(key)
                state.save(state_path)
                log.info("OK: %s -> %s", path.name, target.name)
                if cfg.notify:
                    notify("auto-unpack", f"Extracted: {path.name}")
        except PasswordRequired:
            log.error("Password still wrong for %s", path.name)
            state.passwords.pop(key, None)
            state.save(state_path)
        except Exception:
            log.exception("Failed: %s", path)


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser(prog="auto-unpack")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("watch", "run as a daemon"),
        ("scan",  "process existing archives once"),
        ("retry", "retry archives that need a password"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--config", default="config.toml")
        if name in ("watch", "scan"):
            p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(cfg)
    state_path = cfg.state_file or default_state_path()
    state = State.load(state_path)

    if args.cmd == "watch":
        run_watch(cfg, state, state_path)
    elif args.cmd == "scan":
        run_scan(cfg, state, state_path, args.dry_run)
    elif args.cmd == "retry":
        run_retry(cfg, state, state_path)


if __name__ == "__main__":
    main()