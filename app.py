from flask import Flask, request, render_template_string, Response
import yt_dlp
import os
import subprocess
import re
import glob
import shutil
import time
import urllib.request
import urllib.parse
import json
import random
import threading
import webbrowser

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- PROGRESS MANAGER ---
# {task_id: {"status": str, "percent": float, "song": str, "artist": str, "thumbnail": str}}
task_progress = {}

# --- COVER ART FORMAT GROUPS ---
# Formats grouped by how ffmpeg embeds cover art into them
COVER_FORMATS_ID3V2     = {'mp3', 'mp2'}                # ID3v2 APIC frame
COVER_FORMATS_MOV       = {'m4a', 'm4b', 'alac'}        # MOV/MP4 metadata atom
COVER_FORMATS_VORBIS    = {'flac', 'ogg', 'opus'}       # Vorbis COMMENT + PICTURE block
COVER_FORMATS_MATROSKA  = {'mka', 'webm'}               # Matroska attached picture
COVER_FORMATS_ASF       = {'wma'}                       # ASF WM/Picture

# Union of every format that supports embedded cover art
ALL_COVER_FORMATS = (
    COVER_FORMATS_ID3V2 | COVER_FORMATS_MOV |
    COVER_FORMATS_VORBIS | COVER_FORMATS_MATROSKA | COVER_FORMATS_ASF
)

# LRCLIB API base URL (no rate limit, no key needed)
LRCLIB_API = "https://lrclib.net/api"


def progress_hook(d, task_id):
    current = task_progress.get(task_id, {})
    if d['status'] == 'downloading':
        raw = d.get('_percent_str', '0%').replace('%', '').strip()
        try:
            pct = float(raw)
        except (ValueError, TypeError):
            pct = current.get('percent', 0)

        # Try to extract current filename / song title from yt-dlp hook
        filename = d.get('filename', '') or d.get('tmpfilename', '')
        if filename:
            base = os.path.splitext(os.path.basename(filename))[0]
            # Strip temp suffix yt-dlp adds like .fXXX
            base = re.sub(r'\.(f\d+|webm|m4a|opus|mp4)$', '', base)
            song_name = base if base else current.get('song', '')
        else:
            song_name = current.get('song', '')

        # Speed / ETA for status string
        speed = d.get('_speed_str', '').strip()
        eta   = d.get('_eta_str', '').strip()
        status_parts = ['Downloading']
        if speed: status_parts.append(speed)
        if eta:   status_parts.append(f'ETA {eta}')

        task_progress[task_id] = {
            **current,
            "status":    ' \u00b7 '.join(status_parts),
            "percent":   pct,
            "song":      song_name,
        }

    elif d['status'] == 'finished':
        task_progress[task_id] = {
            **current,
            "status":  "Converting...",
            "percent": 99.0,
        }

    elif d['status'] == 'error':
        task_progress[task_id] = {
            **current,
            "status":  "Error during download",
            "percent": current.get('percent', 0),
        }


# --- HELPERS ---

def save_cover_alongside(cover_path, output_file):
    """Save cover image next to output file for formats that can't embed cover art."""
    if not cover_path or not os.path.exists(cover_path):
        return
    try:
        out_dir = os.path.dirname(output_file)
        name_no_ext = os.path.splitext(os.path.basename(output_file))[0]
        cover_dest = os.path.join(out_dir, f"{name_no_ext}_cover.jpg")
        shutil.copy2(cover_path, cover_dest)
        print(f"  [cover] Saved alongside: {cover_dest}")
    except OSError as e:
        print(f"  [cover] Could not save alongside: {e}")


def save_lyrics_alongside(output_file, synced_lyrics=None, plain_lyrics=None):
    """Save .lrc lyrics file next to output file. Prefers synced lyrics."""
    lyrics = synced_lyrics or plain_lyrics
    if not lyrics or not lyrics.strip():
        return False
    try:
        out_dir = os.path.dirname(output_file)
        name_no_ext = os.path.splitext(os.path.basename(output_file))[0]
        lrc_path = os.path.join(out_dir, f"{name_no_ext}.lrc")
        with open(lrc_path, 'w', encoding='utf-8') as f:
            f.write(lyrics.strip() + '\n')
        print(f"  [lyrics] Saved: {lrc_path}")
        return True
    except Exception as e:
        print(f"  [lyrics] Could not save: {e}")
        return False


def detect_downloaded_file(base_path):
    """Find the actual downloaded file (may have any extension) and return its path."""
    found = glob.glob(base_path + "*")
    for f in found:
        if os.path.isfile(f):
            return f
    return None


def download_thumbnail(url, dest_path):
    """Download a thumbnail image. Returns dest_path on success, None on failure."""
    if not url:
        return None
    try:
        urllib.request.urlretrieve(url, dest_path)
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
    except Exception as e:
        print(f"  [thumb] Download failed: {e}")
    return None


def find_thumbnail_for_track(directory, base_name):
    """Find a thumbnail file downloaded by yt-dlp next to an audio file."""
    for ext in ('.jpg', '.jpeg', '.png', '.webp'):
        candidate = os.path.join(directory, base_name + ext)
        if os.path.exists(candidate):
            return candidate
    # yt-dlp sometimes appends extra suffixes
    for ext in ('.jpg', '.jpeg', '.png', '.webp'):
        candidates = glob.glob(os.path.join(directory, base_name + '*' + ext))
        if candidates:
            return candidates[0]
    return None


def read_track_metadata(json_path):
    """Read title, artist, album, duration, and thumbnail from a yt-dlp .info.json file."""
    if not json_path or not os.path.exists(json_path):
        return None
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        title = meta.get('title', '')
        artist = meta.get('artist') or meta.get('uploader') or meta.get('channel') or ''
        album = meta.get('album') or ''
        duration = meta.get('duration') or 0
        # Get best thumbnail
        thumbnail = meta.get('thumbnail', '')
        thumbnails = meta.get('thumbnails') or []
        if thumbnails:
            thumbnails.sort(key=lambda t: t.get('width', 0) or 0, reverse=True)
            thumbnail = thumbnails[0].get('url', '') or thumbnail
        return {
            'title': title, 'artist': artist, 'album': album,
            'duration': duration, 'thumbnail': thumbnail,
        }
    except Exception as e:
        print(f"  [meta] Could not read {json_path}: {e}")
        return None


