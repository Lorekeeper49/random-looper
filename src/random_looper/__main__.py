#!/usr/bin/env python3
"""
random_looper.py - endlessly play an audio file with randomized, crossfaded loop points.

Every loop, a new (loop_from, loop_to) pair is chosen at random. The jump is hidden by an
equal-power crossfade: the last moments before `loop_from` fade out while the moments just
before `loop_to` fade in, then playback carries on from `loop_to`.

Optionally:
  * exclude time ranges (seconds) from being chosen as loop points
  * render an extended copy of the audio to a desired length

Install:
    pip install numpy soundfile sounddevice

Examples:
    python random_looper.py song.wav
    python random_looper.py song.flac -x "[(0,15),(60,75.5)]"
    python random_looper.py song.ogg -e 10:00 -o song_10min.wav
    python random_looper.py song.mp3 -e 1:00:00 --no-play
    python random_looper.py                      (prompts for the file path)

Notes:
    * Exclusion ranges are in seconds and apply to BOTH loop points (the jump-from point
      and the jump-to point). The crossfade window sits just before each point.
    * Reading mp3 needs libsndfile >= 1.1 (bundled with recent `soundfile` wheels).
"""
import argparse
import ast
import queue
import random
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import soundfile as sf

EPS = 1e-6
Span = Tuple[float, float]


# --------------------------------------------------------------------------- helpers
def parse_time(text) -> float:
    """'90', '90.5', '1:30', '1:02:03' -> seconds."""
    total = 0.0
    for part in str(text).strip().split(":"):
        total = total * 60 + float(part)
    return total


def fmt(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:05.2f}"


def parse_exclusions(text: Optional[str]) -> List[Span]:
    """Parse "[(start, end), (start, end)]" (seconds) into merged, sorted spans."""
    if not text:
        return []
    try:
        val = ast.literal_eval(text)
        if (isinstance(val, tuple) and len(val) == 2
                and all(isinstance(v, (int, float)) for v in val)):
            val = [val]  # a single (start, end) pair
        spans = []
        for a, b in val:
            a, b = float(a), float(b)
            spans.append((min(a, b), max(a, b)))
    except (ValueError, SyntaxError, TypeError):
        sys.exit('Could not parse --exclude. Use the form: "[(10, 20), (45.5, 60)]"')
    spans.sort()
    merged: List[Span] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def allowed_intervals(lo: float, hi: float, excl: List[Span]) -> List[Span]:
    """[lo, hi] minus the excluded spans."""
    spans = [(lo, hi)] if hi - lo > EPS else []
    for ex_a, ex_b in excl:
        out = []
        for a, b in spans:
            if ex_b <= a or ex_a >= b:
                out.append((a, b))
                continue
            if ex_a > a:
                out.append((a, ex_a))
            if ex_b < b:
                out.append((ex_b, b))
        spans = [(a, b) for a, b in out if b - a > EPS]
    return spans


def clip_spans(spans: List[Span], lo: float, hi: float) -> List[Span]:
    out = []
    for a, b in spans:
        a2, b2 = max(a, lo), min(b, hi)
        if b2 - a2 > EPS:
            out.append((a2, b2))
    return out


def sample_uniform(spans: List[Span], rng: random.Random) -> float:
    total = sum(b - a for a, b in spans)
    r = rng.uniform(0, total)
    for a, b in spans:
        if r <= b - a:
            return a + r
        r -= b - a
    return spans[-1][1]


