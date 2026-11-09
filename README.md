# tracklist

**English** · [Español](README.es.md)

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

![tracklist analysing a DJ set in the terminal](assets/screenshot.png)

## How it works

1. **Content ID claims.** When a video contains copyrighted music, YouTube's Content ID attaches a claim to it with the song, artist, label and the exact position in the video. tracklist reads these claims from YouTube Studio using your browser's YouTube session, so you never have to copy cookies or a cURL command by hand.
2. **Your video or someone else's.** Studio only shows claims for videos on your own channel:
   - **Your video:** the claims are read straight away.
   - **Someone else's video:** tracklist downloads the audio with `yt-dlp`, uploads it as a **private** video to your channel through the YouTube Data API, waits for processing and for Content ID to finish (up to an hour), and then reads the claims of that copy.
   - **A local audio file:** it is converted to a video with `ffmpeg` (the picture is the recording's waveform) and uploaded the same way.
3. **Shazam for the gaps.** Any stretch of 90 seconds or more without a claim is sampled every 90 seconds (10-second clips) and recognised with Shazam. Shazam matches are marked with `(?)`, merged with neighbouring samples of the same track, and dropped when they repeat a claimed track. Whatever stays unidentified is listed as `?????` with its time range.
4. **Fallback.** For a YouTube URL, if nothing can be uploaded (upload quota used up, no Google login, download failed, no Studio session), tracklist scans the whole video with Shazam alone. A local file has no video to fall back on, so with the quota used up tracklist stops.
5. **Cleanup.** At the end it offers to delete the private copy it uploaded.

Progress is cached, so an interrupted analysis resumes where it stopped.

## Install

**macOS.** Open Terminal, paste this line and press Enter. It installs Homebrew if you don't have it, then everything tracklist needs:

```bash
curl -fsSL https://raw.githubusercontent.com/vigosan/tracklist/main/install.sh | bash
```

If you already use Homebrew and prefer to do it by hand:

```bash
brew install yt-dlp ffmpeg uv
uv tool install git+https://github.com/vigosan/tracklist
```

To update, run `tracklist update`. Once a day, when you open tracklist, it checks GitHub for a newer version and prints a one-line reminder if there is one (it never updates by itself; offline, it skips the check).

**Windows and Linux** are supported but untested: see [Windows and Linux (experimental)](#windows-and-linux-experimental).

## Windows and Linux (experimental)

tracklist was written and tested on macOS only. Windows and Linux support exists in the code but **has never been run on those systems**. If you try it, follow the checklist below and report what happens: that is how it gets fixed.

### Install on Windows

1. Install the tools. In PowerShell:

   ```powershell
   winget install Git.Git astral-sh.uv yt-dlp.yt-dlp Gyan.FFmpeg
   ```

2. Close the terminal and open a new one, so the new tools are found. Then install tracklist:

   ```powershell
   uv tool install git+https://github.com/vigosan/tracklist
   uv tool update-shell
   ```

   Close and reopen the terminal once more. uv downloads Python 3.13 by itself if you don't have it.
3. Run `tracklist <URL>`. Use Windows Terminal rather than the old `cmd.exe` window: the terminal UI needs a modern terminal.

### Install on Linux

Install `yt-dlp`, `ffmpeg` and `uv` with your package manager or their own installers, then install tracklist:

```bash
sudo apt install ffmpeg wl-clipboard      # or: dnf / pacman; use xclip instead of wl-clipboard on X11
pipx install yt-dlp                       # distro packages of yt-dlp are often too old for YouTube
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/vigosan/tracklist
```

### What differs from macOS

- **Browser:** tracklist looks for each browser's profile folder instead of an app. Safari exists only on macOS.
  - On Windows, yt-dlp can fail to decrypt the cookies of recent Chrome, Edge and Brave. If you see "Could not read the cookies", use Firefox: `tracklist <URL> --browser firefox`.
  - On Linux, Chromium-based browsers keep their key in the desktop keyring (GNOME or KDE), which may ask for your password.
- **Clipboard:** Windows uses `clip`; Linux needs `wl-copy` (`wl-clipboard`) or `xclip`. Without one, tracklist prints "could not copy to the clipboard" and nothing else breaks.
- **Notification when the tracklist is ready:** macOS only. Elsewhere it is skipped.
- **Install script:** `install.sh` is for macOS. Use the steps above.
- **Where data is stored:** see [Stored data](#stored-data).

### Help us test

Go through this list on your system and note what works. Each step says what you should see.

1. `tracklist --help` prints the options. If not, the installation failed.
2. `tracklist <URL of a video on your channel>` finds your browser and its YouTube session, then (the first time only) walks you through the Google credential.
3. The credential wizard opens each Google page, and picks up the `client_secret*.json` you download into your system's downloads folder (even if it has another name, like `~/Descargas` on a Spanish Linux, or has been moved on Windows).
4. `tracklist <URL of someone else's video>` downloads it, uploads the private copy and reads its claims.
5. `tracklist "C:\Users\you\Music\Sesión de prueba.mp3"` (or a Linux path) with spaces and accents in the file name.
6. In the terminal UI the keys work and the screen draws correctly. Press `c`, paste somewhere and check that accents (é, ñ) and symbols arrive intact.
7. Press `q` during the download and run the same command again: it resumes instead of starting over.
8. A video whose title has emoji or non-Latin letters does not make tracklist crash.

To report a problem, open an issue on GitHub or message the maintainer with:

- your system and version (Windows 11, Ubuntu 24.04...) and the terminal you used
- the output of `uv tool list` and `yt-dlp --version`
- which browser you were signed in with
- which step of the list failed, and the last lines tracklist printed (or a screenshot)

## Which browser

tracklist reads the YouTube session of a browser where you are signed in, so it needs to know which one.

- **Automatic:** it detects the browser that has the session and remembers it. If that browser later loses its session, it switches to another one that is signed in.
- **Your choice:** pass `--browser` with one of chrome, brave, edge, firefox, safari, vivaldi, opera or chromium, for example `tracklist <URL> --browser safari`. tracklist remembers it for the next runs, and in the run where you pass it uses only that browser.
- **Safari** is macOS only, and needs "Full Disk Access" for your terminal app (Terminal, iTerm, Ghostty...) in System Settings > Privacy & Security.

## First run

Before you start, make sure you have:

- a YouTube channel on your Google account, because Content ID claims and uploads live there
- that Google account signed in to YouTube in your browser (see [Which browser](#which-browser))

Then run `tracklist <URL>` as usual. The first time, tracklist does a one-time setup that takes about 5 minutes and is never asked again:

1. **Allow access to the browser's cookies.** On macOS, a prompt asks for your keychain password so tracklist can read the browser's YouTube session. Choose "Always Allow" so it doesn't ask on every run. Windows doesn't ask; Linux may ask for the keyring password.
2. **Create your own Google credential.** tracklist uploads to your channel through your own Google project, which is free. A wizard opens each Google Cloud page for you and tells you what to do:
   1. Create a project: type `tracklist` as the project name and click Create.
   2. Enable the YouTube Data API v3: click Enable.
   3. Set up the app: type `tracklist` as the app name, pick your own email as the support email and as the contact email, and choose audience External.
   4. Click "Publish app" so the login doesn't expire every 7 days. Google shows an unverified app warning: it's your own app, so continue.
   5. Create an OAuth client: choose "Desktop app" as the type, type `tracklist` as the name, click Create and download the JSON.

   Back in the terminal, press Enter and tracklist picks up the new `client_secret*.json` from your downloads folder, or paste the JSON's path.
3. **Authorise tracklist.** Your browser opens so you can sign in with Google and approve access. The permission Google asks for (`youtube`) gives tracklist control of your channel; it only uploads private copies and deletes the copies it uploaded.

After that, tracklist starts analysing the video.

## Usage

Tell tracklist what to analyse in one argument, either a YouTube video or an audio file on your Mac:

- **A YouTube video:** paste its URL or just the 11-character video ID. Any form works (`watch?v=`, `youtu.be/`, a Studio link).
- **A local audio file:** give its path, for example `tracklist ~/Music/set.mp3`. Any format `ffmpeg` reads works (mp3, m4a, wav, flac...). Quote the path if it has spaces, or drag the file from Finder into the terminal.
- **Nothing:** run `tracklist` alone and it asks for the URL, ID or file path.

The tracklist starts with the video's title (for a local file, its file name without the extension), then the link, and `c` in the terminal UI copies all of it.

```bash
tracklist https://www.youtube.com/watch?v=XXXXXXXXXXX   # your own video
tracklist https://www.youtube.com/watch?v=YYYYYYYYYYY   # someone else's video
tracklist XXXXXXXXXXX                                    # the same, by video ID
tracklist set.mp3                                        # a local audio file
tracklist "~/Music/Sunday Session.m4a"                   # a path with spaces
tracklist                                                # asks what to analyse
tracklist <URL> --browser brave                          # choose the browser
tracklist <URL> --fresh                                  # ignore any previous analysis
tracklist <URL> --label                                  # show each track's record label
```

Other options: `--no-recognize` (skip Shazam), `--from-json FILE` (use a saved claims response), `--duration SECONDS` (total length, for the final gap).

Content ID can take a while to report claims on a new upload. You can quit during that wait and run the same command later: it resumes from the existing upload and never waits twice unless you pass `--fresh`. The terminal UI shows claims as they arrive, and on macOS a notification tells you when the tracklist is ready. The terminal UI also shows the download, conversion and upload progress, and then the Content ID wait. Quitting during the download or conversion stops them cleanly, and quitting during the upload asks first, because the next run uploads it again and spends quota.

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

This README is also available in Spanish: [README.es.md](README.es.md).

## Limits

- About 6 uploads per day: the API quota belongs to your own Google project.
- Content ID takes up to an hour to review an upload.
- Other people's videos are uploaded as private, and you are offered to delete them when done.

## Stored data

- `~/.config/tracklist`: chosen browser, Google credential and login, analysis cache. On Windows this is `C:\Users\<you>\.config\tracklist`.
- Downloaded audio, so an analysis can resume:
  - macOS: `~/Library/Caches/tracklist`
  - Windows: `%LOCALAPPDATA%\tracklist\Cache`
  - Linux: `~/.cache/tracklist` (or `$XDG_CACHE_HOME/tracklist`)
