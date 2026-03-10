from __future__ import annotations
# tmdb_client.py

import os
import json
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple

import requests

TMDB_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"


CastItem = Tuple[int, str, str, Optional[str]]   # (tmdb_person_id, name, role, profile_path)
CrewItem = Tuple[int, str, str, Optional[str]]   # (tmdb_person_id, name, job, profile_path)
GenreItem = Tuple[int, str]                      # (tmdb_genre_id, name)


@dataclass
class TMDBMovie:
    tmdb_id: int
    title: str
    original_title: str
    year: Optional[int]
    overview: str
    poster_path: Optional[str]
    backdrop_path: Optional[str]

    genres: List[GenreItem]
    runtime: Optional[int]
    original_language: Optional[str]
    production_countries: List[str]

    cast: List[CastItem]            # top 12
    crew: List[CrewItem]            # director/producer/writer/musician

    studio: str
    tmdb_rating: Optional[float]
    tmdb_votes: Optional[int]


class TMDBClient:
    def __init__(self, api_key: str, cache_dir: str = "cache"):
        self.api_key = api_key
        self.session = requests.Session()

        self.poster_dir = os.path.join(cache_dir, "posters")
        self.backdrop_dir = os.path.join(cache_dir, "backdrops")
        self.profile_dir = os.path.join(cache_dir, "profiles")
        self.thumb_dir = os.path.join(cache_dir, "tmdb_thumbs")  # fallback dialog thumbs
        self.json_dir = os.path.join(cache_dir, "tmdb_json")

        os.makedirs(self.poster_dir, exist_ok=True)
        os.makedirs(self.backdrop_dir, exist_ok=True)
        os.makedirs(self.profile_dir, exist_ok=True)
        os.makedirs(self.thumb_dir, exist_ok=True)
        os.makedirs(self.json_dir, exist_ok=True)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = dict(params or {})
        params["api_key"] = self.api_key
        url = f"{TMDB_BASE}{path}"
        r = self.session.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def search_movie(self, title: str, year: Optional[int] = None, language: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = year
        if language:
            params["language"] = language
        data = self._get("/search/movie", params=params)
        return data.get("results", []) or []

    @staticmethod
    def _norm_title(s: str) -> str:
        s = (s or "").lower().strip()
        for ch in [":", "-", "_", ".", ",", "’", "'"]:
            s = s.replace(ch, " ")
        s = " ".join(s.split())
        for art in ("the ", "a ", "an "):
            if s.startswith(art):
                s = s[len(art):]
                break
        return s

    def pick_best(self, results: List[Dict[str, Any]], want_title: str, want_year: Optional[int]) -> Optional[Dict[str, Any]]:
        if not results:
            return None
        wt = self._norm_title(want_title)

        def score(item: Dict[str, Any]) -> float:
            t = self._norm_title(item.get("title") or item.get("original_title") or "")
            rd = item.get("release_date") or ""
            oy = int(rd[:4]) if len(rd) >= 4 and rd[:4].isdigit() else None

            wt_set = set(wt.split())
            t_set = set(t.split())
            overlap = len(wt_set & t_set)
            base = overlap / max(1, len(wt_set))

            y_bonus = 0.0
            if want_year and oy:
                diff = abs(want_year - oy)
                y_bonus = max(0.0, 1.0 - (diff / 5.0))

            pop = float(item.get("popularity") or 0.0)
            return base * 4.0 + y_bonus * 2.0 + min(pop / 50.0, 1.0)

        return max(results, key=score)

    def get_movie_details(self, tmdb_id: int, language: Optional[str] = None) -> TMDBMovie:
        cache_path = os.path.join(self.json_dir, f"movie_{tmdb_id}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._to_movie(data)
            except Exception:
                pass

        params = {"append_to_response": "credits"}
        if language:
            params["language"] = language
        data = self._get(f"/movie/{tmdb_id}", params=params)

        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

        return self._to_movie(data)

    def _to_movie(self, data: Dict[str, Any]) -> TMDBMovie:
        tmdb_id = int(data["id"])
        title = data.get("title") or ""
        original_title = data.get("original_title") or title
        rd = data.get("release_date") or ""
        year = int(rd[:4]) if len(rd) >= 4 and rd[:4].isdigit() else None
        overview = data.get("overview") or ""
        poster_path = data.get("poster_path")
        backdrop_path = data.get("backdrop_path")

        genres: List[GenreItem] = []
        for g in (data.get("genres") or []):
            gid = g.get("id")
            name = g.get("name")
            if isinstance(gid, int) and name:
                genres.append((int(gid), str(name)))

        runtime = data.get("runtime")
        runtime = runtime if isinstance(runtime, int) else None

        original_language = data.get("original_language")
        production_countries = [
            c.get("iso_3166_1", "")
            for c in (data.get("production_countries") or [])
            if c.get("iso_3166_1")
        ]

        credits = data.get("credits") or {}
        cast_items = (credits.get("cast") or [])[:12]
        cast: List[CastItem] = []
        for c in cast_items:
            pid = c.get("id")
            name = c.get("name") or ""
            character = c.get("character") or ""
            profile = c.get("profile_path")
            if isinstance(pid, int) and name:
                cast.append((int(pid), name, character, profile))

        wanted_jobs = {"director", "producer", "writer", "original music composer"}
        crew_items = (credits.get("crew") or [])
        crew: List[CrewItem] = []
        for c in crew_items:
            job = (c.get("job") or "").strip()
            low = job.lower()
            if low not in wanted_jobs:
                continue
            pid = c.get("id")
            name = c.get("name") or ""
            profile = c.get("profile_path")
            if not (isinstance(pid, int) and name):
                continue

            if low == "original music composer":
                norm_job = "musician"
            else:
                norm_job = low

            crew.append((int(pid), name, norm_job, profile))

        companies = data.get("production_companies") or []
        studio = companies[0].get("name", "") if companies else ""

        tmdb_rating = data.get("vote_average")
        tmdb_votes = data.get("vote_count")
        try:
            tmdb_rating = float(tmdb_rating) if tmdb_rating is not None else None
        except Exception:
            tmdb_rating = None
        try:
            tmdb_votes = int(tmdb_votes) if tmdb_votes is not None else None
        except Exception:
            tmdb_votes = None

        return TMDBMovie(
            tmdb_id=tmdb_id,
            title=title,
            original_title=original_title,
            year=year,
            overview=overview,
            poster_path=poster_path,
            backdrop_path=backdrop_path,
            genres=genres,
            runtime=runtime,
            original_language=original_language,
            production_countries=production_countries,
            cast=cast,
            crew=crew,
            studio=studio,
            tmdb_rating=tmdb_rating,
            tmdb_votes=tmdb_votes,
        )

    def _download_image(self, path: Optional[str], local_dir: str, size: str) -> Optional[str]:
        if not path:
            return None
        safe = path.strip("/").replace("/", "_")
        local = os.path.join(local_dir, f"{size}_{safe}")
        if os.path.exists(local) and os.path.getsize(local) > 0:
            return local
        url = f"{IMG_BASE}/{size}{path}"
        try:
            r = self.session.get(url, timeout=25)
            r.raise_for_status()
            with open(local, "wb") as f:
                f.write(r.content)
            return local
        except Exception:
            return None

    def poster_file(self, poster_path: Optional[str], size: str = "w342") -> Optional[str]:
        return self._download_image(poster_path, self.poster_dir, size)

    def backdrop_file(self, backdrop_path: Optional[str], size: str = "w780") -> Optional[str]:
        return self._download_image(backdrop_path, self.backdrop_dir, size)

    def profile_file(self, profile_path: Optional[str], size: str = "w185") -> Optional[str]:
        return self._download_image(profile_path, self.profile_dir, size)

    # default now w64 (per your request)
    def poster_thumb_file(self, poster_path: Optional[str], size: str = "w92") -> Optional[str]:
        return self._download_image(poster_path, self.thumb_dir, size)


__all__ = ["TMDBClient", "TMDBMovie"]