# --------------------------------------------------------------------------- loop logic
class LoopPlanner:
    """
    Picks random (loop_to, loop_from) pairs that always:
      * avoid the excluded ranges,
      * leave at least `min_len` seconds of playing before the jump and inside the loop,
      * leave room for a crossfade before the loop_to point,
      * never paint themselves into a corner (a valid next jump always exists).
    """

    def __init__(self, duration: float, crossfade: float, min_len: float,
                 excl: List[Span], rng: random.Random):
        self.cf, self.min_len, self.rng = crossfade, min_len, rng
        self.a_spans = allowed_intervals(crossfade, duration, excl)  # valid loop_to points
        self.b_spans = allowed_intervals(0.0, duration, excl)        # valid loop_from points
        if not self.a_spans or not self.b_spans:
            raise ValueError("The exclusion ranges cover the whole file.")
        self.a_min = self.a_spans[0][0]
        self.b_max = self.b_spans[-1][1]
        if self.b_max - self.a_min <= min_len + EPS:
            raise ValueError(
                "The exclusion ranges leave too little room for a loop "
                f"(need more than {min_len:.1f}s between the earliest and latest allowed points). "
                "Shrink the exclusions or lower --min-length / --crossfade.")

    def next_pair(self, pos: float) -> Tuple[float, float]:
        """Given current play position (s), return (loop_to A, loop_from B) with A < B."""
        lo = max(pos + self.min_len, self.a_min + self.min_len)
        b_spans = clip_spans(self.b_spans, lo, self.b_max)
        if not b_spans:
            raise RuntimeError("No valid loop point found.")
        b = sample_uniform(b_spans, self.rng)
        a_hi = min(b - self.min_len, self.b_max - self.min_len)
        a_spans = clip_spans(self.a_spans, self.cf, a_hi)
        if not a_spans:
            raise RuntimeError("No valid loop point found.")
        a = sample_uniform(a_spans, self.rng)
        return a, b


@dataclass
class Piece:
    data: np.ndarray          # (frames, channels) float32
    label: Optional[str] = None


def loop_stream(audio: np.ndarray, sr: int, planner: LoopPlanner, crossfade: float):
    """Infinite generator of Pieces: body, crossfaded jump, body, crossfaded jump, ..."""
    n = len(audio)
    c = max(1, int(round(crossfade * sr)))
    t = np.linspace(0.0, 1.0, c, dtype=np.float32)
    fade_out = np.cos(t * np.pi / 2)[:, None]   # equal-power curves
    fade_in = np.sin(t * np.pi / 2)[:, None]
    pos = 0
    while True:
        a_s, b_s = planner.next_pair(pos / sr)
        a = max(c, int(round(a_s * sr)))
        b = min(n, int(round(b_s * sr)))
        yield Piece(audio[pos:b - c])
        xfade = audio[b - c:b] * fade_out + audio[a - c:a] * fade_in
        yield Piece(np.clip(xfade, -1.0, 1.0).astype(np.float32),
                    f"loop: {fmt(b / sr)} -> {fmt(a / sr)}")
        pos = a


