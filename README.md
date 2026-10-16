# Random Looper

Play an audio file forever with **randomized, crossfaded loop points**, and optionally render an extended copy of it to any length you like.

Every time the audio loops, the script picks a brand-new random "jump from" point and "jump to" point, then hides the jump with a crossfade. Because the points change on every loop, the result never repeats in the same way twice.

> ## ⚠️ Read this first: this is for atmosphere, not music
>
> This kind of looping only really works for **ambient / atmospheric audio**: rain, wind, ocean waves, forest or jungle ambience, a crackling fire, room tone, crowd murmur, an engine hum, a synth drone, and similar steady textures.
>
> It will **not** work well on **music**. Music has rhythm, melody, harmony and structure. Jumping between two random points means:
>
> - the beat lands in the wrong place, so the tempo feels like it stumbles,
> - the chords or key change abruptly or clash,
> - phrases, lyrics and builds get cut off or repeated mid-thought,
> - a crossfade between two unrelated musical passages just sounds like two songs playing over each other (muddy, phasing, "mashup" sound).
>
> Atmosphere survives this because it has no beat, melody or story. Any 10-second slice sounds much like any other, so a smooth blend between two random slices is nearly invisible.
>
> Even with atmosphere, **distinct one-off sounds** (a single bird call, a thunderclap, a door slam, a voice) can end up cut in half, doubled, or repeated suspiciously often. Use `--exclude` (below) to keep loop points away from those moments.

---

## Install

From PyPI:

```Shell
pip install random-looper
```

From this repo (editable, for development):

```Shell
pip install -e .
```

Once installed you get a `random-looper` command:

```Shell
random-looper rain.wav
random-looper ocean.ogg -e 10:00 -o ocean_10min.wav
```

Or run it as a module:

```Shell
python -m random_looper rain.wav
```

## Manual Install

Requires Python 3.8+.

```Shell
pip install numpy soundfile sounddevice
```

- `numpy` and `soundfile` are needed for everything.
- `sounddevice` is only needed for live playback. If you only use `--no-play`, you can skip it.
- WAV, FLAC and OGG work out of the box. MP3 needs libsndfile 1.1 or newer, which recent `soundfile` wheels include.

Or you can install from `requirements.txt` using `pip install -r requirments.txt`.

If you're on Linux, you also need the PortAudio system library for sounddevice to work, use `sudo apt install libportaudio2` to install it if you haven't already.

---

## Quick start

```Shell
python random_looper.py rain.wav
```

Play `rain.wav` forever with random loop points. Press **Ctrl+C** to stop.

```Shell
python random_looper.py                      # (it asks you for the file path)
```

```Shell
python random_looper.py forest.flac -x "[(0,15),(60,75.5)]"
```

Same, but never choose a loop point in 0-15s or 60-75.5s (for example, to avoid an intro or a loud bird call).

```Shell
python random_looper.py ocean.ogg -e 10:00 -o ocean_10min.wav
```

Write a 10-minute version to `ocean_10min.wav`, then play the original with random loops.

```Shell
python random_looper.py wind.mp3 -e 1:00:00 --no-play
```

Just write a one-hour file and don't play anything.

---

## Arguments

