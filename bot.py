from __future__ import annotations

import calendar
import json
import os
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==========================
# Branding / Style
# ==========================
BOT_NAME = "VRX Mídias"
FOOTER_TEXT = "Vorax eSports • #midias-vrx"
STATE_FILE = "state.json"

COLOR_YT = 0xFF0000
COLOR_IG = 0xC13584

POSTED_IDS_LIMIT = 120          # histórico curto pra evitar repost
MAX_ITEMS_HARD_CAP = 10         # segurança

BRAND_NAME = (os.getenv("BRAND_NAME", "Vorax eSports") or "Vorax eSports").strip()
YT_CHANNEL_URL = (os.getenv("YT_CHANNEL_URL", "https://youtube.com/@voraxsports") or "").strip()
IG_PROFILE_URL = (os.getenv("IG_PROFILE_URL", "https://www.instagram.com/voraxsportss/") or "").strip()

BRAND_ICON_URL = (os.getenv("BRAND_ICON_URL", "") or "").strip() or None     # ícone pequeno no embed
BOT_AVATAR_URL = (os.getenv("BOT_AVATAR_URL", "") or "").strip() or None     # avatar do webhook (topo)

# ==========================
# Logging
# ==========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("vrx-midias-bot")


# ==========================
# Helpers
# ==========================
def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def clamp_int(val: int, lo: int, hi: int) -> int:
    return max(lo, min(val, hi))


def now_iso() -> str:
    # Discord embed timestamp accepts ISO8601
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

DISCORD_TITLE_MAX = 256
DISCORD_DESC_MAX = 4096

def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

def unix_to_iso(unix_ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(unix_ts)))

# ==========================
# Config
# ==========================
@dataclass(frozen=True)
class Config:
    discord_webhook_url: str
    yt_feed_url: str

    # Instagram Graph (optional)
    ig_user_id: str
    ig_access_token: str
    ig_graph_version: str

    max_items_per_run: int
    dry_run: bool

    # Bootstrap behavior
    allow_initial_post: bool  # False = first run sync state, no spam
    allow_batch_posts: bool   # True = if many new items, allow up to MAX_ITEMS_PER_RUN


def load_config() -> Config:
    discord_webhook_url = (os.getenv("DISCORD_WEBHOOK_URL", "") or "").strip()
    yt_feed_url = (os.getenv("YT_FEED_URL", "") or "").strip()

    ig_user_id = (os.getenv("IG_USER_ID", "") or "").strip()
    ig_access_token = (os.getenv("IG_ACCESS_TOKEN", "") or "").strip()
    ig_graph_version = (os.getenv("IG_GRAPH_VERSION", "") or "").strip() or "v20.0"

    max_items_per_run = clamp_int(int(os.getenv("MAX_ITEMS_PER_RUN", "3")), 1, MAX_ITEMS_HARD_CAP)
    dry_run = env_bool("DRY_RUN", False)
    allow_initial_post = env_bool("ALLOW_INITIAL_POST", False)
    allow_batch_posts = env_bool("ALLOW_BATCH_POSTS", True)

    if not discord_webhook_url:
        raise RuntimeError("Faltou DISCORD_WEBHOOK_URL")
    if not yt_feed_url:
        raise RuntimeError("Faltou YT_FEED_URL")

    return Config(
        discord_webhook_url=discord_webhook_url,
        yt_feed_url=yt_feed_url,
        ig_user_id=ig_user_id,
        ig_access_token=ig_access_token,
        ig_graph_version=ig_graph_version,
        max_items_per_run=max_items_per_run,
        dry_run=dry_run,
        allow_initial_post=allow_initial_post,
        allow_batch_posts=allow_batch_posts,
    )


# ==========================
# State (with migration)
# ==========================
def default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "youtube_last_video_id": "",
        "instagram_last_media_id": "",
        "posted_ids": [],
        # RSS conditional fetch
        "yt_etag": None,
        "yt_modified": None,
    }


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return default_state()

    st = default_state()
    # migrate fields safely
    st["version"] = int(data.get("version", 1))
    st["youtube_last_video_id"] = str(data.get("youtube_last_video_id", "") or "")
    st["instagram_last_media_id"] = str(data.get("instagram_last_media_id", "") or "")

    posted = data.get("posted_ids", [])
    if isinstance(posted, list):
        st["posted_ids"] = [str(x) for x in posted][:POSTED_IDS_LIMIT]
    else:
        st["posted_ids"] = []

    st["yt_etag"] = data.get("yt_etag", None)
    st["yt_modified"] = data.get("yt_modified", None)

    return st