# --------------------------------------------------------------------------- extended file
def render_extended(audio, sr, planner, crossfade, target_sec, fade_sec, out_path: Path):
    """Stream an extended version of the audio to disk (exactly target_sec long)."""
    channels = audio.shape[1]
    target = int(round(target_sec * sr))
    fade_n = min(int(round(fade_sec * sr)), target // 4)
    body_limit = target - fade_n
    written = 0
    tail: List[np.ndarray] = []
    tail_len = 0
    loops = 0
    last_print = 0.0

    with sf.SoundFile(str(out_path), "w", samplerate=sr, channels=channels) as out:
        for piece in loop_stream(audio, sr, planner, crossfade):
            d = piece.data
            if piece.label:
                loops += 1
            if written < body_limit:
                take = min(len(d), body_limit - written)
                out.write(d[:take])
                written += take
                d = d[take:]
            if len(d):
                tail.append(d)
                tail_len += len(d)
            if written >= body_limit and tail_len >= fade_n:
                break
            now = time.time()
            if now - last_print > 0.5:
                pct = 100 * (written + tail_len) / target
                print(f"\r  rendering... {pct:5.1f}%", end="", flush=True)
                last_print = now
        if fade_n:
            end = np.concatenate(tail)[:fade_n]
            end = end * np.linspace(1.0, 0.0, fade_n, dtype=np.float32)[:, None]
            out.write(end)
    print(f"\r  rendered {fmt(target_sec)} with {loops} loop jumps -> {out_path}      ")


# --------------------------------------------------------------------------- playback
def play(audio, sr, planner, crossfade, log=True):
    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("Playback needs sounddevice:  pip install sounddevice")

    channels = audio.shape[1]
    q: "queue.Queue[Piece]" = queue.Queue(maxsize=4)
    logq: "queue.Queue[str]" = queue.Queue()
    stop = threading.Event()
    state = {"cur": None, "off": 0}

    def producer():
        for piece in loop_stream(audio, sr, planner, crossfade):
            while not stop.is_set():
                try:
                    q.put(piece, timeout=0.2)
                    break
                except queue.Full:
                    continue
            if stop.is_set():
                return

    def callback(outdata, frames, time_info, status):
        filled = 0
        while filled < frames:
            cur = state["cur"]
            if cur is None or state["off"] >= len(cur):
                try:
                    piece = q.get_nowait()
                except queue.Empty:
                    outdata[filled:] = 0
                    return
                state["cur"], state["off"] = piece.data, 0
                if piece.label:
                    logq.put(piece.label)
                continue
            take = min(frames - filled, len(cur) - state["off"])
            outdata[filled:filled + take] = cur[state["off"]:state["off"] + take]
            state["off"] += take
            filled += take

    threading.Thread(target=producer, daemon=True).start()
    print("Playing. Press Ctrl+C to stop.")
    try:
        with sd.OutputStream(samplerate=sr, channels=channels, dtype="float32",
                             callback=callback, blocksize=2048):
            while True:
                try:
                    msg = logq.get(timeout=0.2)
                    if log:
                        print(" ", msg)
                except queue.Empty:
                    pass
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop.set()


# --------------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Play audio with random crossfaded loop points.")
    p.add_argument("audio", nargs="?", help="audio file (prompted for if omitted)")
    p.add_argument("-x", "--exclude", metavar="RANGES",
                   help='seconds to exclude from loop points, e.g. "[(0,15),(60,75.5)]"')
    p.add_argument("-e", "--extend", metavar="LENGTH",
                   help="also write an extended file of this length (seconds, or mm:ss / h:mm:ss)")
    p.add_argument("-o", "--output", help="extended file path (default: <name>_extended.wav)")
    p.add_argument("--no-play", action="store_true", help="skip playback (just write the file)")
    p.add_argument("--no-log", action="store_true", help="skip logging loop points to stdout")
    p.add_argument("-c", "--crossfade", type=float, default=2.0,
                   help="crossfade length in seconds (default 2)")
    p.add_argument("-m", "--min-length", type=float, default=10.0,
                   help="minimum seconds of audio between jumps / inside a loop (default 10)")
    p.add_argument("--fade-out", type=float, default=4.0,
                   help="fade-out at the end of the extended file, seconds (default 4)")
    p.add_argument("--seed", type=int, help="random seed (for repeatable results)")
    args = p.parse_args()

    path = args.audio or input("Audio file path: ").strip().strip('"').strip("'")
    if not Path(path).is_file():
        sys.exit(f"File not found: {path}")

    try:
        audio, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Could not read audio file: {e}")
    duration = len(audio) / sr

    # Keep settings sane for short files.
    cf = max(0.05, min(args.crossfade, duration / 8))
    min_len = max(2 * cf, min(args.min_length, duration / 3))
    if (cf, min_len) != (args.crossfade, args.min_length):
        print(f"Adjusted for file length: crossfade={cf:.2f}s, min-length={min_len:.2f}s")

    excl = parse_exclusions(args.exclude)
    rng = random.Random(args.seed)
    try:
        planner = LoopPlanner(duration, cf, min_len, excl, rng)
    except ValueError as e:
        sys.exit(str(e))

    if not args.no_log:
        print(f"Loaded {Path(path).name}: {fmt(duration)}, {sr} Hz, {audio.shape[1]} ch"
              + (f", excluding {excl}" if excl else ""))

    if args.extend:
        target = parse_time(args.extend)
        if target <= 0:
            sys.exit("--extend must be positive.")
        out_path = Path(args.output) if args.output else \
            Path(path).with_name(Path(path).stem + "_extended.wav")
        render_extended(audio, sr, planner, cf, target, args.fade_out, out_path)

    if not args.no_play:
        # fresh planner so playback isn't affected by the render
        play(audio, sr, LoopPlanner(duration, cf, min_len, excl, rng), cf,
             log=not args.no_log)


if __name__ == "__main__":
    main()
