"""
سيرفر بسيط يستخدم yt-dlp لتحليل روابط الفيديو (يوتيوب، تيك توك، انستكرام،
تويتر/X، فيسبوك ...) وإرجاع رابط تحميل مباشر، بالإضافة لدعم بلي لست يوتيوب
مباشرة من yt-dlp نفسه (بدون حاجة لمفتاح YouTube Data API).

⚠️ استخدم هذا السيرفر بس لتحميل محتوى تملك حقوقه أو مسموح تحميله.
"""

import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

API_SECRET = os.environ.get("API_SECRET", "")  # لو فاضي، ما يسوي تحقق (غير مستحسن بالإنتاج)

app = FastAPI(title="yt-dlp resolver")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdlp_out"
DOWNLOAD_DIR.mkdir(exist_ok=True)


def check_auth(x_api_key: Optional[str]):
    if API_SECRET and x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="مفتاح API غير صحيح")


class ResolveBody(BaseModel):
    url: str
    quality: str = "720"  # 360 | 480 | 720 | 1080 | 1440 | 2160
    audio_only: bool = False


class PlaylistBody(BaseModel):
    url: str


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/resolve")
def resolve(body: ResolveBody, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)

    if body.audio_only:
        fmt = "bestaudio/best"
    else:
        h = "".join(ch for ch in body.quality if ch.isdigit()) or "720"
        # نفضل صيغة progressive (فيديو+صوت بملف وحد) عشان نعطي رابط مباشر
        # بدون حاجة لدمج (mux) على السيرفر، وهذا يخليها أسرع بكثير.
        fmt = f"best[height<={h}][ext=mp4]/best[height<={h}]/best"

    ydl_opts = {
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(body.url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"تعذر تحليل الرابط: {e}")

    direct_url = info.get("url")
    # لو الصيغة المختارة فصلت فيديو عن صوت (requested_formats)، نحتاج ندمجها
    # عبر /download بدل ما نرجع رابط مباشر ناقص صوت.
    needs_merge = not direct_url and info.get("requested_formats")

    return {
        "ok": True,
        "title": info.get("title"),
        "ext": info.get("ext"),
        "resolution": info.get("resolution") or info.get("format_note"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "direct_url": direct_url,
        "needs_merge": bool(needs_merge),
    }


@app.post("/download")
def download(body: ResolveBody, x_api_key: Optional[str] = Header(None)):
    """
    يستخدم لما /resolve يرجع needs_merge=true — يحمّل الفيديو والصوت
    ويدمجهم بـ ffmpeg على السيرفر، وبعدين يسلّم الملف مباشرة للمستخدم.
    أبطأ من /resolve لأنه يعالج الملف فعلياً.
    """
    check_auth(x_api_key)

    h = "".join(ch for ch in body.quality if ch.isdigit()) or "720"
    fmt = "bestaudio/best" if body.audio_only else f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"

    out_id = uuid.uuid4().hex
    out_template = str(DOWNLOAD_DIR / f"{out_id}.%(ext)s")

    ydl_opts = {
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": out_template,
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(body.url, download=True)
            filepath = ydl.prepare_filename(info)
            if body.audio_only:
                # عند استخراج صوت فقط، الامتداد النهائي يمكن يختلف
                p = Path(filepath)
                candidates = list(DOWNLOAD_DIR.glob(f"{out_id}.*"))
                if candidates:
                    filepath = str(candidates[0])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"تعذر تحميل الرابط: {e}")

    if not os.path.exists(filepath):
        raise HTTPException(status_code=500, detail="فشل إنشاء الملف")

    filename = f"{info.get('title', 'video')}.{Path(filepath).suffix.lstrip('.')}"
    return FileResponse(filepath, filename=filename, background=None)


@app.post("/playlist")
def playlist(body: PlaylistBody, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(body.url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"تعذر تحليل البلي لست: {e}")

    entries = info.get("entries") or []
    items = [
        {
            "videoId": e.get("id"),
            "title": e.get("title"),
            "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url") if e.get("thumbnails") else None,
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
        }
        for e in entries
        if e
    ]

    return {"ok": True, "count": len(items), "items": items}
