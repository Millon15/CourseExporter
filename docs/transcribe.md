# Transcription with pipeline D2

Every published course video gets a transcript that UniversalCourseHost reads beside the video on Drive.

## Shape

- The VPS is the source of truth. `python3 -m course_exporter transcribe` there owns the catalog, the Drive reads,
  the audio preparation, the journal, validation, conversion and publishing.
- The Mac only computes. It runs pipeline D2: Parakeet-TDT-0.6B-v3 (MLX) for words, Nemotron 3 Diarization for
  speakers, about 1.8 min of compute per audio hour at a 2.1 GB peak. It never holds a video, a Drive credential or a
  destination id.
- The VPS cannot reach the Mac, so the Mac starts every exchange over ssh with a restricted key. The key's forced
  command allows six verbs: `request`, `lease`, `audio <lease>`, `renew <lease>`, `submit <lease>`, `status`.

| Step | Where | What |
| --- | --- | --- |
| 1 | VPS | `request` queues one published video revision; the prepare unit writes its 16 kHz mono Opus |
| 2 | Mac | `lease` takes one ready item; `audio` streams its Opus into scratch; the SHA-256 must match |
| 3 | Mac | D2 runs while the worker renews the lease every 120 s |
| 4 | Mac | `submit` sends the `uch-segments/1` result; an identical resubmit after a lost reply gets the same receipt |
| 5 | VPS | validation, conversion (`transcribe/adapter.py`) and publishing next to the MP4; the selection file is last |

## Mac setup, once

1. Install the D2 environment and the pinned models:

   ```sh
   uv sync --frozen --project tools/transcribe-d2
   uv run --frozen --project tools/transcribe-d2 python -m course_exporter.transcribe.d2 --fetch
   ```

2. Create the restricted key and give its public half to the VPS operator, who adds it to `openclaw`'s
   `authorized_keys` as `restrict,command="/home/openclaw/.local/bin/course-transcribe-ssh" <key>`:

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/jarvis_transcribe_ed25519 -C course-transcribe
   ```

3. Add the alias to `~/.ssh/config`, with the same `HostName`, `Port` and `ProxyCommand` as the operator's own VPS
   alias:

   ```text
   Host jarvis-transcribe
     User openclaw
     IdentityFile ~/.ssh/jarvis_transcribe_ed25519
     IdentitiesOnly yes
   ```

   `IdentitiesOnly yes` keeps the worker off every other key, so it can never reach the operator's login.

## Run

```sh
uv run --frozen --project tools/transcribe-d2 python -m course_exporter.transcribe.mac_worker          # until the queue is empty
uv run --frozen --project tools/transcribe-d2 python -m course_exporter.transcribe.mac_worker --once   # one item
ssh jarvis-transcribe status                                                                           # journal counts
```

| Exit | Meaning |
| --- | --- |
| 0 | the queue is empty or deferred (maintenance window, fleet lock, staging floor) |
| 1 | an item stopped: audio mismatch, lease fenced, result refused; the reason is on stderr |
| 3 | a pinned model is missing: run `--fetch` |
| 75 | another worker holds `~/Library/Caches/course-transcribe/.lock` |

Scratch is `~/Library/Caches/course-transcribe/` (0700). It holds one item at most: `item.opus` (about 4 MB per
audio hour), `item.lease.json` and `item.result.json`. The worker deletes them after the VPS confirms the receipt.
A result whose submit reply was lost stays there and goes out again, unchanged, on the next start.

## Hourly on the Mac (opt-in)

It stays off until the operator decides to turn it on. It runs hourly while the Mac is awake, and the VPS's daily
alert covers the gaps.

```sh
sed -e "s|@UV@|$(command -v uv)|" -e "s|@REPO@|$PWD|" -e "s|@HOME@|$HOME|" \
    -e "s|@FFMPEG_DIR@|$(dirname "$(command -v ffmpeg)")|" \
    launchd/com.millon.course-transcribe.plist.in > ~/Library/LaunchAgents/com.millon.course-transcribe.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.millon.course-transcribe.plist
launchctl print "gui/$(id -u)/com.millon.course-transcribe" | grep -E 'state|last exit'   # status
tail -n 50 ~/Library/Logs/course-transcribe.log
launchctl bootout "gui/$(id -u)/com.millon.course-transcribe"                             # off again
```

## Pins

`course_exporter/transcribe/d2_pins.py` holds the model revisions and the producer config string. A change there
changes `CONFIG8`, so the published names change and nothing collides with an older run. The VPS refuses a result
whose config differs from the lease.