def save_state_atomic(state: Dict[str, Any]) -> None:
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def posted_has(state: Dict[str, Any], pid: str) -> bool:
    return pid in state.get("posted_ids", [])


def posted_add(state: Dict[str, Any], pid: str) -> None:
    arr: List[str] = state.get("posted_ids", [])
    arr.insert(0, pid)
    state["posted_ids"] = arr[:POSTED_IDS_LIMIT]


# ==========================
# HTTP session (retries)
# ==========================
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


# ==========================
# Discord webhook (rate limit handling)
# ==========================
def post_discord_embed(session: requests.Session, webhook_url: str, embed: Dict[str, Any]) -> None:
    payload = {
        "username": BOT_NAME,
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }
    if BOT_AVATAR_URL:
        payload["avatar_url"] = BOT_AVATAR_URL

    for attempt in range(1, 4):
        r = session.post(webhook_url, json=payload, timeout=20)

        if r.status_code == 204 or (200 <= r.status_code < 300):
            return

        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2.0))
            except Exception:
                wait = 2.0
            wait = clamp_int(int(wait if wait >= 1 else 2), 1, 30)
            log.warning(f"Discord rate-limit (429). Waiting {wait}s (attempt {attempt}/3)")
            time.sleep(wait)
            continue

        raise RuntimeError(f"Discord webhook failed: {r.status_code} | {r.text[:240]}")



# ==========================
# YouTube RSS (ETag/Modified optimized)
# ==========================
def parse_youtube_feed(
    feed_url: str,
    etag: Optional[str],
    modified: Optional[str],
) -> Tuple[List[Dict[str, str]], Optional[str], Optional[str], int]:
    kwargs: Dict[str, Any] = {}
    if etag:
        kwargs["etag"] = etag
    if modified:
        kwargs["modified"] = modified

    d = feedparser.parse(feed_url, **kwargs)

    status = int(getattr(d, "status", 200) or 200)
    new_etag = getattr(d, "etag", None)
    new_modified = getattr(d, "modified", None)

    entries_out: List[Dict[str, str]] = []

    if status == 304:
        return entries_out, new_etag or etag, new_modified or modified, status

    for e in (d.entries or []):
        link = (getattr(e, "link", "") or "").strip()
        title = (getattr(e, "title", "Vídeo novo") or "Vídeo novo").strip()

        video_id = getattr(e, "yt_videoid", None)
        if (not video_id) and ("watch?v=" in link):
            video_id = link.split("watch?v=")[-1].split("&")[0].strip()

        if not video_id:
            continue

        published_unix = 0
        pp = getattr(e, "published_parsed", None)
        if pp:
            published_unix = int(calendar.timegm(pp))

        # thumbnail mais “à prova de falha”
        thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        entries_out.append({
            "id": str(video_id),
            "title": title,
            "url": link or f"https://www.youtube.com/watch?v={video_id}",
            "thumb": thumb,
            "published_unix": published_unix,
        })

    # garante ordem newest->oldest
    entries_out.sort(key=lambda x: int(x.get("published_unix") or 0), reverse=True)

    return entries_out, new_etag or etag, new_modified or modified, status

    for e in (d.entries or []):
        link = (getattr(e, "link", "") or "").strip()
        title = (getattr(e, "title", "Vídeo novo") or "Vídeo novo").strip()
        video_id = getattr(e, "yt_videoid", None)
        published_unix = None
        pp = getattr(e, "published_parsed", None)
        if pp:
            published_unix = int(calendar.timegm(pp))


        
        if (not video_id) and ("watch?v=" in link):
            video_id = link.split("watch?v=")[-1].split("&")[0].strip()

        if not video_id:
            continue

    entries_out.append({
        "id": str(video_id),
        "title": title,
        "url": link or f"https://www.youtube.com/watch?v={video_id}",
        "thumb": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        "published_unix": published_unix,
    })


    return entries_out, new_etag or etag, new_modified or modified, status