def _lrclib_api_get(path, params=None):
    """Make a GET request to LRCLIB API. Returns parsed JSON or None."""
    try:
        url = f"{LRCLIB_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
        req = urllib.request.Request(url, headers={
            'User-Agent': 'SonicStream/1.0 (https://github.com/sonicstream)',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"  [lrclib] Request failed: {e}")
        return None


def fetch_lyrics_from_lrclib(track_name, artist_name="", album_name="", duration=None):
    """Fetch lyrics from LRCLIB.

    Strategy:
      1. If track_name + artist_name + duration are available, try /api/get (exact match).
      2. If that fails, try /api/search with track_name + artist_name.
      3. If only track_name, try /api/search with q=track_name.

    Returns dict with 'syncedLyrics', 'plainLyrics', 'instrumental' keys, or None.
    """
    if not track_name:
        return None

    # Clean up track name and artist for search
    clean_track = clean_title_for_search(track_name)
    clean_artist = clean_title_for_search(artist_name) if artist_name else ""
    clean_album = album_name or ""

    # Strategy 1: Exact match via /api/get
    if clean_artist and duration and duration > 0:
        result = _lrclib_api_get("/get", {
            'track_name': clean_track,
            'artist_name': clean_artist,
            'album_name': clean_album,
            'duration': int(duration),
        })
        if result and isinstance(result, dict) and result.get('syncedLyrics') or result.get('plainLyrics'):
            print(f"  [lrclib] Exact match found for: {clean_artist} - {clean_track}")
            return result

    # Strategy 2: Search by track_name + artist_name
    if clean_artist:
        results = _lrclib_api_get("/search", {
            'track_name': clean_track,
            'artist_name': clean_artist,
        })
        if results and isinstance(results, list) and results:
            # Pick best result (first match)
            best = results[0]
            if best.get('syncedLyrics') or best.get('plainLyrics'):
                print(f"  [lrclib] Search match: {best.get('artistName', '')} - {best.get('trackName', '')}")
                return best

    # Strategy 3: Broad search with q parameter
    if clean_artist:
        q = f"{clean_artist} {clean_track}"
    else:
        q = clean_track

    results = _lrclib_api_get("/search", {'q': q})
    if results and isinstance(results, list) and results:
        for r in results:
            if r.get('syncedLyrics') or r.get('plainLyrics'):
                print(f"  [lrclib] Broad match: {r.get('artistName', '')} - {r.get('trackName', '')}")
                return r

    print(f"  [lrclib] No lyrics found for: {clean_track}")
    return None


def resolve_spotify_link(spotify_url):
    """Returns (display_title, artist, album, thumbnail_url)"""
    try:
        req = urllib.request.Request(
            spotify_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8')
        title = re.search(r'<meta property="og:title" content="(.*?)"', html)
        desc  = re.search(r'<meta property="og:description" content="(.*?)"', html)
        image = re.search(r'<meta property="og:image" content="(.*?)"', html)
        if title and desc:
            song   = title.group(1)
            artist = desc.group(1).split('\u00b7')[0].strip()
            thumb  = image.group(1) if image else ""
            print(f"[spotify] {artist} - {song}")
            return f"{artist} - {song}", artist, "", thumb
    except Exception as e:
        print(f"[spotify] resolve error: {e}")
    return "Spotify_Audio", "", "", ""


def download_spotify_via_api(spotify_url, output_path):
    api_servers = [
        "https://api.cobalt.tools/api/json", "https://cobalt.pog.com.hr/api/json",
        "https://api.wuk.sh/api/json",        "https://cobalt.club/api/json",
        "https://api.server.garden/api/json",  "https://cobalt.timelessroses.ca/api/json",
        "https://cobalt.synbay.app/api/json",  "https://dl.khub.ky/api/json",
        "https://cobalt.raycast.com/api/json", "https://cobalt.kwiatekmiki.com/api/json",
        "https://cobalt.mashed.jp/api/json",   "https://cobalt.cafe/api/json",
        "https://api.cobalt.kyrie25.me/api/json","https://cobalt.6769.club/api/json",
        "https://cobalt.xy24.eu.org/api/json"
    ]
    random.shuffle(api_servers)
    payload = {"url": spotify_url, "isAudioOnly": True, "aFormat": "mp3"}
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    for api_url in api_servers:
        try:
            print(f"[cobalt] Trying: {api_url}")
            req = urllib.request.Request(api_url, data=json.dumps(payload).encode(), headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                if 'url' in data:
                    print("[cobalt] Success! Downloading...")
                    with urllib.request.urlopen(data['url']) as stream, open(output_path, 'wb') as f:
                        shutil.copyfileobj(stream, f)
                    return True
        except Exception:
            continue
    return False

def clean_title_for_search(title):
    title = re.sub(r'[\(\[\{].*?[\)\]\}]', '', title)
    for kw in ['official video','official audio','lyrics','video','audio','hq','hd','4k','remastered','visualizer']:
        title = re.sub(r'(?i)\b' + kw + r'\b', '', title)
    return re.sub(r'\s+', ' ', title).strip()

def get_output_extension(fmt):
    mapping = {'alac': 'm4a', 'spx': 'ogg', 'aac': 'm4a', 'g722': 'wav', '8svx': 'iff'}
    return mapping.get(fmt, fmt)


# Quality presets: which formats support quality selection
# 'vbr' means variable bitrate / best quality mode
LOSSY_QUALITY_FORMATS = {
    'mp3', 'm4a', 'm4b', '3gp', 'opus', 'webm', 'ogg', 'wma', 'mp2', 'mp1', 'ac3', 'eac3', 'spx', 'mka'
}


def get_quality_audio_flags(fmt, quality='320k'):
    """Return ffmpeg audio codec flags for the given format and quality preset.

    quality: '128k', '256k', '320k', 'vbr'
    For lossless / uncompressed / fixed-rate formats, the quality parameter is ignored.
    """
    # -- LOSSLESS FORMATS (quality has no effect) --
    if fmt == 'flac':    return ['-c:a', 'flac']
    if fmt == 'alac':    return ['-c:a', 'alac']
    if fmt == 'wv':      return ['-c:a', 'wavpack']
    if fmt == 'tta':     return ['-c:a', 'tta']

    # -- UNCOMPRESSED FORMATS --
    if fmt == 'wav':     return ['-c:a', 'pcm_s16le']
    if fmt == 'rf64':    return ['-c:a', 'pcm_s16le', '-f', 'rf64']
    if fmt == 'w64':     return ['-c:a', 'pcm_s16le', '-f', 'w64']
    if fmt == 'aiff':    return ['-c:a', 'pcm_s16be']
    if fmt == 'au':      return ['-c:a', 'pcm_s16be']
    if fmt == 'caf':     return ['-c:a', 'pcm_s16be']
    if fmt == 'raw':     return ['-c:a', 'pcm_f32le', '-f', 'f32le']

    # -- FIXED-RATE / SPECIAL CODECS --
    if fmt == 'amr':   return ['-c:a', 'libopencore_amrnb', '-ar', '8000', '-ac', '1']
    if fmt == 'awb':   return ['-c:a', 'libvo_amrwbenc', '-ar', '16000', '-ac', '1']
    if fmt == 'gsm':   return ['-c:a', 'gsm', '-ar', '8000', '-ac', '1']
    if fmt == 'vox':   return ['-c:a', 'adpcm_ima_oki', '-f', 'u8', '-ar', '8000', '-ac', '1']
    if fmt == 'sln':   return ['-c:a', 'pcm_s16le', '-f', 's16le', '-ar', '8000', '-ac', '1']
    if fmt == 'g722':  return ['-c:a', 'g722', '-ar', '16000', '-ac', '1']
    if fmt == '8svx':  return ['-c:a', 'pcm_s8', '-f', 'iff', '-ac', '1']
    if fmt == 'adx':   return ['-c:a', 'adpcm_adx', '-ar', '44100', '-ac', '2']
    if fmt == 'voc':   return ['-c:a', 'pcm_u8', '-ac', '1', '-f', 'voc']
    if fmt == 'mmf':   return ['-c:a', 'adpcm_yamaha', '-f', 'mmf']
    if fmt == 'spx':   return ['-c:a', 'libspeex', '-ar', '16000', '-ac', '1']
    if fmt == 'mka':   return ['-c:a', 'libvorbis']

    # -- LOSSY FORMATS WITH QUALITY CONTROL --

    # MP3: VBR uses -V 0 (best VBR, ~245kbps)
    if fmt == 'mp3':
        if quality == 'vbr':  return ['-c:a', 'libmp3lame', '-q:a', '0']
        return ['-c:a', 'libmp3lame', '-b:a', quality]

    # AAC (M4A, M4B, 3GP): native FFmpeg aac encoder
    # -q:a is NOT supported by FFmpeg's aac encoder; use -vbr for VBR (FFmpeg 5.1+),
    # or fall back to high bitrate CBR which sounds just as good.
    if fmt in ('m4a', 'm4b', '3gp'):
        if quality == 'vbr':  return ['-c:a', 'aac', '-b:a', '320k']
        return ['-c:a', 'aac', '-b:a', quality]

    # OPUS / WEBM: Opus is natively VBR, just vary the bitrate ceiling
    if fmt in ('opus', 'webm'):
        br = {'128k': '128k', '192k': '192k', '256k': '256k', '320k': '320k', 'vbr': '320k'}
        return ['-c:a', 'libopus', '-b:a', br.get(quality, '192k')]

    # OGG Vorbis: uses -q:a quality scale (0-10)
    if fmt == 'ogg':
        q = {'128k': '4', '192k': '6', '256k': '7', '320k': '9', 'vbr': '10'}
        return ['-c:a', 'libvorbis', '-q:a', q.get(quality, '6')]

    # WMA
    if fmt == 'wma':
        br = {'128k': '128k', '192k': '192k', '256k': '256k', '320k': '320k', 'vbr': '192k'}
        return ['-c:a', 'wmav2', '-b:a', br.get(quality, '192k')]

    # MP2
    if fmt == 'mp2':
        br = {'128k': '128k', '192k': '192k', '256k': '256k', '320k': '320k', 'vbr': '384k'}
        return ['-c:a', 'mp2', '-b:a', br.get(quality, '320k')]

    # MP1
    if fmt == 'mp1':
        br = {'128k': '128k', '192k': '192k', '256k': '256k', '320k': '320k', 'vbr': '384k'}
        return ['-c:a', 'mp2', '-b:a', br.get(quality, '384k')]

    # AC3
    if fmt == 'ac3':
        br = {'128k': '128k', '192k': '192k', '256k': '256k', '320k': '384k', 'vbr': '640k'}
        return ['-c:a', 'ac3', '-b:a', br.get(quality, '192k')]

    # E-AC3
    if fmt == 'eac3':
        br = {'128k': '128k', '192k': '192k', '256k': '256k', '320k': '320k', 'vbr': '768k'}
        return ['-c:a', 'eac3', '-b:a', br.get(quality, '256k')]

    # Fallback
    return ['-c:a', 'aac', '-b:a', quality]

# ---- FFMPEG COMMAND BUILDER ----

def build_conversion_command(fmt, input_file, output_file, title="", artist="", album="", cover_path=None, quality='320k'):
    """Build ffmpeg command with optional cover art, metadata, and quality control.

    Cover art embedding strategy varies by format family:
      - ID3v2 (mp3, mp2):   uses APIC frame with mjpeg codec
      - MOV (m4a, m4b, alac): uses attached picture disposition
      - Vorbis (flac, ogg, opus): uses PICTURE metadata block
      - Matroska (mka, webm): uses attached picture
      - ASF (wma):            uses mjpeg codec for WM/Picture

    quality: '128k', '256k', '320k', 'vbr'
      - For lossless/uncompressed formats, quality is ignored.
      - For lossy formats, maps to appropriate ffmpeg flags per codec.

    All metadata fields (title, artist, album, cover) are fully optional.
    """
    cmd = ['ffmpeg', '-y', '-i', input_file]

    use_cover = bool(cover_path and os.path.exists(cover_path) and fmt in ALL_COVER_FORMATS)

    if use_cover:
        cmd += ['-i', cover_path]
        cmd += ['-map', '0:a', '-map', '1:v']
        if fmt in COVER_FORMATS_ID3V2:
            # MP3 / MP2: ID3v2 APIC frame
            cmd += ['-c:v', 'mjpeg', '-id3v2_version', '3',
                    '-metadata:s:v', 'title=Album cover',
                    '-metadata:s:v', 'comment=Cover (front)']
        elif fmt in COVER_FORMATS_ASF:
            # WMA: ASF container with WM/Picture
            cmd += ['-c:v', 'mjpeg', '-metadata:s:v', 'title=Album cover']
        elif fmt in COVER_FORMATS_VORBIS:
            # FLAC: needs -c:v mjpeg WITHOUT -disposition — FLAC stores art
            # as METADATA_BLOCK_PICTURE automatically when a video stream is
            # mapped. Adding attached_pic is a Matroska concept and silently
            # breaks FLAC embedding on some FFmpeg builds.
            # OGG/OPUS in their native containers also handle this fine.
            cmd += ['-c:v', 'mjpeg']
        else:
            # M4A, M4B, ALAC, MKA, WEBM: copy stream works natively
            cmd += ['-c:v', 'copy', '-disposition:v:1', 'attached_pic']
    else:
        cmd += ['-map', '0:a']

    # Audio codec + quality selection via get_quality_audio_flags()
    audio_flags = get_quality_audio_flags(fmt, quality)
    cmd.extend(audio_flags)

    # Embed title / artist / album metadata (all optional)
    if title:  cmd += ['-metadata', f'title={title}']
    if artist: cmd += ['-metadata', f'artist={artist}']
    if album:  cmd += ['-metadata', f'album={album}']

    cmd.append(output_file)
    return cmd


# --- HTML UI ---
HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SonicStream | Ultimate Local</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #0f0f0f; --card: #1c1c1c; --accent: #ff0033; --text: #ffffff; }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Outfit', sans-serif; background-color: var(--bg); color: var(--text);
            min-height: 100vh; display: flex; justify-content: center; align-items: center;
            background-image: radial-gradient(circle at 10% 20%, rgba(255,0,51,0.08) 0%, transparent 20%);
            padding: 20px;
        }
        .container {
            background: rgba(28,28,28,0.95); width: 100%; max-width: 650px;
            padding: 40px; border-radius: 24px; border: 1px solid rgba(255,255,255,0.05); text-align: center;
            box-shadow: 0 30px 60px -15px rgba(0,0,0,0.8); position: relative; z-index: 1;
        }
        .header h1 { margin-bottom: 5px; font-size: 32px; letter-spacing: -1px; }
        .header span { color: var(--accent); }
        .input-group { margin-bottom: 25px; text-align: left; position: relative; }
        label { display: flex; justify-content: space-between; margin-bottom: 10px; font-size: 12px; font-weight: 700; color: #888; text-transform: uppercase; letter-spacing: 1px; }

        input[type="text"] {
            width: 100%; height: 55px; padding: 0 20px; background: #252525;
            border: 1px solid #333; border-radius: 14px; color: white;
            font-size: 15px; font-family: inherit; outline: none; transition: 0.3s;
        }
        input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 4px rgba(255,0,51,0.15); }

        .checkbox-wrapper { display: flex; align-items: center; margin-top: 15px; background: #252525; padding: 10px 15px; border-radius: 10px; border: 1px solid #333; }
        .checkbox-wrapper input { width: auto; height: auto; margin-right: 10px; accent-color: var(--accent); cursor: pointer; }
        .checkbox-wrapper label { margin: 0; color: #fff; cursor: pointer; text-transform: none; font-size: 14px; font-weight: 500; }

        .custom-select-wrapper { position: relative; user-select: none; }
        .custom-select { position: relative; display: flex; flex-direction: column; }
        .select-trigger {
            position: relative; display: flex; align-items: center; justify-content: space-between;
            padding: 0 20px; height: 55px; font-size: 15px; font-weight: 500;
            background: #252525; border: 1px solid #333; border-radius: 14px;
            cursor: pointer; transition: 0.3s; color: #fff; z-index: 2;
        }
        .select-trigger:hover { border-color: #555; }
        .arrow { width: 10px; height: 10px; border-bottom: 2px solid var(--accent); border-right: 2px solid var(--accent); transform: rotate(45deg); transition: transform 0.4s cubic-bezier(0.68,-0.55,0.27,1.55); margin-bottom: 3px; }
        .custom-select.open .arrow { transform: rotate(-135deg); margin-bottom: -3px; }
        .custom-select.open .select-trigger { border-color: var(--accent); border-radius: 14px 14px 0 0; }
        .custom-options {
            position: absolute; display: block; top: 100%; left: 0; right: 0;
            background: #222; border: 1px solid var(--accent); border-top: 0;
            border-radius: 0 0 14px 14px; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            overflow-y: hidden; z-index: 100;
            max-height: 0; opacity: 0; transform: translateY(-10px);
            pointer-events: none; transition: all 0.4s cubic-bezier(0.25,0.8,0.25,1);
        }
        .custom-select.open .custom-options { max-height: 250px; opacity: 1; transform: translateY(0); pointer-events: all; overflow-y: auto; }
        .custom-option { padding: 12px 20px; cursor: pointer; transition: 0.2s; border-bottom: 1px solid #2a2a2a; font-size: 14px; color: #ccc; }
        .custom-option:last-child { border-bottom: none; }
        .custom-option.selected { background: #333; color: var(--accent); font-weight: 700; }
        .custom-option:hover { background: var(--accent); color: white; padding-left: 25px; }
        .opt-group-label { padding: 8px 15px; font-size: 11px; font-weight: 800; text-transform: uppercase; color: #666; background: #181818; letter-spacing: 1px; pointer-events: none; }
        select { display: none; }

        button { width: 100%; padding: 20px; background: var(--accent); color: white; border: none; border-radius: 14px; font-weight: 700; font-size: 16px; cursor: pointer; transition: all 0.3s; text-transform: uppercase; letter-spacing: 1px; box-shadow: 0 10px 20px -5px rgba(255,0,51,0.4); }
        button:hover { transform: translateY(-2px); box-shadow: 0 15px 25px -5px rgba(255,0,51,0.5); filter: brightness(110%); }

        /* ---- LOADER OVERLAY ---- */
        #loader {
            display: none; position: absolute; inset: 0;
            background: rgba(12,12,12,0.97); z-index: 200;
            flex-direction: column; justify-content: center; align-items: center;
            border-radius: 24px; padding: 40px; gap: 0;
        }

        /* Thumbnail / spinning ring combo */
        .thumb-ring-wrap {
            position: relative; width: 96px; height: 96px;
            margin-bottom: 22px; flex-shrink: 0;
        }
        #loader-thumb {
            width: 88px; height: 88px; border-radius: 12px;
            object-fit: cover; position: absolute;
            top: 50%; left: 50%; transform: translate(-50%,-50%);
            background: #222; transition: opacity 0.4s;
            display: block;
        }
        .ring {
            position: absolute; inset: 0; border-radius: 50%;
            border: 3px solid transparent;
            border-top-color: var(--accent);
            animation: spin 0.9s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* Song info */
        #loader-song   { font-size: 17px; font-weight: 700; color: #fff; margin-bottom: 3px; max-width: 380px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        #loader-artist { font-size: 13px; color: #888; margin-bottom: 20px; max-width: 380px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        /* Progress bar */
        .progress-wrap { width: 100%; max-width: 420px; }
        .progress-track { background: #1e1e1e; border-radius: 20px; height: 10px; overflow: hidden; border: 1px solid #2a2a2a; }
        .progress-bar { width: 0%; height: 100%; background: linear-gradient(90deg, var(--accent), #ff6680); transition: width 0.35s cubic-bezier(0.1,0.7,0.1,1); box-shadow: 0 0 12px rgba(255,0,51,0.5); border-radius: 20px; }
        .progress-meta { display: flex; justify-content: space-between; margin-top: 8px; font-size: 12px; color: #666; }
        #progress-pct  { color: var(--accent); font-weight: 700; }
        #status-line   { color: #555; font-size: 12px; margin-top: 14px; min-height: 16px; font-family: monospace; }

        .sort-btn { background: #333; border: none; padding: 5px 10px; border-radius: 6px; color: #fff; font-size: 11px; cursor: pointer; margin-left: 10px; box-shadow: none; width: auto; text-transform: none; letter-spacing: 0; }
        .sort-btn:hover { background: #444; transform: none; }

        .checkbox-stack { display: flex; flex-direction: column; gap: 8px; }
        .checkbox-hint { font-size: 11px; color: #555; margin-left: 30px; margin-top: -6px; }
        .option-transition {
            max-height: 120px; opacity: 1;
            transition: max-height 0.35s cubic-bezier(0.4,0,0.2,1), opacity 0.3s ease, margin 0.3s ease, padding 0.3s ease, border-color 0.3s ease;
        }
        .option-transition.option-hidden {
            max-height: 0; opacity: 0; overflow: hidden; margin-top: 0 !important; padding-top: 0 !important; padding-bottom: 0 !important; border-color: transparent !important; pointer-events: none;
        }

        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #1a1a1a; border-radius: 4px; }
        ::-webkit-scrollbar-thumb { background: #444; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--accent); }
    </style>
</head>
<body>
    <div class="container">
        <!-- LOADER OVERLAY -->
        <div id="loader">
            <div class="thumb-ring-wrap">
                <img id="loader-thumb" src="" alt="">
                <div class="ring"></div>
            </div>
            <div id="loader-song">Fetching info...</div>
            <div id="loader-artist">&nbsp;</div>
            <div class="progress-wrap">
                <div class="progress-track"><div class="progress-bar" id="progressBar"></div></div>
                <div class="progress-meta">
                    <span id="status-line">Starting...</span>
                    <span id="progress-pct">0%</span>
                </div>
            </div>
        </div>

        <!-- MAIN UI -->
        <div class="header">
            <h1>Sonic<span>Stream</span></h1>
            <p style="color:#666; margin-bottom:30px; font-size:14px;">Ultimate Local Converter</p>
        </div>

        <form id="convertForm">
            <div class="input-group">
                <label>YouTube / SoundCloud / Spotify Link</label>
                <input type="text" name="url" placeholder="Paste link here..." required autocomplete="off">
            </div>

            <div class="input-group">
                <label>
                    Target Format
                    <button type="button" class="sort-btn" onclick="toggleSort()">Sort: Default &#9660;</button>
                </label>
                <select name="format" id="realSelect">
                    <optgroup label="Popular & Modern">
                        <option value="mp3"  data-rank="80" selected>MP3 - Universal Audio</option>
                        <option value="m4a"  data-rank="90">M4A - AAC (Apple/Web Standard)</option>
                        <option value="flac" data-rank="100">FLAC - Free Lossless Audio</option>
                        <option value="opus" data-rank="95">OPUS - High Efficiency</option>
                        <option value="ogg"  data-rank="85">OGG - Vorbis</option>
                        <option value="webm" data-rank="85">WEBM - HTML5 Audio</option>
                    </optgroup>
                    <optgroup label="Uncompressed / Studio">
                        <option value="wav"  data-rank="100">WAV - Microsoft Wave</option>
                        <option value="aiff" data-rank="100">AIFF - Apple Interchange</option>
                        <option value="caf"  data-rank="100">CAF - Apple Core Audio</option>
                        <option value="w64"  data-rank="100">W64 - Sony Wave64</option>
                        <option value="rf64" data-rank="100">RF64 - Broadcast Wave</option>
                        <option value="au"   data-rank="99">AU - Sun Microsystems</option>
                        <option value="raw"  data-rank="99">RAW - Headerless PCM</option>
                    </optgroup>
                    <optgroup label="Audiophile">
                        <option value="alac" data-rank="100">ALAC - Apple Lossless</option>
                        <option value="wv"   data-rank="100">WV - WavPack</option>
                        <option value="tta"  data-rank="100">TTA - True Audio</option>
                    </optgroup>
                    <optgroup label="Telephony / Retro">
                        <option value="amr"  data-rank="20">AMR - Narrowband</option>
                        <option value="gsm"  data-rank="20">GSM - Mobile</option>
                        <option value="vox"  data-rank="15">VOX - Dialogic</option>
                        <option value="sln"  data-rank="15">SLN - Asterisk PCM</option>
                        <option value="8svx" data-rank="10">8SVX - Amiga 8-bit</option>
                        <option value="voc"  data-rank="10">VOC - Creative Labs</option>
                        <option value="g722" data-rank="40">G.722 - ADPCM</option>
                        <option value="adx"  data-rank="40">ADX - CRI Middleware</option>
                    </optgroup>
                    <optgroup label="Legacy / Other">
                        <option value="wma"  data-rank="60">WMA - Windows Media</option>
                        <option value="mp2"  data-rank="60">MP2 - MPEG Layer II</option>
                        <option value="mp1"  data-rank="50">MP1 - MPEG Layer I</option>
                        <option value="m4b"  data-rank="75">M4B - Audiobook</option>
                        <option value="spx"  data-rank="35">SPX - Speex</option>
                        <option value="ac3"  data-rank="80">AC3 - Dolby Digital</option>
                        <option value="eac3" data-rank="85">E-AC3 - Dolby Plus</option>
                        <option value="mka"  data-rank="90">MKA - Matroska Audio</option>
                    </optgroup>
                </select>
                <div class="custom-select-wrapper">
                    <div class="custom-select">
                        <div class="select-trigger"><span id="triggerText">MP3 - Universal Audio</span><div class="arrow"></div></div>
                        <div class="custom-options" id="customOptions"></div>
                    </div>
                </div>
            </div>
            <div class="input-group" id="qualityGroup">
                <label>Audio Quality</label>
                <select name="quality" id="qualitySelect">
                    <option value="128k">128 kbps</option>
                    <option value="192k">192 kbps</option>
                    <option value="256k">256 kbps</option>
                    <option value="320k" selected>320 kbps</option>
                    <option value="vbr">VBR (Best Quality)</option>
                </select>
                <div class="custom-select-wrapper">
                    <div class="custom-select" id="qualityDropdown">
                        <div class="select-trigger"><span id="qualityTriggerText">320 kbps</span><div class="arrow"></div></div>
                        <div class="custom-options" id="qualityCustomOptions"></div>
                    </div>
                </div>
            </div>

            <div class="input-group">
                <div class="checkbox-stack">
                    <div class="checkbox-wrapper">
                        <input type="checkbox" id="is_playlist" name="is_playlist" value="true">
                        <label for="is_playlist">Download as Playlist (Batch)</label>
                    </div>
                    <div class="checkbox-wrapper" id="artistOption">
                        <input type="checkbox" id="want_artist" name="want_artist" value="true" checked>
                        <label for="want_artist">Artist &amp; Title Tags</label>
                    </div>
                    <div class="checkbox-wrapper" id="coverOption">
                        <input type="checkbox" id="want_cover" name="want_cover" value="true" checked>
                        <label for="want_cover">Song Cover Art</label>
                    </div>
                    <div class="checkbox-wrapper" id="lyricsOption">
                        <input type="checkbox" id="fetch_lyrics" name="fetch_lyrics" value="true">
                        <label for="fetch_lyrics">Fetch Lyrics (.lrc)</label>
                    </div>
                    <p class="checkbox-hint" id="lyricsHint">Uses LRCLIB to fetch synced lyrics if available</p>
                </div>
            </div>

            <button type="submit">CONVERT & DOWNLOAD</button>
        </form>
    </div>

    <script>
        // ---- DROPDOWN ----
        const realSelect    = document.getElementById('realSelect');
        const customOptions = document.getElementById('customOptions');
        const triggerText   = document.getElementById('triggerText');
        const wrapper       = document.querySelector('.custom-select');
        let sortMode = 0;

        function buildDropdown() {
            customOptions.innerHTML = '';
            let allOptions = [];
            const groups = realSelect.getElementsByTagName('optgroup');
            if (sortMode === 0) {
                for (let group of groups) {
                    let divGroup = document.createElement('div');
                    divGroup.className = 'opt-group-label';
                    divGroup.textContent = group.label;
                    customOptions.appendChild(divGroup);
                    for (let opt of group.getElementsByTagName('option')) createOptionDiv(opt);
                }
            } else {
                let opts = realSelect.querySelectorAll('option');
                opts.forEach(o => allOptions.push(o));
                if (sortMode === 1) allOptions.sort((a,b) => b.getAttribute('data-rank') - a.getAttribute('data-rank'));
                if (sortMode === 2) allOptions.sort((a,b) => a.getAttribute('data-rank') - b.getAttribute('data-rank'));
                allOptions.forEach(opt => createOptionDiv(opt));
            }
        }

        function createOptionDiv(opt) {
            const div = document.createElement('div');
            div.className = 'custom-option';
            if (opt.selected) div.classList.add('selected');
            div.textContent = opt.textContent;
            div.onclick = function() {
                realSelect.value = opt.value;
                triggerText.textContent = opt.textContent;
                wrapper.classList.remove('open');
                document.querySelectorAll('#customOptions .custom-option').forEach(el => el.classList.remove('selected'));
                div.classList.add('selected');
                updateOptionVisibility();
            };
            customOptions.appendChild(div);
        }

        document.querySelector('.select-trigger').addEventListener('click', function() { wrapper.classList.toggle('open'); });
        window.addEventListener('click', function(e) { if (!wrapper.contains(e.target)) wrapper.classList.remove('open'); });

        function toggleSort() {
            sortMode = (sortMode + 1) % 3;
            const btn = document.querySelector('.sort-btn');
            if (sortMode === 0) btn.textContent = "Sort: Default \u25BC";
            if (sortMode === 1) btn.textContent = "Sort: Quality High \u2605";
            if (sortMode === 2) btn.textContent = "Sort: Quality Low \u2606";
            buildDropdown();
            wrapper.classList.add('open');
        }

        // ---- CONVERT + LIVE PROGRESS ----
        async function startConversion(event) {
            event.preventDefault();
            const form     = event.target;
            const formData = new FormData(form);
            const taskId   = 'task_' + Math.random().toString(36).substr(2, 9);
            formData.append('task_id', taskId);

            // Show overlay
            document.getElementById('loader').style.display = 'flex';

            const progressBar  = document.getElementById('progressBar');
            const progressPct  = document.getElementById('progress-pct');
            const statusLine   = document.getElementById('status-line');
            const loaderSong   = document.getElementById('loader-song');
            const loaderArtist = document.getElementById('loader-artist');
            const loaderThumb  = document.getElementById('loader-thumb');

            let lastThumb = '';

            // SSE - live progress feed
            const eventSource = new EventSource('/progress/' + taskId);
            eventSource.onmessage = function(e) {
                const data = JSON.parse(e.data);
                const pct  = Math.min(data.percent || 0, 100);

                progressBar.style.width = pct + '%';
                progressPct.textContent = Math.round(pct) + '%';
                statusLine.textContent  = data.status || '';

                if (data.song)   loaderSong.textContent   = data.song;
                if (data.artist) loaderArtist.textContent = data.artist;

                if (data.thumbnail && data.thumbnail !== lastThumb) {
                    lastThumb = data.thumbnail;
                    loaderThumb.style.opacity = '0';
                    setTimeout(() => {
                        loaderThumb.src = data.thumbnail;
                        loaderThumb.style.opacity = '1';
                    }, 200);
                }

                if (data.status === 'Complete' && pct >= 100) eventSource.close();
            };
            eventSource.onerror = function() { eventSource.close(); };

            try {
                const response   = await fetch('/convert', { method: 'POST', body: formData });
                const resultHtml = await response.text();
                eventSource.close();
                document.open(); document.write(resultHtml); document.close();
            } catch (err) {
                eventSource.close();
                alert('Error: ' + err);
                document.getElementById('loader').style.display = 'none';
            }
        }

        // ---- QUALITY DROPDOWN ----
        const qualitySelect       = document.getElementById('qualitySelect');
        const qualityCustomOpts   = document.getElementById('qualityCustomOptions');
        const qualityTriggerText  = document.getElementById('qualityTriggerText');
        const qualityWrapper      = document.getElementById('qualityDropdown');

        function buildQualityDropdown() {
            qualityCustomOpts.innerHTML = '';
            for (let opt of qualitySelect.getElementsByTagName('option')) {
                const div = document.createElement('div');
                div.className = 'custom-option';
                if (opt.selected) div.classList.add('selected');
                div.textContent = opt.textContent;
                div.onclick = function() {
                    qualitySelect.value = opt.value;
                    qualityTriggerText.textContent = opt.textContent;
                    qualityWrapper.classList.remove('open');
                    qualityCustomOpts.querySelectorAll('.custom-option').forEach(el => el.classList.remove('selected'));
                    div.classList.add('selected');
                };
                qualityCustomOpts.appendChild(div);
            }
        }

        qualityWrapper.querySelector('.select-trigger').addEventListener('click', function(e) { e.stopPropagation(); qualityWrapper.classList.toggle('open'); });
        window.addEventListener('click', function(e) { if (!qualityWrapper.contains(e.target)) qualityWrapper.classList.remove('open'); });

        // ---- DYNAMIC OPTION VISIBILITY ----
        // Formats with no bitrate/quality control (lossless, uncompressed, fixed-rate codecs)
        const NO_QUALITY = new Set([
            'flac','alac','wv','tta',
            'wav','rf64','w64','aiff','au','caf','raw',
            'amr','awb','gsm','vox','sln','g722','8svx','adx','voc','mmf','spx','mka'
        ]);
        // Formats that cannot hold any metadata at all
        const NO_METADATA = new Set([
            'raw','vox','sln','gsm','amr','awb','g722','8svx','adx','voc','mmf'
        ]);

        function toggleOption(id, hidden) {
            const el = document.getElementById(id);
            if (!el) return;
            if (!el.classList.contains('option-transition')) el.classList.add('option-transition');
            requestAnimationFrame(() => el.classList.toggle('option-hidden', hidden));
        }

        function updateOptionVisibility() {
            const fmt = realSelect.value;
            const noQuality  = NO_QUALITY.has(fmt);
            const noMetadata = NO_METADATA.has(fmt);

            toggleOption('qualityGroup', noQuality);
            toggleOption('artistOption', noMetadata);
            toggleOption('coverOption', noMetadata);
            toggleOption('lyricsOption', noMetadata);
            toggleOption('lyricsHint', noMetadata);
        }

        document.getElementById('convertForm').addEventListener('submit', startConversion);
        buildDropdown();
        buildQualityDropdown();
        updateOptionVisibility();
    </script>
</body>
</html>
"""

# ---- ROUTES ----

@app.route('/')
def home():
    return render_template_string(HTML_PAGE)

def _cleanup_old_tasks():
    """Remove completed tasks older than 60 seconds to prevent memory leaks."""
    now = time.time()
    expired = [tid for tid, data in task_progress.items()
               if data.get('percent', 0) >= 100 and data.get('status') == 'Complete'
               and now - data.get('_completed_at', now) > 60]
    for tid in expired:
        del task_progress[tid]


@app.route('/progress/<task_id>')
def progress(task_id):
    def generate():
        while True:
            prog = task_progress.get(task_id, {"status": "Waiting...", "percent": 0, "song": "", "artist": "", "thumbnail": ""})
            yield f"data: {json.dumps(prog)}\n\n"
            if prog.get("percent", 0) >= 100 and prog.get("status") == "Complete":
                if '_completed_at' not in task_progress.get(task_id, {}):
                    task_progress.setdefault(task_id, {})['_completed_at'] = time.time()
                _cleanup_old_tasks()
                break
            time.sleep(0.4)
    return Response(generate(), mimetype='text/event-stream')


@app.route('/open_folder', methods=['POST'])
def open_folder():
    file_path = request.form.get('file_path', '')
    # SECURITY: use list-form subprocess to prevent shell injection
    safe = os.path.normpath(file_path)
    if safe and os.path.exists(safe) and not any(c in safe for c in ('&', '|', ';', '$', '`', '(', ')')):
        subprocess.run(['explorer', '/select,', safe])
    return "", 204

# ---- MAIN CONVERT ROUTE ----

@app.route('/convert', methods=['POST'])
def convert():
    url           = request.form.get('url', '').strip()
    fmt           = request.form.get('format')
    quality       = request.form.get('quality', '320k') or '320k'
    is_playlist   = request.form.get('is_playlist')
    want_artist   = request.form.get('want_artist')
    want_cover    = request.form.get('want_cover')
    fetch_lyrics  = request.form.get('fetch_lyrics')
    task_id       = request.form.get('task_id', 'default')
    download_folder = os.path.join(os.path.expanduser("~"), "Downloads")

    # Validate URL
    if not url or not re.match(r'^https?://', url):
        return error_page("Invalid URL. Please paste a valid YouTube, SoundCloud, or Spotify link.")

    task_progress[task_id] = {"status": "Starting...", "percent": 0, "song": "", "artist": "", "thumbnail": ""}

    # ==== PLAYLIST MODE ====
    if is_playlist:
        return _convert_playlist(url, fmt, task_id, download_folder,
                                 want_lyrics=bool(fetch_lyrics), want_artist=bool(want_artist), want_cover=bool(want_cover),
                                 quality=quality)

    # ==== SINGLE TRACK MODE ====
    return _convert_single(url, fmt, task_id, download_folder,
                           want_lyrics=bool(fetch_lyrics), want_artist=bool(want_artist), want_cover=bool(want_cover),
                           quality=quality)


# ---- PLAYLIST CONVERTER (with per-track metadata + lyrics) ----

def _convert_playlist(url, fmt, task_id, download_folder, want_lyrics=False, want_artist=True, want_cover=True, quality='320k'):
    batch_folder = os.path.join(download_folder, "sonic_batch_temp")
    os.makedirs(batch_folder, exist_ok=True)
    for f in glob.glob(os.path.join(batch_folder, "*")):
        try: os.remove(f)
        except: pass

    cookie_path      = os.path.join(os.environ.get('LOCALAPPDATA',''), r"BraveSoftware\Brave-Browser\User Data\Default\Network\Cookies")
    temp_cookie_path = os.path.join(download_folder, "temp_playlist_cookies")

    opts = {
        'format':         'bestaudio/best',
        'outtmpl':        os.path.join(batch_folder, '%(playlist_title)s', '%(title)s.%(ext)s'),
        'noplaylist':     False,
        'quiet':          False,
        'ignoreerrors':   True,
        'writethumbnail': want_cover,
        'writeinfojson':  True,
        'extractor_args': {'youtube': {'player_client': ['android']}},
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
    }

    if os.path.exists(cookie_path):
        try:
            shutil.copyfile(cookie_path, temp_cookie_path)
            opts['cookiefile'] = temp_cookie_path
        except OSError:
            pass

    try:
        task_progress[task_id] = {"status": "Fetching playlist...", "percent": 5, "song": "", "artist": "", "thumbnail": ""}
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        if os.path.exists(temp_cookie_path):
            try: os.remove(temp_cookie_path)
            except OSError: pass
        return error_page(f"Playlist Download Failed.<br>{str(e)}")

    if os.path.exists(temp_cookie_path):
        try: os.remove(temp_cookie_path)
        except OSError: pass

    # Walk downloaded files and convert with per-track metadata
    audio_extensions = {'.webm', '.m4a', '.opus', '.mp3', '.ogg', '.wav', '.flac', '.wma', '.mp2', '.aac', '.mkv', '.ts'}
    converted_count = 0
    total_found = 0
    lyrics_found = 0
    final_playlist_path = ""
    last_artist = ""
    last_thumbnail = ""

    # First pass: count audio files
    for root, dirs, files in os.walk(batch_folder):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in audio_extensions:
                total_found += 1

    # Second pass: convert each track with metadata
    for root, dirs, files in os.walk(batch_folder):
        for file in files:
            ext = os.path.splitext(file)[1].lower()

            # Skip non-audio files (json, images, etc.)
            if ext not in audio_extensions:
                continue

            input_path = os.path.join(root, file)
            name_no_ext = os.path.splitext(file)[0]
            rel_path    = os.path.relpath(root, batch_folder)
            final_folder = os.path.join(download_folder, rel_path)
            os.makedirs(final_folder, exist_ok=True)
            final_playlist_path = final_folder

            out_ext     = get_output_extension(fmt)
            output_file = os.path.join(final_folder, f"{name_no_ext}.{out_ext}")

            # --- Read metadata from .info.json (all fields optional) ---
            track_title    = name_no_ext
            track_artist   = ""
            track_album    = ""
            track_duration = None
            track_thumbnail = ""

            json_file = os.path.join(root, name_no_ext + '.info.json')
            meta = read_track_metadata(json_file)
            if meta:
                track_title    = meta.get('title', '') or name_no_ext
                track_artist   = meta.get('artist', '') or ""
                track_album    = meta.get('album', '') or ""
                track_duration = meta.get('duration') or None
                track_thumbnail = meta.get('thumbnail', '') or ""
                if track_artist:
                    last_artist = track_artist
                if track_thumbnail:
                    last_thumbnail = track_thumbnail

            # --- Find downloaded thumbnail file (only if user wants cover) ---
            cover_file = None
            if want_cover:
                cover_file = find_thumbnail_for_track(root, name_no_ext)
                # If no local thumbnail but we have a URL, download it
                if not cover_file and track_thumbnail:
                    temp_cover = os.path.join(root, f"{name_no_ext}_dl_cover.jpg")
                    cover_file = download_thumbnail(track_thumbnail, temp_cover)

            # --- Update progress with rich metadata ---
            task_progress[task_id] = {
                "status":    f"Converting {converted_count+1}/{total_found}...",
                "percent":   50 + int(50 * converted_count / max(total_found, 1)),
                "song":      track_title,
                "artist":    track_artist,
                "thumbnail": track_thumbnail or last_thumbnail,
            }

            # --- Build safe title ---
            safe_title = "".join([c for c in track_title if c.isalpha() or c.isdigit() or c in ' -_']).strip()
            if safe_title:
                output_file = os.path.join(final_folder, f"{safe_title}.{out_ext}")

            try:
                cmd = build_conversion_command(
                    fmt, input_path, output_file,
                    title=safe_title or name_no_ext if want_artist else "",
                    artist=track_artist if want_artist else "",
                    album=track_album if want_artist else "",
                    cover_path=cover_file if want_cover else None,
                    quality=quality
                )
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                converted_count += 1

                # Save cover art alongside for formats that can't embed it (only if user wants cover)
                if want_cover and fmt not in ALL_COVER_FORMATS:
                    if cover_file:
                        save_cover_alongside(cover_file, output_file)
                    elif track_thumbnail:
                        temp_dl = os.path.join(download_folder, "temp_playlist_cover.jpg")
                        dl_path = download_thumbnail(track_thumbnail, temp_dl)
                        if dl_path:
                            save_cover_alongside(dl_path, output_file)
                            try: os.remove(dl_path)
                            except OSError: pass

                # --- Fetch lyrics (optional) ---
                if want_lyrics:
                    lrclib_result = fetch_lyrics_from_lrclib(
                        track_name=track_title,
                        artist_name=track_artist,
                        album_name=track_album,
                        duration=track_duration,
                    )
                    if lrclib_result:
                        synced = lrclib_result.get('syncedLyrics') or ''
                        plain  = lrclib_result.get('plainLyrics') or ''
                        if save_lyrics_alongside(output_file, synced_lyrics=synced, plain_lyrics=plain):
                            lyrics_found += 1

            except Exception as e:
                print(f"  [fail] Could not convert: {file} - {e}")

    # Cleanup batch folder
    try: shutil.rmtree(batch_folder)
    except OSError: pass

    # Build completion message
    extra = ""
    if want_lyrics and lyrics_found > 0:
        extra = f"<br><small style='color:#888;'>Lyrics (.lrc) found for {lyrics_found}/{converted_count} tracks</small>"

    task_progress[task_id] = {
        "status": "Complete", "percent": 100,
        "song": "", "artist": last_artist, "thumbnail": last_thumbnail
    }
    return success_page(fmt, f"{converted_count} of {total_found} items", final_playlist_path,
                        is_playlist=True, artist=last_artist, thumbnail=last_thumbnail, extra_html=extra)


# ---- SINGLE TRACK CONVERTER ----

def _convert_single(url, fmt, task_id, download_folder, want_lyrics=False, want_artist=True, want_cover=True, quality='320k'):
    temp_filename = "temp_audio"
    temp_path     = os.path.join(download_folder, temp_filename)
    for f in glob.glob(temp_path + "*"):
        try: os.remove(f)
        except OSError: pass

    download_success = False
    source_ext       = ""
    video_title      = "Converted_Audio"
    song_artist      = ""
    song_album       = ""
    song_duration    = None
    thumbnail_url    = ""

    # ==== SPOTIFY ====
    if "spotify.com" in url:
        print("[spotify] Detected. Resolving...")
        task_progress[task_id] = {"status": "Resolving Spotify link...", "percent": 5, "song": "", "artist": "", "thumbnail": ""}
        video_title, song_artist, song_album, thumbnail_url = resolve_spotify_link(url)
        video_title = video_title.replace("ytsearch1:", "").replace(" audio", "")

        # Push metadata to loader immediately
        task_progress[task_id] = {
            "status":    "Fetching from Spotify...",
            "percent":   10,
            "song":      video_title,
            "artist":    song_artist,
            "thumbnail": thumbnail_url,
        }

        if download_spotify_via_api(url, temp_path):
            download_success = True
            source_ext = ""
        else:
            print("[spotify] Cobalt APIs failed, falling back to YouTube search...")
            url = f"ytsearch1:{video_title} audio"

    # ==== YOUTUBE BRUTE FORCE ====
    if not download_success:
        strategies = [
            {"name": "Android",       "args": {'extractor_args': {'youtube': {'player_client': ['android']}}}},
            {"name": "TV Embedded",   "args": {'extractor_args': {'youtube': {'player_client': ['tv_embedded']}}}},
            {"name": "Android VR",    "args": {'extractor_args': {'youtube': {'player_client': ['android_vr']}}}},
            {"name": "Android Music", "args": {'extractor_args': {'youtube': {'player_client': ['android_music']}}}},
            {"name": "Brave Cookies", "args": {}, "use_cookies": True},
        ]

        for strat in strategies:
            try:
                print(f"[yt] Trying strategy: {strat['name']}...")
                task_progress[task_id] = {
                    **task_progress.get(task_id, {}),
                    "status":  f"Connecting ({strat['name']})...",
                    "percent": 8,
                }

                opts = {
                    'format':   'bestaudio/best',
                    'outtmpl':  temp_path,
                    'noplaylist': True,
                    'quiet':    True,
                    'progress_hooks': [lambda d: progress_hook(d, task_id)],
                }
                opts.update(strat.get('args', {}))

                if strat.get('use_cookies'):
                    cookie_path      = os.path.join(os.environ.get('LOCALAPPDATA',''), r"BraveSoftware\Brave-Browser\User Data\Default\Network\Cookies")
                    temp_cookie_path = os.path.join(download_folder, "temp_brave_cookies")
                    if os.path.exists(cookie_path):
                        shutil.copyfile(cookie_path, temp_cookie_path)
                        opts['cookiefile'] = temp_cookie_path

                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if 'entries' in info: info = info['entries'][0]

                    # Grab rich metadata from yt-dlp (all optional)
                    if not thumbnail_url:
                        thumbnails = info.get('thumbnails') or []
                        if thumbnails:
                            thumbnails.sort(key=lambda t: t.get('width', 0) or 0, reverse=True)
                            thumbnail_url = thumbnails[0].get('url', '') or info.get('thumbnail', '')
                        else:
                            thumbnail_url = info.get('thumbnail', '')
                    if not song_artist:
                        song_artist = (info.get('artist') or info.get('uploader') or info.get('creator') or info.get('channel') or '')
                    if not song_album:
                        song_album = info.get('album') or ''
                    if not song_duration:
                        song_duration = info.get('duration') or None
                    if "spotify.com" not in request.form.get('url', ''):
                        video_title = info.get('title', 'audio')

                    # Push to progress so loader shows it
                    task_progress[task_id] = {
                        **task_progress.get(task_id, {}),
                        "song":      video_title,
                        "artist":    song_artist,
                        "thumbnail": thumbnail_url,
                    }

                if strat.get('use_cookies') and os.path.exists(temp_cookie_path):
                    try: os.remove(temp_cookie_path)
                    except OSError: pass

                if glob.glob(temp_path + "*"):
                    download_success = True
                    break

            except Exception as e:
                print(f"  [yt] Strategy {strat['name']} failed: {e}")
                continue

        # ==== SOUNDCLOUD FALLBACK ====
        if not download_success:
            print("[sc] YouTube blocked. Trying SoundCloud fallback...")
            try:
                search_query = video_title if "spotify.com" in request.form.get('url', '') else url
                return fallback_search(search_query, {'format':'bestaudio/best','outtmpl':temp_path,'noplaylist':True}, fmt, download_folder, task_id, want_lyrics=want_lyrics, want_artist=want_artist, want_cover=want_cover, quality=quality)
            except Exception as e:
                return error_page(f"Mission Failed.<br>{str(e)}")

    # ==== CONVERSION ====
    safe_title = "".join([c for c in video_title if c.isalpha() or c.isdigit() or c in ' -_']).strip()
    out_ext    = get_output_extension(fmt)

    if not source_ext:
        found = detect_downloaded_file(temp_path)
        if found:
            source_ext = os.path.splitext(found)[1]

    input_file  = temp_path + source_ext
    output_file = os.path.join(download_folder, f"{safe_title}.{out_ext}")

    # Download thumbnail for embedding (only if user wants cover)
    cover_path = None
    if want_cover and thumbnail_url:
        cover_path = download_thumbnail(thumbnail_url, os.path.join(download_folder, "temp_cover.jpg"))

    ffmpeg_cmd = build_conversion_command(fmt, input_file, output_file,
                                          title=safe_title if want_artist else "",
                                          artist=song_artist if want_artist else "",
                                          album=song_album if want_artist else "",
                                          cover_path=cover_path,
                                          quality=quality)

    try:
        print(f"[ffmpeg] Converting to {fmt}...")
        task_progress[task_id] = {
            "status":    f"Converting to {fmt.upper()}...",
            "percent":   92,
            "song":      safe_title,
            "artist":    song_artist,
            "thumbnail": thumbnail_url,
        }
        subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Save cover alongside for formats that can't embed it (only if user wants cover)
        if want_cover and fmt not in ALL_COVER_FORMATS and cover_path:
            save_cover_alongside(cover_path, output_file)

        # --- Fetch lyrics (optional) ---
        lyrics_extra = ""
        if want_lyrics:
            task_progress[task_id] = {
                **task_progress.get(task_id, {}),
                "status": "Fetching lyrics...",
                "percent": 96,
            }
            lrclib_result = fetch_lyrics_from_lrclib(
                track_name=video_title,
                artist_name=song_artist if want_artist else "",
                album_name=song_album if want_artist else "",
                duration=song_duration,
            )
            if lrclib_result:
                synced = lrclib_result.get('syncedLyrics') or ''
                plain  = lrclib_result.get('plainLyrics') or ''
                if save_lyrics_alongside(output_file, synced_lyrics=synced, plain_lyrics=plain):
                    lyrics_extra = "<br><small style='color:#888;'>Lyrics (.lrc) saved</small>"

        task_progress[task_id] = {"status": "Complete", "percent": 100, "song": safe_title, "artist": song_artist if want_artist else "", "thumbnail": thumbnail_url if want_cover else ""}
    except Exception as e:
        return error_page(f"<b>Conversion Failed.</b><br>FFmpeg Error.<br><small>{str(e)}</small>")
    finally:
        try: os.remove(input_file)
        except OSError: pass
        if cover_path and os.path.exists(cover_path):
            try: os.remove(cover_path)
            except OSError: pass

    return success_page(out_ext, safe_title, output_file, artist=song_artist if want_artist else "", thumbnail=thumbnail_url if want_cover else "", extra_html=lyrics_extra)


# ---- SOUNDCLOUD FALLBACK (with metadata + lyrics) ----

def fallback_search(original_url, options, fmt, folder, task_id='default', want_lyrics=False, want_artist=True, want_cover=True, quality='320k'):
    if "ytsearch1:" in original_url:
        title = original_url.split(":", 1)[1].replace(" audio", "").strip()
    else:
        try:
            with yt_dlp.YoutubeDL({'quiet': True, 'ignoreerrors': True}) as ydl:
                info  = ydl.extract_info(original_url, download=False)
                title = info.get('title', '')
        except Exception:
            title = ""
    if not title: raise Exception("Could not find title for SoundCloud fallback.")

    clean = clean_title_for_search(title)

    # Extract artist and thumbnail from SoundCloud search results (all optional)
    track_artist   = ""
    track_thumbnail = ""
    track_album    = ""
    track_duration = None
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'ignoreerrors': True}) as ydl:
            sc_info = ydl.extract_info(f"scsearch1:{clean}", download=False)
            if sc_info and 'entries' in sc_info and sc_info['entries']:
                entry = sc_info['entries'][0]
                track_artist = entry.get('artist') or entry.get('uploader') or ''
                track_album  = entry.get('album') or ''
                track_duration = entry.get('duration') or None
                thumbs = entry.get('thumbnails') or []
                if thumbs:
                    thumbs.sort(key=lambda t: t.get('width', 0) or 0, reverse=True)
                    track_thumbnail = thumbs[0].get('url', '') or entry.get('thumbnail', '')
                else:
                    track_thumbnail = entry.get('thumbnail', '')
                print(f"[sc] Found: {track_artist} - {title}")
    except Exception as e:
        print(f"[sc] Metadata extraction failed: {e}")

    sc_temp = os.path.join(folder, "sc_temp.mp3")
    options.update({'outtmpl': sc_temp, 'progress_hooks': [lambda d: progress_hook(d, task_id)]})

    task_progress[task_id] = {
        "status":    "Searching SoundCloud...",
        "percent":   20,
        "song":      title,
        "artist":    track_artist,
        "thumbnail": track_thumbnail,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([f"scsearch1:{clean}"])

    safe_title  = "".join([c for c in title if c.isalpha() or c.isdigit() or c in ' -_']).strip()
    out_ext     = get_output_extension(fmt)
    output_file = os.path.join(folder, f"{safe_title}.{out_ext}")

    # Download thumbnail (only if user wants cover)
    cover_path = None
    if want_cover and track_thumbnail:
        cover_path = download_thumbnail(track_thumbnail, os.path.join(folder, "temp_sc_cover.jpg"))

    cmd = build_conversion_command(fmt, sc_temp, output_file, title=safe_title if want_artist else "",
                                   artist=track_artist if want_artist else "", album=track_album if want_artist else "",
                                   cover_path=cover_path, quality=quality)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Save cover alongside for formats that can't embed it (only if user wants cover)
    if want_cover and fmt not in ALL_COVER_FORMATS and cover_path:
        save_cover_alongside(cover_path, output_file)

    # --- Fetch lyrics (optional) ---
    lyrics_extra = ""
    if want_lyrics:
        task_progress[task_id] = {
            **task_progress.get(task_id, {}),
            "status": "Fetching lyrics...",
            "percent": 96,
        }
        lrclib_result = fetch_lyrics_from_lrclib(
            track_name=title,
            artist_name=track_artist if want_artist else "",
            album_name=track_album if want_artist else "",
            duration=track_duration,
        )
        if lrclib_result:
            synced = lrclib_result.get('syncedLyrics') or ''
            plain  = lrclib_result.get('plainLyrics') or ''
            if save_lyrics_alongside(output_file, synced_lyrics=synced, plain_lyrics=plain):
                lyrics_extra = "<br><small style='color:#888;'>Lyrics (.lrc) saved</small>"

    try: os.remove(sc_temp)
    except OSError: pass
    if cover_path and os.path.exists(cover_path):
        try: os.remove(cover_path)
        except OSError: pass

    task_progress[task_id] = {
        "status": "Complete", "percent": 100,
        "song": safe_title, "artist": track_artist if want_artist else "", "thumbnail": track_thumbnail if want_cover else ""
    }
    return success_page(out_ext, safe_title, output_file, artist=track_artist if want_artist else "", thumbnail=track_thumbnail if want_cover else "", extra_html=lyrics_extra)


# ---- RESULT PAGES ----

def success_page(fmt, title, full_path, is_playlist=False, artist="", thumbnail="", extra_html=""):
    thumb_html  = f'<img src="{thumbnail}" style="width:140px;height:140px;object-fit:cover;border-radius:14px;margin-bottom:16px;box-shadow:0 8px 24px rgba(0,0,0,0.6);">' if thumbnail else ""
    artist_html = f'<p style="color:#888;font-size:13px;margin-bottom:16px;">{artist}</p>' if artist else ""

    msg = f"Converted <b>{title}</b> items." if is_playlist else f"Saved <b>{title}.{fmt}</b> to Downloads."

    return f"""
    <body style="background:#0f0f0f;color:#fff;font-family:'Outfit',sans-serif;text-align:center;padding:50px;min-height:100vh;display:flex;align-items:center;justify-content:center;">
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;500;700&display=swap" rel="stylesheet">
        <div style="background:#1c1c1c;padding:40px;border-radius:24px;border:1px solid #2a2a2a;max-width:480px;width:100%;box-shadow:0 30px 60px -15px rgba(0,0,0,0.8);">
            {thumb_html}
            <h2 style="color:#00e676;margin-bottom:6px;font-size:22px;">Done!</h2>
            {artist_html}
            <p style="color:#ccc;margin-bottom:28px;font-size:14px;">{msg}{extra_html}</p>
            <div style="display:flex;gap:10px;justify-content:center;">
                <a href="/" style="text-decoration:none;">
                    <button style="padding:14px 22px;background:#ff0033;color:white;border:none;border-radius:12px;font-weight:bold;font-size:14px;cursor:pointer;width:auto;text-transform:uppercase;letter-spacing:1px;">Convert Another</button>
                </a>
                <form action="/open_folder" method="post" target="hiddenFrame" style="margin:0;">
                    <input type="hidden" name="file_path" value="{full_path}">
                    <button type="submit" style="padding:14px 22px;background:#252525;color:white;border:1px solid #444;border-radius:12px;font-weight:bold;font-size:14px;cursor:pointer;width:auto;">Show in Files</button>
                </form>
            </div>
            <iframe name="hiddenFrame" style="display:none;"></iframe>
        </div>
    </body>
    """

def error_page(msg):
    return f"""
    <body style="background:#0f0f0f;color:#fff;font-family:sans-serif;text-align:center;padding:50px;">
        <h2 style="color:#ff0033;">Error</h2>
        <p style="color:#ccc;margin:16px 0;">{msg}</p>
        <a href="/"><button style="padding:10px 20px;cursor:pointer;background:#333;color:white;border:none;border-radius:8px;">Go Back</button></a>
    </body>
    """

def open_browser():
    webbrowser.open_new("http://127.0.0.1:5000")

if __name__ == '__main__':
    threading.Timer(1, open_browser).start()
    app.run(port=5000, threaded=True)