| Argument                         | Description                                                                                                                                                                                          | Default                                        |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `audio`                        | Path to the audio file. Optional: if you leave it out, the script prompts you for it. Quotes around the path are fine.                                                                               | prompt                                         |
| `-x`, `--exclude RANGES`     | Time ranges, in **seconds**, that loop points may not land in. See [Exclusion ranges](#exclusion-ranges).                                                                                       | none                                           |
| `-e`, `--extend LENGTH`      | Also write an extended copy of the audio of exactly this length. Accepts seconds (`90`, `90.5`) or `mm:ss` / `h:mm:ss` (`10:00`, `1:30:00`).                                             | off                                            |
| `-o`, `--output PATH`        | Where to write the extended file. The format comes from the extension (`.wav`, `.flac`, `.ogg`...). Only used with `--extend`.                                                               | `<name>_extended.wav` next to the input file |
| `--no-play`                    | Don't play anything. Useful with `--extend` to just make the file.                                                                                                                                 | off (plays)                                    |
| `--no-log`                     | Don't print the startup info line or the loop points while playing.                                                                                                                                  | off (prints)                                   |
| `-c`, `--crossfade SECONDS`  | Length of the crossfade used to hide each jump.                                                                                                                                                      | `2.0`                                        |
| `-m`, `--min-length SECONDS` | Minimum amount of audio **played before each jump** and **inside each loop** (the distance between the jump-to and jump-from points). Larger values mean longer stretches between jumps. | `10.0`                                       |
| `--fade-out SECONDS`           | Length of the fade-out at the very end of the extended file. Capped at a quarter of the extended length. Only used with `--extend`.                                                                | `4.0`                                        |
| `--seed N`                     | Seed the random number generator so you get the same sequence of loop points every run.                                                                                                              | random                                         |

### Automatic adjustments for short files

So that short files don't break the algorithm, the script clamps your settings:

- crossfade is limited to 1/8 of the file length (and is at least 0.05 s),
- min-length is limited to 1/3 of the file length, but never less than twice the crossfade.

If it changes your values, it prints the values it used.

### Exclusion ranges

`-x` takes a Python-style list of `(start, end)` pairs in seconds:

```Shell
-x "[(0, 15), (60, 75.5)]"
```

- Wrap it in quotes so your shell/command prompt doesn't mangle the brackets and commas.
- A single pair works too: `-x "(0,15)"`.
- Overlapping ranges are merged, and a pair given backwards (`(20,10)`) is flipped.
- The ranges apply to **both** loop points: the point it jumps **from** and the point it jumps **to**. Neither will ever be picked inside an excluded range.
- Playback itself can still pass through excluded areas on its way. They are only off-limits as loop points.
- The crossfade window sits just *before* each loop point, so a crossfade can still overlap the edge of an excluded range. If that matters, make the range a little bigger.
- If your exclusions leave too little room for a loop (for example, you excluded almost the whole file), the script stops with an error telling you to shrink them or lower `--min-length` / `--crossfade`.

---

## How it works

1. **Load.** The file is decoded into memory as 32-bit float audio.
2. **Plan a jump.** For every loop the script picks a random pair of points, **B** (where it jumps *from*) and **A** (where it jumps *to*), with A earlier than B. The picks are uniform over all the allowed time (everything not excluded), and always satisfy:
   - neither point is inside an excluded range,
   - at least `--min-length` seconds play before the jump,
   - at least `--min-length` seconds sit between A and B,
   - A is at least one crossfade length into the file (there has to be audio before it to fade in),
   - a valid next jump always exists, so the script can never paint itself into a corner.
3. **Crossfade.** The audio just before B (the last `--crossfade` seconds) fades out while the audio just before A fades in, using an **equal-power** curve so the volume stays steady through the blend. When the crossfade finishes, playback continues from A as if nothing happened.
4. **Repeat.** Playback continues to the next randomly chosen B, jumps back to a new random A, and so on forever. The first pass starts at the beginning of the file.

### Playback

A background thread prepares audio in pieces (the normal stretch, then the crossfaded jump, and so on) and feeds a small queue. The sound card callback just copies from that queue, so the heavy lifting never happens in the audio thread. Each time a jump starts, a line like this is printed (unless `--no-log` is set):

```
  loop: 2:14.30 -> 0:45.20
```

That means "jumped from 2:14.30 back to 0:45.20".

### Extended file

With `--extend`, the same jump logic is run offline and streamed straight to disk, so even very long outputs don't need to fit in memory. The file is trimmed to exactly the length you asked for and ends with a fade-out (`--fade-out`). A progress percentage is shown while it renders. After rendering, the script plays the original unless you pass `--no-play`.

Output is 16-bit PCM for WAV by default. Samples are clipped to the valid range, since an equal-power crossfade of very similar loud audio can briefly exceed full scale.

---

## Tips for good results

- Pick **steady, texture-like** recordings with no obvious beginning, end or repeating pattern.
- If the source has a fade-in, fade-out, or a distinct event you don't want repeated or chopped, cover it with `--exclude`.
- Longer `--crossfade` (3-6 s) hides jumps better on very smooth sounds like drones. Shorter is fine for noisy textures like rain.
- A larger `--min-length` makes the loop feel slower and more natural; a smaller one means more frequent (but still smooth) jumps.
- Use `--seed` if you find a render you like and want to reproduce it.
- Very short source files give the randomizer little to work with, and repetition becomes easy to notice. Longer recordings (a minute or more) work best.

---

## Troubleshooting

| Problem                                           | Fix                                                                                                       |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `Playback needs sounddevice`                    | `pip install sounddevice`                                                                               |
| `File not found`                                | Check the path. Put quotes around paths with spaces.                                                      |
| `Could not read audio file`                     | The format may not be supported by your libsndfile build. Convert the file to WAV or FLAC.                |
| `Could not parse --exclude`                     | Use the form `"[(10, 20), (45.5, 60)]"`, with the quotes.                                               |
| `The exclusion ranges leave too little room...` | Shrink the exclusions, or lower `--min-length` / `--crossfade`.                                       |
| Audible clicks or stutters in playback            | Your machine may be too busy to keep the audio queue filled. Close other heavy programs.                  |
| Jumps sound obvious                               | The source is probably not stationary enough. See the warning at the top about music and distinct sounds. |
