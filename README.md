# tracklist

Builds a timestamped tracklist for a YouTube video (a DJ set, a mix, a radio show) from YouTube's Content ID claims, and fills the gaps with Shazam.

```
Sunday Session — Live Mix
https://www.youtube.com/watch?v=XXXXXXXXXXX

  - 00:00 Artist One - First Track
  - 04:12 Artist Two - Second Track (?)
  - 09:30 ????? (unidentified, until 12:05)
  - 12:05 Artist Three - Third Track

2 identified, 1 suggested, 1 gaps (02:35 unidentified of 15:40)
```

## How it works

1. **Content ID claims.** When a video contains copyrighted music, YouTube's Content ID attaches a claim to it with the song, artist, label and the exact position in the video. tracklist reads these claims from YouTube Studio using your browser's YouTube session, so you never have to copy cookies or a cURL command by hand.
2. **Your video or someone else's.** Studio only shows claims for videos on your own channel:
   - **Your video:** the claims are read straight away.
   - **Someone else's video:** tracklist downloads the audio with `yt-dlp`, uploads it as a **private** video to your channel through the YouTube Data API, waits for processing and for Content ID to finish (up to an hour), and then reads the claims of that copy.
   - **A local audio file:** it is converted to a video with `ffmpeg` and uploaded the same way.
3. **Shazam for the gaps.** Any stretch of 90 seconds or more without a claim is sampled every 90 seconds (10-second clips) and recognised with Shazam. Shazam matches are marked with `(?)`, merged with neighbouring samples of the same track, and dropped when they repeat a claimed track. Whatever stays unidentified is listed as `?????` with its time range.
4. **Fallback.** For a YouTube URL, if nothing can be uploaded (upload quota used up, no Google login, download failed, no Studio session), tracklist scans the whole video with Shazam alone. A local file has no video to fall back on, so with the quota used up tracklist stops.
5. **Cleanup.** At the end it offers to delete the private copy it uploaded.

Progress is cached, so an interrupted analysis resumes where it stopped.

## Install

Requires macOS with Homebrew.

```bash
brew install yt-dlp ffmpeg uv
uv tool install git+https://github.com/vigosan/tracklist
```

To update: `uv tool install --force git+https://github.com/vigosan/tracklist`.

## First run

You need a YouTube channel on your Google account: Content ID claims and uploads live there.

1. Sign in to YouTube in your browser. tracklist detects which browser has the session and remembers it; to pick one yourself use `--browser` (chrome, brave, edge, firefox, safari, vivaldi, opera, chromium). Safari needs "Full Disk Access" for your terminal app (Terminal, iTerm, Ghostty...) in System Settings > Privacy & Security.
2. macOS asks for your keychain password so tracklist can read the browser's cookies: choose "Always Allow" so it doesn't ask on every run.
3. A wizard walks you through creating your own Google credential: a Google Cloud project with the YouTube Data API v3 enabled and an OAuth client of type "Desktop app".
4. Your browser opens so you can sign in with Google and authorise access. The permission Google asks for (`youtube`) gives tracklist control of your channel; it only uploads private copies and deletes the copies it uploaded.

## Usage

```bash
tracklist https://www.youtube.com/watch?v=XXXXXXXXXXX   # your own video
tracklist https://www.youtube.com/watch?v=YYYYYYYYYYY   # someone else's video
tracklist set.mp3                                        # a local audio file
tracklist <URL> --browser brave                          # choose the browser
tracklist <URL> --fresh                                  # ignore any previous analysis
tracklist <URL> --label                                  # show each track's record label
```

Other options: `--no-recognize` (skip Shazam), `--from-json FILE` (use a saved claims response), `--duration SECONDS` (total length, for the final gap).

Content ID can take a while to report claims on a new upload. You can quit during that wait and run the same command later: it resumes from the existing upload and never waits twice unless you pass `--fresh`. The terminal UI shows claims as they arrive, and a macOS notification tells you when the tracklist is ready.

While it runs, a terminal UI shows the tracklist as it grows:

| Key | Action |
|-----|--------|
| `p` | pause / resume Shazam |
| `r` | refresh Content ID claims |
| `n` | analyse another video |
| `x` | discard Shazam suggestions and start over (confirm with `y`) |
| `c` | copy the tracklist |
| `q` | quit |

## Language

tracklist speaks English or Spanish, following your system's locale (`LC_ALL`, `LC_MESSAGES`, then `LANG`). A locale starting with `es` gets Spanish, any other gets English. To switch for one run:

```bash
LC_ALL=es_ES.UTF-8 tracklist <URL>
```

In Spanish, yes/no questions and the TUI's confirm key use `s` instead of `y`.

## Limits

- About 6 uploads per day: the API quota belongs to your own Google project.
- Content ID takes up to an hour to review an upload.
- Other people's videos are uploaded as private, and you are offered to delete them when done.

## Stored data

- `~/.config/tracklist`: chosen browser, Google credential and login, analysis cache.
- `~/Library/Caches/tracklist`: downloaded audio, so an analysis can resume.