def youtube_embed(item: Dict[str, str]) -> Dict[str, Any]:
    published_unix = int(item.get("published_unix") or 0)
    ts = unix_to_iso(published_unix) if published_unix > 0 else now_iso()

    lines = [
        "🚀 **Saiu vídeo novo!**",
        f"▶️ **[Assistir agora]({item['url']})**",
    ]
    if published_unix > 0:
        lines.append(f"🕒 Publicado <t:{published_unix}:R>")

    title = clip(f"🎥 {item['title']}", DISCORD_TITLE_MAX)
    desc = clip("\n".join(lines), DISCORD_DESC_MAX)

    emb: Dict[str, Any] = {
        "title": title,
        "url": item["url"],
        "description": desc,
        "color": COLOR_YT,
        "image": {"url": item["thumb"]},
        "footer": {"text": FOOTER_TEXT},
        "timestamp": ts,
        "fields": [
            {"name": "Plataforma", "value": "YouTube", "inline": True},
            {"name": "Canal", "value": BRAND_NAME, "inline": True},
        ],
    }

    author = {"name": BRAND_NAME}
    if YT_CHANNEL_URL:
        author["url"] = YT_CHANNEL_URL
    if BRAND_ICON_URL:
        author["icon_url"] = BRAND_ICON_URL
        emb["thumbnail"] = {"url": BRAND_ICON_URL}
    emb["author"] = author

    return emb




# ==========================
# Instagram Graph API (optional)
# ==========================
def fetch_instagram_media(
    session: requests.Session,
    version: str,
    user_id: str,
    token: str,
    limit: int = 8
) -> List[Dict[str, str]]:
    if not user_id or not token:
        return []

    url = f"https://graph.facebook.com/{version}/{user_id}/media"
    params = {
        "fields": "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp",
        "limit": str(limit),
        "access_token": token,
    }

    r = session.get(url, params=params, timeout=20)
    if r.status_code != 200:
        log.warning(f"Instagram fetch failed: {r.status_code} | {r.text[:240]}")
        return []

    items = (r.json() or {}).get("data", []) or []
    out: List[Dict[str, str]] = []

    for m in items:
        mid = str(m.get("id", "") or "").strip()
        if not mid:
            continue

        caption = (m.get("caption") or "").strip()
        if len(caption) > 220:
            caption = caption[:217] + "..."

        image = (m.get("thumbnail_url") or m.get("media_url") or "").strip()
        out.append({
            "id": mid,
            "url": (m.get("permalink") or "").strip(),
            "caption": caption if caption else "(sem legenda)",
            "media_type": (m.get("media_type") or "—").strip(),
            "image": image,
            "timestamp": (m.get("timestamp") or "").strip(),
        })

    out.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return out


def instagram_embed(item: Dict[str, str]) -> Dict[str, Any]:
    # deixa chamativo e seguro (sem estourar limite do Discord)
    caption = (item.get("caption") or "(sem legenda)").strip()
    if len(caption) > 2800:
        caption = caption[:2797] + "..."

    url = item.get("url") or ""
    desc = f"{caption}\n\n📲 **[Abrir no Instagram]({url})**"

    brand_name = (os.getenv("BRAND_NAME", "Vorax eSports") or "Vorax eSports").strip()
    ig_profile_url = (os.getenv("IG_PROFILE_URL", "https://www.instagram.com/voraxsportss/") or "").strip()
    brand_icon_url = (os.getenv("BRAND_ICON_URL", "") or "").strip()

    emb: Dict[str, Any] = {
        "title": "📸 Post novo no Instagram",
        "url": url,
        "description": desc,
        "color": COLOR_IG,
        "timestamp": item.get("timestamp") or now_iso(),
        "footer": {"text": FOOTER_TEXT},
        "fields": [
            {"name": "Tipo", "value": item.get("media_type", "—"), "inline": True},
            {"name": "Perfil", "value": f"[{brand_name}]({ig_profile_url})" if ig_profile_url else brand_name, "inline": True},
        ],
    }

    if item.get("image"):
        emb["image"] = {"url": item["image"]}

    # Author + ícone (se você setar BRAND_ICON_URL)
    author = {"name": brand_name}
    if ig_profile_url:
        author["url"] = ig_profile_url
    if brand_icon_url:
        author["icon_url"] = brand_icon_url
        emb["thumbnail"] = {"url": brand_icon_url}
    emb["author"] = author

    return emb




