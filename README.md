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
unsupported assets.

Test: `python3 -m unittest discover -s tests -t .`
