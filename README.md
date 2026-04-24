# SonicStream

A local web app that converts YouTube, SoundCloud, and Spotify links into audio files. You paste a link, pick a format, and the file lands in your Downloads folder. That's pretty much it.

Everything runs on your machine. Nothing gets uploaded anywhere. The web interface is just there so you don't have to touch the command line.

## What it supports

**Sources:**
- YouTube (videos and playlists)
- SoundCloud (tracks and playlists)
- Spotify (single tracks -- playlists are not supported for Spotify)

**Output formats -- 30 total:**

| Category | Formats |
|---|---|
| Popular | MP3, M4A, FLAC, OPUS, OGG, WEBM |
| Uncompressed / Studio | WAV, AIFF, CAF, W64, RF64, AU, RAW |
| Audiophile | ALAC, WavPack (WV), TTA |
| Telephony / Retro | AMR, GSM, VOX, SLN, 8SVX, VOC, G.722, ADX |
| Legacy | WMA, MP2, MP1, M4B, SPX, AC3, E-AC3, MKA |

**Per-format quality control** is available for lossy formats. You can pick 128, 192, 256, 320 kbps, or VBR (best quality). Lossless and uncompressed formats don't have this option because bitrate doesn't apply to them.

**Cover art** gets embedded directly into the file for formats that support it (MP3, M4A, FLAC, OPUS, OGG, WMA, MKA, WEBM, ALAC, M4B, MP2). For formats that can't hold cover art, the image gets saved as a separate file next to the audio.

**Lyrics** can be fetched automatically. When enabled, SonicStream queries the LRCLIB database using a three-tier search strategy (exact match, then track+artist search, then broad search). If synced lyrics are found, they get saved as a `.lrc` file next to the audio. Plain text lyrics are used as a fallback.

**Metadata tagging** adds title, artist, and album fields to formats that support them. Artist and cover art are enabled by default but can be turned off.

## Setup

You need three things installed on your system:

1. **Python 3.8 or newer** -- [python.org](https://www.python.org/downloads/)
2. **ffmpeg** -- [ffmpeg.org](https://ffmpeg.org/download.html) (needs to be on your PATH so the terminal can find it)
3. **yt-dlp** -- install via pip: `pip install yt-dlp`

Then install the Python dependencies:

```bash
pip install flask yt-dlp
```

That's it. There's no database, no config file, no account to create.

## Running it

Drop the `new.py` file wherever you want and run it:

```bash
python new.py
```

A browser tab opens automatically at `http://127.0.0.1:5000`. If it doesn't, just open that address manually.

Press Ctrl+C in the terminal to stop the server.

## How it works

The app is a single Flask server with an embedded HTML interface. When you paste a link and hit convert:

1. yt-dlp downloads the audio from the source
2. ffmpeg converts it to your chosen format with the right codec settings
3. Metadata (title, artist, album) gets written into the file
4. Cover art gets embedded or saved alongside
5. If lyrics are enabled, LRCLIB is queried and an `.lrc` file is saved

All output goes to your Downloads folder. For playlists, files are organized into a subfolder named after the playlist.

Progress updates happen in real time through Server-Sent Events, so the UI shows download speed, percentage, and current track name as it works through playlists.

Spotify works a bit differently. Since Spotify doesn't have a public download API, SonicStream resolves the track info from the Spotify page and then routes it through a cobalt API instance to get the audio. It tries multiple cobalt mirror servers at random until one responds.

## Playlist notes

Playlists are downloaded sequentially, one track at a time. Each track gets its own metadata, cover art, and lyrics fetched independently. This means a 50-track playlist will make 50 separate API calls to LRCLIB if lyrics are enabled, which takes a few seconds per track.

YouTube playlists can be large. The app uses yt-dlp's built-in playlist extraction, which handles pagination automatically. Age-restricted videos may fail unless you're logged into YouTube in your browser (the app tries to use Brave browser cookies as a fallback on Windows, but this isn't guaranteed).

SoundCloud playlists work the same way as YouTube playlists through yt-dlp.

## Formats and codecs

Here's what ffmpeg actually does under the hood for each format group:

- **MP3** -- libmp3lame, CBR or VBR via `-q:a 0`
- **M4A / M4B / 3GP** -- native FFmpeg AAC encoder
- **FLAC** -- lossless, no quality setting
- **OPUS / WEBM** -- libopus, bitrate ceiling up to 320k
- **OGG** -- libvorbis, quality scale 0-10
- **WAV / AIFF / AU / CAF** -- PCM 16-bit, no compression
- **ALAC** -- Apple Lossless Audio Codec in MP4 container
- **WV** -- WavPack lossless
- **AMR / GSM / VOX** -- low bitrate speech codecs with fixed sample rates
- **AC3 / E-AC3** -- Dolby Digital, up to 640k / 768k

## Tips

- If a conversion fails on a YouTube video, try again. yt-dlp updates frequently to keep up with YouTube's changes. Update it with `pip install -U yt-dlp`.
- FLAC files with cover art: if the cover doesn't show in your player, the player might not support the PICTURE metadata block. This is a player issue, not an app issue.
- The quality selector automatically hides itself when you pick a lossless or uncompressed format, since bitrate doesn't apply. Same with metadata tags and cover art for formats like RAW or VOX that can't hold them.
- VBR mode for MP3 uses the highest quality VBR setting (LAME `-V 0`), which typically produces files around 220-260 kbps. For MP3, this usually sounds better than 320k CBR.
- Lyrics are fetched from LRCLIB, which is a community-maintained database. Not every song has lyrics available, especially obscure or very new releases. Instrumental tracks are detected and skipped.

## Known limitations

- Spotify playlists don't work. Only single Spotify track links are supported.
- The app runs on Flask's built-in development server. It's fine for personal use but not designed to handle multiple concurrent users.
- Spotify downloads depend on third-party cobalt API mirrors. If all mirrors are down, Spotify conversions will fail. This is out of the app's control.
- Very long playlists (hundreds of tracks) will take a while since tracks are processed one at a time.
- The app saves files to your local Downloads folder. There's no way to change this destination from the UI without editing the code.

## Requirements

- Python 3.8+
- Flask
- yt-dlp
- ffmpeg (on system PATH)

## License

Do whatever you want with it.