# ==========================
# Main logic
# ==========================
def compute_new_items(
    items_newest_first: List[Dict[str, str]],
    last_id: str
) -> List[Dict[str, str]]:
    """
    Recebe itens newest->oldest. Retorna itens 'novos' até encontrar last_id.
    """
    new_items: List[Dict[str, str]] = []
    for it in items_newest_first:
        if it["id"] == last_id:
            break
        new_items.append(it)
    return new_items


def main() -> None:
    cfg = load_config()
    state = load_state()
    session = make_session()

    changed = False

    # ---------- YouTube ----------
    yt_entries, yt_etag, yt_modified, yt_status = parse_youtube_feed(
        cfg.yt_feed_url,
        state.get("yt_etag"),
        state.get("yt_modified"),
    )

    state["yt_etag"] = yt_etag
    state["yt_modified"] = yt_modified

    if yt_status == 304:
        log.info("YouTube RSS: 304 (no changes)")
    elif not yt_entries:
        log.info(f"YouTube RSS: no entries (status={yt_status})")
    else:
        last_yt = state.get("youtube_last_video_id", "")

        # bootstrap (first run)
        if not last_yt and not cfg.allow_initial_post:
            state["youtube_last_video_id"] = yt_entries[0]["id"]
            posted_add(state, f"yt:{yt_entries[0]['id']}")
            save_state_atomic(state)
            log.info("YouTube bootstrap: synced state (no post). Set ALLOW_INITIAL_POST=1 to post on first run.")
        else:
            new_yt = compute_new_items(yt_entries, last_yt)

            if new_yt:
                # Send oldest -> newest for nicer timeline
                to_send = list(reversed(new_yt))

                if not cfg.allow_batch_posts:
                    to_send = to_send[-1:]  # only the newest
                else:
                    to_send = to_send[-cfg.max_items_per_run:]

                log.info(f"YouTube new items: {len(new_yt)} (sending {len(to_send)})")

                for it in to_send:
                    pid = f"yt:{it['id']}"
                    if posted_has(state, pid):
                        continue

                    if cfg.dry_run:
                        log.info(f"[DRY_RUN] YT -> {it['id']} | {it['title']}")
                    else:
                        post_discord_embed(session, cfg.discord_webhook_url, youtube_embed(it))

                    posted_add(state, pid)
                    changed = True

                # mark newest id
                state["youtube_last_video_id"] = new_yt[0]["id"]
                changed = True
            else:
                log.info("YouTube: no new items")

    # ---------- Instagram (optional) ----------
    if cfg.ig_user_id and cfg.ig_access_token:
        ig_items = fetch_instagram_media(
            session,
            cfg.ig_graph_version,
            cfg.ig_user_id,
            cfg.ig_access_token,
            limit=10
        )

        if ig_items:
            last_ig = state.get("instagram_last_media_id", "")

            if not last_ig and not cfg.allow_initial_post:
                state["instagram_last_media_id"] = ig_items[0]["id"]
                posted_add(state, f"ig:{ig_items[0]['id']}")
                save_state_atomic(state)
                log.info("Instagram bootstrap: synced state (no post). Set ALLOW_INITIAL_POST=1 to post on first run.")
            else:
                new_ig = compute_new_items(ig_items, last_ig)

                if new_ig:
                    to_send = list(reversed(new_ig))
                    if not cfg.allow_batch_posts:
                        to_send = to_send[-1:]
                    else:
                        to_send = to_send[-cfg.max_items_per_run:]

                    log.info(f"Instagram new items: {len(new_ig)} (sending {len(to_send)})")

                    for it in to_send:
                        pid = f"ig:{it['id']}"
                        if posted_has(state, pid):
                            continue

                        if cfg.dry_run:
                            log.info(f"[DRY_RUN] IG -> {it['id']} | {it['url']}")
                        else:
                            post_discord_embed(session, cfg.discord_webhook_url, instagram_embed(it))

                        posted_add(state, pid)
                        changed = True

                    state["instagram_last_media_id"] = new_ig[0]["id"]
                    changed = True
                else:
                    log.info("Instagram: no new items")
        else:
            log.info("Instagram: no items fetched")
    else:
        log.info("Instagram: not configured (missing IG_USER_ID/IG_ACCESS_TOKEN)")

    if changed:
        # keep posted_ids trimmed
        state["posted_ids"] = (state.get("posted_ids", []) or [])[:POSTED_IDS_LIMIT]
        save_state_atomic(state)
        log.info("State updated.")
    else:
        log.info("No state change.")


if __name__ == "__main__":
    main()
