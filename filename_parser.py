# filename_parser.py
# Robust filename parsing utilities for Movie Collection App

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Optional

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm"
}

@dataclass(frozen=True)
class ParsedMovieName:
    title: str
    year: Optional[int] = None
    part_index: Optional[int] = None
    raw: str = ""
    cleaned: str = ""

def is_video_file(path: str) -> bool:
    """Return True if `path` looks like a supported video file."""
    try:
        ext = os.path.splitext(path)[1].lower()
    except Exception:
        return False
    return ext in VIDEO_EXTS


_NOISE_WORDS = {
    # sources / formats
    "bluray", "brrip", "bdrip", "bdremux", "dvdrip", "dvd", "hdrip", "webrip", "web", "webdl", "web-dl",
    "hdtv", "cam", "ts", "telesync", "tc", "telecine", "remux", "proper", "repack", "internal",
    "limited", "unrated", "remastered", "extended", "directors", "director", "cut",

    # codecs / containers
    "x264", "h264", "x265", "h265", "hevc", "avc", "xvid", "divx",

    # audio
    "aac", "ac3", "dd", "ddp", "dts", "truehd", "hq",

    # resolution / misc
    "2160p", "1080p", "720p", "576p", "480p", "4k", "10bit", "8bit", "hdr", "sdr",
    "dubbed", "multi", "sub", "subs",

    # common “site / junk”
    "www", "com", "net", "org", "ro",

    # languages (ενδεικτικά)
    "eng", "english", "greek", "ell", "ita", "spanish", "spa", "french", "fre", "ger", "german",
    "rus", "jpn", "japanese", "kor", "korean",

    # groups / sites
    "yify", "yts", "rarbg", "galaxyrg", "evo", "axxo", "fxg", "maxspeed", "sparks", "amiable",
    "etrg", "rmteam", "hon3y", "riprg", "rpg", "torentz", "3xforum", "bitloks", "deceit", "bokutox",
}

_DISC_RE = re.compile(r"\b(?:cd|disc|disk|part)\s*(\d)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:\(|\[|\b)(19\d{2}|20\d{2})(?:\)|\]|\b)")


def _normalize_separators(s: str) -> str:
    s = s.replace("_", " ").replace(".", " ").replace("-", " ")
    return " ".join(s.split()).strip()


def _strip_brackets(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\{[^\}]*\}", " ", s)
    return " ".join(s.split()).strip()


def _drop_leading_track_number(s: str) -> str:
    # "01 Title", "25. Spectre", "14 - Never Say Never Again" κλπ
    return re.sub(r"^\s*\d{1,3}[\.)\-\_: ]+", "", s).strip()


def _cleanup_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for t in tokens:
        tt = t.strip()
        if not tt:
            continue
        low = tt.lower()

        # σκέτη στίξη
        if re.fullmatch(r"[^\w]+", tt):
            continue

        # drop 1..9 (από 5.1 / dd5 1 κλπ) αλλά ΚΡΑΤΑΜΕ 12, 25, 300, 1917 κλπ
        if re.fullmatch(r"\d+", low):
            try:
                n = int(low)
                if 0 <= n <= 9:
                    continue
            except Exception:
                pass

        # καθαρά “noise”
        if low in _NOISE_WORDS:
            continue

        # π.χ. 1400MB
        if re.fullmatch(r"\d{3,4}mb", low):
            continue

        # π.χ. 5.1
        if re.fullmatch(r"\d\.\d", low):
            continue

        # π.χ. DD5
        if low.startswith("dd") and re.fullmatch(r"dd\d+", low):
            continue

        out.append(tt)
    return out


def parse_movie_name(filename_or_stem: str) -> ParsedMovieName:
    """Parse a filename (or stem) into a movie title + optional year.

    Στόχος: καλό query για TMDB, όχι “τέλειος” ανθρώπινος τίτλος.
    """
    raw = (filename_or_stem or "").strip()
    if not raw:
        return ParsedMovieName(title="", year=None, raw=raw, cleaned="")

    base = os.path.basename(raw)
    stem, _ext = os.path.splitext(base)

    # 1) ΠΡΩΤΑ προσπάθησε να βρεις year ΠΡΙΝ σβήσεις [] {} (για Zodiac[2007] κλπ)
    s_pre = _normalize_separators(stem)
    year: Optional[int] = None
    ym = _YEAR_RE.search(s_pre)
    if ym:
        try:
            year = int(ym.group(1))
        except Exception:
            year = None
        s_pre = (s_pre[:ym.start()] + " " + s_pre[ym.end():]).strip()

    # 2) τώρα καθάρισε brackets / separators
    s = _strip_brackets(s_pre)
    s = _normalize_separators(s)

    # part / cd
    part_index: Optional[int] = None
    dm = _DISC_RE.search(s)
    if dm:
        try:
            part_index = int(dm.group(1))
        except Exception:
            part_index = None
        s = _DISC_RE.sub(" ", s)

    s = _drop_leading_track_number(s)
    s = " ".join(s.split()).strip()

    tokens = _cleanup_tokens(s.split())
    cleaned = " ".join(tokens).strip()

    # fallback αν άδειασε
    if not cleaned:
        cleaned = _drop_leading_track_number(_normalize_separators(stem))

    title = cleaned.strip()

    # extra safety: αν έβγαλε “The” σκέτο, γύρνα σε raw-ish stem
    if title.lower() in {"the", "a", "an"}:
        title = _drop_leading_track_number(_normalize_separators(stem)).strip()

    return ParsedMovieName(title=title, year=year, part_index=part_index, raw=raw, cleaned=cleaned)
