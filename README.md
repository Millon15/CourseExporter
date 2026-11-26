# course-exporter

Exports courses you bought on Kwiga and GetCourse into an offline archive: lesson text as HTML, videos as MP4, a
course index. Lessons are read through your own logged-in Chrome, reached as an OpenClaw node (`browser.proxy`);
media downloads run with yt-dlp and ffmpeg on a Linux host and finished lessons move to Google Drive with rclone.
It skips DRM-protected or encrypted media and never submits quizzes, homework or comments. For personal offline
study only.

## Setup

1. Copy `config.example.toml` to `~/.config/course-exporter/config.toml` and fill in your node, rclone remote and
   staging directory. That file names real machines and paths, so it stays out of git.
2. Configure an rclone remote with `scope = drive.file`; its token lives in rclone's own config, never here.

## Run

```sh
python3 -m course_exporter export --provider kwiga --course <course URL> --name <folder> \
  --out <staging_root>/<folder> --first-video --decode        # pilot: first verified video
python3 -m course_exporter export --provider kwiga --course <course URL> --name <folder> --out <staging_root>/<folder>
python3 -m course_exporter status --out <staging_root>/<folder>
```

Exit codes: 0 complete, 1 incomplete (rerun resumes), 2 usage, 3 another export holds the folder, 5 complete with
gaps (gated lessons or unsupported assets). A run stops with exit 1 when staging has no room for an hour, when the disk
floor or staging cap is reached mid-download, or when three lessons in a row fail.

For a full course, `scripts/export-loop.sh` reruns the export with `--rediscover` until it ends with anything but exit
1, waiting `RETRY_SECONDS` (600) between attempts and giving up after `MAX_IDLE` (6) attempts in a row that publish no
new lesson. Under systemd it survives the login session:

```sh
systemd-run --user --unit course-<folder> scripts/export-loop.sh <staging_root>/<folder>.log \
  --provider kwiga --course <course URL> --name <folder> --out <staging_root>/<folder> --decode
```

Test: `python3 -m unittest discover -s tests -t .`
