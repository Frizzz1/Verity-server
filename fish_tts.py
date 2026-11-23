"""
Fish Audio TTS for Verity bridge (shared Windows + Android via sync_both).

Voice: FISH_REFERENCE_ID. Plays on PC (sounddevice) or Termux (media player).
On Termux, clips go to shared storage as MP3 (private tmp is unreadable).
Temp files are deleted after play; leftover clips are purged on startup.
Prosody: S2.1 bracket tags ([whispering], [angry], …) from delivery style.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from typing import Optional

import httpx

FISH_API_URL = "https://api.fish.audio/v1/tts"
DEFAULT_REFERENCE_ID = ""
DEFAULT_FISH_MODEL = "s2.1-pro-free"

_SECTION = re.compile(r"§.")
_FISH_TAG = re.compile(r"\[[^\]]{1,48}\]")
_purge_done = False

# Delivery styles → Fish S2.1 natural-language control tags
_STYLE_PREFIX = {
    "normal": "",
    "calm": "[calm] ",
    "happy": "[happy] ",
    "curious": "[curious] ",
    "phase1": "[happy][energetic] ",
    "phase2": "[hesitant][nervous][soft tone] ",
    "phase3": "[whispering][soft tone] ",
    "phase4": "[angry][frustrated] ",
    "angry": "[angry][frustrated] ",
    "scold": "[angry][emphasis] ",
    "scared": "[scared][whispering] ",
    "sad": "[sad][soft tone] ",
    "apology": "[sad][soft tone][gentle] ",
    "excited": "[happy][excited] ",
}


def _strip_mc_formatting(text: str) -> str:
    return _SECTION.sub("", str(text or "")).strip()


def fish_tts_configured() -> bool:
    key = os.getenv("FISH_API_KEY", "").strip()
    return bool(key) and not key.startswith("your_")


def _is_termux() -> bool:
    return bool(
        shutil.which("termux-media-player")
        or os.environ.get("TERMUX_VERSION")
        or os.path.isdir("/data/data/com.termux/files/home")
    )


def _is_ios_ashell() -> bool:
    """aShell / iOS Python — no Termux mic; Fish uses native `play` command."""
    forced = os.getenv("VERITY_MIC_BACKEND", "").strip().lower()
    if forced in ("ios", "ashell"):
        return True
    if forced in ("termux", "android", "windows", "sounddevice", "pc"):
        return False
    home = os.path.expanduser("~")
    return bool(
        shutil.which("play")
        or os.environ.get("ASHELL")
        or os.path.isdir("/var/mobile")
        or "ish" in home.lower()
    )


def _play_ios_pyto_sound(abs_path: str, duration: float) -> bool:
    """Pyto sound module (bundled with some aShell Python builds)."""
    try:
        import sound  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        player = sound.AudioPlayer(abs_path)
        player.play()
        deadline = time.monotonic() + max(duration + 2.0, 4.0)
        while player.playing and time.monotonic() < deadline:
            time.sleep(0.08)
        if player.playing:
            player.stop()
        print("[fish] playback done (pyto sound)", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[fish] pyto sound failed: {exc}", flush=True)
        return False


def _play_ios(path: str) -> bool:
    abs_path = os.path.abspath(path)
    duration = audio_duration_sec(abs_path)

    # a-Shell native player (AVFoundation) — NOT macOS afplay.
    if shutil.which("play"):
        print(f"[fish] playing via aShell play ({duration:.1f}s)…", flush=True)
        proc = subprocess.run(
            ["play", abs_path],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            time.sleep(duration + 0.25)
            print("[fish] playback done", flush=True)
            return True
        err = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        print(f"[fish] play failed: {err or proc.returncode}", flush=True)

    if _play_ios_pyto_sound(abs_path, duration):
        return True

    if shutil.which("ffplay"):
        print(f"[fish] playing via ffplay ({duration:.1f}s)…", flush=True)
        proc = subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", abs_path],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print("[fish] playback done", flush=True)
            return True
        err = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        print(f"[fish] ffplay failed: {err or proc.returncode}", flush=True)

    print("[fish] no iOS audio player found (need aShell `play` or pkg install ffmpeg)", flush=True)
    return False


def _android_audio_dir() -> str:
    """Shared path Termux:API / Android MediaPlayer can actually open."""
    candidates = [
        os.path.join(os.path.expanduser("~"), "storage", "downloads", "verity-tts"),
        os.path.join(os.path.expanduser("~"), "storage", "shared", "Download", "verity-tts"),
        "/storage/emulated/0/Download/verity-tts",
        "/sdcard/Download/verity-tts",
        os.path.join(os.path.expanduser("~"), "verity-tts"),
    ]
    for folder in candidates:
        try:
            os.makedirs(folder, exist_ok=True)
            probe = os.path.join(folder, ".write_test")
            with open(probe, "wb") as f:
                f.write(b"ok")
            os.remove(probe)
            return folder
        except OSError:
            continue
    return tempfile.gettempdir()


def _tts_dirs() -> list[str]:
    dirs = [tempfile.gettempdir()]
    if _is_termux():
        dirs.insert(0, _android_audio_dir())
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _safe_unlink(path: Optional[str], *, retries: int = 8, delay: float = 0.25) -> None:
    if not path:
        return
    for i in range(retries):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except OSError as err:
            if i + 1 >= retries:
                print(f"[fish] could not delete {path}: {err}", flush=True)
                return
            time.sleep(delay)


def purge_old_tts_files(*, keep: Optional[str] = None) -> int:
    """Delete leftover verity TTS clips from temp + Android download folders."""
    removed = 0
    keep_abs = os.path.abspath(keep) if keep else None
    patterns = ("verity_*.mp3", "verity_*.wav", "verity_*.opus", "verity_tts_current.*")
    for folder in _tts_dirs():
        for pattern in patterns:
            for path in glob.glob(os.path.join(folder, pattern)):
                if keep_abs and os.path.abspath(path) == keep_abs:
                    continue
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
    return removed


def _ensure_purged_once() -> None:
    global _purge_done
    if _purge_done:
        return
    _purge_done = True
    n = purge_old_tts_files()
    if n:
        print(f"[fish] purged {n} leftover TTS file(s)", flush=True)


def strip_fish_tags(text: str) -> str:
    """Remove Fish control tags so chat never shows [whispering] etc."""
    return re.sub(r"\s{2,}", " ", _FISH_TAG.sub("", _strip_mc_formatting(text))).strip()


def _add_prosody_hints(text: str) -> str:
    """Light mid-line emphasis / pause cues for Fish."""
    out = text
    # Pause on ellipsis
    out = out.replace("...", " [break] ...")
    # Emphasize ALL-CAPS words (2+ letters)
    out = re.sub(r"\b([A-Z]{2,})\b", r"[emphasis] \1", out)
    # Soft emphasis before ! sentences if not already tagged
    if "!" in out and "[angry]" not in out and "[excited]" not in out:
        out = re.sub(r"(^|[.!?]\s+)([^[\].!?]+!)", r"\1[emphasis] \2", out, count=1)
    return re.sub(r"\s{2,}", " ", out).strip()


def wrap_fish_delivery(text: str, style: str = "normal") -> str:
    """
    Build TTS input with Fish S2.1 emotion/delivery tags.
    Chat should use strip_fish_tags(text) — never show these brackets in-game.
    """
    clean = strip_fish_tags(text)
    if not clean:
        return clean
    key = str(style or "normal").strip().lower()
    prefix = _STYLE_PREFIX.get(key, "")
    body = _add_prosody_hints(clean)
    return f"{prefix}{body}".strip()


def resolve_fish_style(
    *,
    phase: int = 1,
    intent: str = "",
    draft: str = "",
) -> str:
    """Pick a delivery style from addon phase + intent."""
    intent_l = str(intent or "").strip().lower()
    draft_l = str(draft or "").lower()

    if intent_l in ("scold", "insult") or "scold" in draft_l:
        return "scold"
    if intent_l in ("angry",) or any(
        w in draft_l for w in ("furious", "angry", "mad at")
    ):
        return "angry"
    if intent_l in ("mercy", "chase_mercy", "apology") or draft_l in (
        "sorry",
        "mercy_sorry",
    ):
        return "apology"
    if intent_l in ("thatmob", "tmob") or draft_l in ("miss", "i_miss_him"):
        return "sad"
    if intent_l in ("secret_who", "hide") or draft_l in ("hide", "secret"):
        return "phase2" if phase < 3 else "phase3"

    if phase >= 4:
        return "phase4"
    if phase >= 3:
        return "phase3"
    if phase >= 2:
        return "phase2"
    return "phase1"


def synthesize_fish_audio(
    text: str,
    out_path: Optional[str] = None,
    *,
    fmt: Optional[str] = None,
    style: str = "normal",
) -> str:
    api_key = os.getenv("FISH_API_KEY", "").strip()
    if not api_key or api_key.startswith("your_"):
        raise RuntimeError("Set FISH_API_KEY in .env (https://fish.audio)")

    reference_id = (
        os.getenv("FISH_REFERENCE_ID", "").strip()
    )
    fish_model = os.getenv("FISH_TTS_MODEL", "").strip() or DEFAULT_FISH_MODEL
    clean = wrap_fish_delivery(text, style=style)
    if not clean:
        raise RuntimeError("Empty text for Fish TTS")

    # MP3 on Termux — Android MediaPlayer is unreliable with Fish WAV in private tmp.
    if fmt is None:
        fmt = os.getenv("FISH_TTS_FORMAT", "").strip().lower()
        if not fmt:
            fmt = "mp3" if _is_termux() else "wav"
    if fmt not in ("mp3", "wav", "opus"):
        fmt = "mp3"

    if out_path is None:
        if _is_termux():
            out_path = os.path.join(_android_audio_dir(), f"verity_tts_current.{fmt}")
        else:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}")
            out_path = tmp.name
            tmp.close()

    print(
        f"[fish] TTS model={fish_model} voice={reference_id[:8]}… "
        f"style={style} fmt={fmt} text={clean[:80]!r}",
        flush=True,
    )

    resp = httpx.post(
        FISH_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "model": fish_model,
        },
        json={
            "text": clean,
            "reference_id": reference_id,
            "format": fmt,
            "latency": "balanced",
            "normalize": True,
        },
        timeout=90.0,
    )
    if resp.status_code >= 400:
        detail = resp.text[:300]
        if resp.status_code == 402:
            raise RuntimeError(
                "Fish TTS: out of API credit. Top up at "
                "https://fish.audio/app/developers"
            )
        raise RuntimeError(f"Fish TTS HTTP {resp.status_code}: {detail}")

    with open(out_path, "wb") as f:
        f.write(resp.content)

    print(f"[fish] {fmt} {len(resp.content)} bytes → {out_path}", flush=True)
    return out_path


def synthesize_fish_wav(text: str, out_path: Optional[str] = None) -> str:
    return synthesize_fish_audio(text, out_path)


def audio_duration_sec(path: str) -> float:
    """Accurate-ish duration of a synthesized clip (seconds)."""
    lower = path.lower()

    if lower.endswith(".wav"):
        try:
            with wave.open(path, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate() or 24000
                if frames > 0 and rate > 0:
                    return max(0.35, min(60.0, frames / float(rate)))
        except Exception:  # noqa: BLE001
            pass

    # Prefer real decoders over byte-size guesses (MP3 bitrate varies a lot).
    try:
        from mutagen import File as MutagenFile  # type: ignore

        meta = MutagenFile(path)
        if meta is not None and getattr(meta, "info", None) is not None:
            length = float(getattr(meta.info, "length", 0) or 0)
            if length > 0:
                return max(0.35, min(60.0, length))
    except Exception:  # noqa: BLE001
        pass

    if shutil.which("ffprobe"):
        try:
            proc = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
            length = float((proc.stdout or "").strip() or 0)
            if length > 0:
                return max(0.35, min(60.0, length))
        except Exception:  # noqa: BLE001
            pass

    try:
        size = os.path.getsize(path)
    except OSError:
        return 2.5

    # Last resort: assume ~192 kbps for Fish MP3 (size/16000 overestimates badly).
    if lower.endswith(".mp3"):
        return max(0.35, min(60.0, size / 24000.0))
    if lower.endswith(".opus"):
        return max(0.35, min(60.0, size / 16000.0))
    return max(0.35, min(60.0, size / 48000.0))


def mouth_hold_ticks(duration_sec: float) -> int:
    """Unused by mouth anim (scripts use 1 tick per character). Kept for callers. """
    sec = max(0.35, float(duration_sec or 0))
    ticks = int(round(sec * 20 * 0.92))
    return max(6, min(1200, ticks))


def _guess_duration_sec(path: str) -> float:
    return audio_duration_sec(path)


def _play_termux(path: str) -> bool:
    abs_path = os.path.abspath(path)
    duration = audio_duration_sec(abs_path)

    if shutil.which("play-audio"):
        print(f"[fish] playing via play-audio ({duration:.1f}s)…", flush=True)
        proc = subprocess.run(
            ["play-audio", abs_path],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print("[fish] playback done", flush=True)
            return True
        err = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        print(f"[fish] play-audio failed: {err or proc.returncode}", flush=True)

    if shutil.which("termux-media-player"):
        print(
            f"[fish] playing via termux-media-player ({duration:.1f}s)…",
            flush=True,
        )
        start = subprocess.run(
            ["termux-media-player", "play", abs_path],
            check=False,
            capture_output=True,
            text=True,
        )
        out = ((start.stdout or "") + "\n" + (start.stderr or "")).strip()
        if out:
            print(f"[fish] media-player: {out}", flush=True)
        if "Prepare failed" in out or "Unable to open" in out or start.returncode != 0:
            print(
                "[fish] tip: pkg install play-audio && termux-setup-storage",
                flush=True,
            )
            return False
        time.sleep(duration + 0.4)
        subprocess.run(
            ["termux-media-player", "stop"],
            check=False,
            capture_output=True,
        )
        time.sleep(0.35)
        print("[fish] playback done", flush=True)
        return True

    if shutil.which("ffplay"):
        print("[fish] playing via ffplay…", flush=True)
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", abs_path],
            check=False,
        )
        print("[fish] playback done", flush=True)
        return True

    print("[fish] no Android audio player found", flush=True)
    return False


def play_wav_file(path: str) -> None:
    if _is_termux():
        if not _play_termux(path):
            raise RuntimeError(
                "Android playback failed (Prepare failed / no player). "
                "Fix: pkg install play-audio && termux-setup-storage"
            )
        return

    if _is_ios_ashell():
        if not _play_ios(path):
            raise RuntimeError(
                "iOS playback failed — aShell `play` or ffplay could not play Fish audio. "
                "Chat still works. Fix: update setup, or run `pkg install ffmpeg` in aShell."
            )
        return

    import numpy as np
    import sounddevice as sd

    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())

    if sample_width == 2:
        dtype = np.int16
    elif sample_width == 4:
        dtype = np.int32
    else:
        raise RuntimeError(f"Unsupported sample width: {sample_width}")

    audio = np.frombuffer(frames, dtype=dtype)
    if channels > 1:
        audio = audio.reshape(-1, channels)

    print(f"[fish] playing {sample_rate}Hz ch={channels}…", flush=True)
    sd.play(audio, sample_rate)
    sd.wait()
    print("[fish] playback done", flush=True)


def prepare_fish(
    text: str,
    *,
    style: str = "normal",
) -> tuple[Optional[str], float]:
    """
    Download TTS only. Returns (path, duration_sec).
    Caller must play then delete, or call finish_fish(path).
    """
    if not fish_tts_configured():
        print("[fish] skip — FISH_API_KEY not set", flush=True)
        return None, 0.0

    _ensure_purged_once()
    path = synthesize_fish_audio(text, style=style)
    duration = audio_duration_sec(path)
    print(f"[fish] ready duration={duration:.2f}s style={style}", flush=True)
    return path, duration


def finish_fish(path: Optional[str]) -> None:
    _safe_unlink(path)
    n = purge_old_tts_files()
    if n:
        print(f"[fish] cleaned {n} leftover TTS file(s)", flush=True)


def speak_fish(text: str, *, style: str = "normal") -> float:
    """Synthesize + play. Returns audio duration seconds (0 if skipped/failed)."""
    path = None
    try:
        path, duration = prepare_fish(text, style=style)
        if not path:
            return 0.0
        play_wav_file(path)
        return duration
    except Exception as exc:  # noqa: BLE001
        print(f"[fish] FAILED: {exc}", flush=True)
        return 0.0
    finally:
        finish_fish(path)
