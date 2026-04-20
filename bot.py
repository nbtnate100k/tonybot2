"""
GREENBEANS CC — Telegram bot UI (hub, profile, top-up, payments, cart).
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

CAPTION = """♨️ Big Restocks Every Day
♨️ Convenient Deposit Via Btc, Ltc
♨️ Support Will Help You 24/7

COUNTRY LIST: 🇺🇸 🇫🇷 🇨🇦 🇩🇪 🇪🇸 🇦🇪 🇮🇱 🇨🇴 🇲🇽 🇨🇱 🇯🇵 🇵🇭

⚜️ Have a good day with GREENBEANS CC ⚜️"""

USERS: dict[int, dict[str, Any]] = {}


def _env_wallet(key: str, fallback: str = "") -> str:
    return (os.getenv(key) or "").strip() or fallback


# GetToTheMoneyJR — defaults to legacy BTC_WALLET / LTC_WALLET if base-specific vars unset
BASE_GTJM_BTC = _env_wallet("BASE_GTJM_BTC", _env_wallet("BTC_WALLET"))
BASE_GTJM_LTC = _env_wallet("BASE_GTJM_LTC", _env_wallet("LTC_WALLET"))
# TONY BASE — separate addresses (set in .env)
BASE_TONY_BTC = _env_wallet("BASE_TONY_BTC")
BASE_TONY_LTC = _env_wallet("BASE_TONY_LTC")


def payment_base_label(key: str | None) -> str:
    if key == "tony":
        return "TONY BASE"
    return "GetToTheMoneyJR"


def payment_base_key(key: str | None) -> str:
    return key if key in ("gtjm", "tony") else "gtjm"


def addresses_for_payment_base(base_key: str | None) -> dict[str, str]:
    k = payment_base_key(base_key)
    if k == "tony":
        return {
            "btc": BASE_TONY_BTC or "— set BASE_TONY_BTC in .env —",
            "ltc": BASE_TONY_LTC or "— set BASE_TONY_LTC in .env —",
        }
    return {
        "btc": BASE_GTJM_BTC or "— set BASE_GTJM_BTC or BTC_WALLET —",
        "ltc": BASE_GTJM_LTC or "— set BASE_GTJM_LTC or LTC_WALLET —",
    }

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
ASSETS_DIR = ROOT_DIR / "assets"
KNOWN_USERS_PATH = DATA_DIR / "known_users.json"
PAYMENTS_PATH = DATA_DIR / "payments.json"
STOCK_PATH = DATA_DIR / "stock_tiers.json"

TIER_IDS: tuple[str, ...] = ("random", "70", "80", "90", "100")
STOCK_BY_TIER: dict[str, dict[str, list[str]]] = {k: {} for k in TIER_IDS}
# Display names from HTML "Bank name" (per price slot); persisted in stock JSON __meta__.
BANK_NAME_BY_TIER: dict[str, str] = {k: "" for k in TIER_IDS}

CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Leadbot-Secret",
}


def _parse_admin_ids() -> set[int]:
    """Comma-separated numeric Telegram user IDs (from @userinfobot). No @username."""
    raw = (os.getenv("ADMIN_USER_IDS", "") or "").strip()
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1].strip()
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        p = part.strip().strip('"').strip("'").replace(" ", "")
        if not p:
            continue
        try:
            out.add(int(p))
        except ValueError:
            log.warning("Invalid ADMIN_USER_IDS entry: %s", part)
    return out


ADMIN_USER_IDS: set[int] = _parse_admin_ids()

LEADBOT_API_SECRET: str = os.getenv("LEADBOT_API_SECRET", "").strip()


def load_stock_tiers() -> None:
    global STOCK_BY_TIER, BANK_NAME_BY_TIER
    STOCK_BY_TIER = {k: {} for k in TIER_IDS}
    BANK_NAME_BY_TIER = {k: "" for k in TIER_IDS}
    if not STOCK_PATH.is_file():
        return
    try:
        raw = json.loads(STOCK_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        meta = raw.get("__meta__")
        if isinstance(meta, dict):
            bn = meta.get("bank_names")
            if isinstance(bn, dict):
                for tid in TIER_IDS:
                    v = bn.get(tid)
                    if isinstance(v, str) and v.strip():
                        BANK_NAME_BY_TIER[tid] = v.strip()[:128]
        for tid in TIER_IDS:
            tier_obj = raw.get(tid)
            if not isinstance(tier_obj, dict):
                continue
            for bin_key, lines in tier_obj.items():
                if not isinstance(lines, list):
                    continue
                STOCK_BY_TIER[tid][str(bin_key)] = [str(x) for x in lines]
        for _tid in TIER_IDS:
            for _bk, lines in STOCK_BY_TIER[_tid].items():
                sort_stock_lines(lines)
    except (OSError, ValueError, TypeError) as e:
        log.warning("Could not load stock tiers: %s", e)


def save_stock_tiers() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {
        "__meta__": {
            "bank_names": {
                tid: (BANK_NAME_BY_TIER.get(tid) or "").strip()[:128] for tid in TIER_IDS
            },
        },
    }
    for tid in TIER_IDS:
        out[tid] = STOCK_BY_TIER[tid]
    STOCK_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def merge_stock_groups(
    tier: str,
    groups: dict[str, list[str]],
    bank: str | None = None,
) -> tuple[int, int]:
    if tier not in TIER_IDS:
        raise ValueError("invalid tier")
    t = STOCK_BY_TIER[tier]
    bins_touched = 0
    lines_added = 0
    for bin_key, lines in groups.items():
        bk = str(bin_key)
        arr = t.setdefault(bk, [])
        for line in lines:
            arr.append(str(line))
            lines_added += 1
        sort_stock_lines(arr)
        bins_touched += 1
    if bank is not None:
        b = bank.strip()[:128]
        if b:
            BANK_NAME_BY_TIER[tier] = b
    save_stock_tiers()
    return bins_touched, lines_added


def extract_bin_prefix_from_line(line: str) -> str | None:
    """First pipe field = card number; first 6 digits = BIN (same as HTML sorter)."""
    s = line.strip()
    if not s:
        return None
    pipe = s.find("|")
    if pipe == -1:
        return None
    card = s[:pipe].strip().strip('"').strip()
    digits = "".join(c for c in card if c.isdigit())
    if len(digits) < 6:
        return None
    return digits[:6]


_SORT_MISSING = "\uffff"


def extract_locality_bin_sort_key(line: str) -> tuple[str, str, str, str]:
    """Order: state → city → ZIP → BIN (aligned with HTML sendout)."""
    b = extract_bin_prefix_from_line(line) or "000000"
    parts = line.split("|")
    city = (parts[6] if len(parts) > 6 else "").strip().strip('"').lower()
    state_raw = (parts[7] if len(parts) > 7 else "").strip().strip('"').upper()
    # Full state token for order (avoids "North Carolina" → wrong 2-letter cut)
    state = state_raw if state_raw else ""
    zipc = (parts[8] if len(parts) > 8 else "").strip().strip('"')
    return (
        state if state else _SORT_MISSING,
        city if city else _SORT_MISSING,
        zipc if zipc else _SORT_MISSING,
        b,
    )


def sort_stock_lines(lines: list[str]) -> None:
    """In-place: state, city, ZIP; BIN identical within bucket."""
    lines.sort(key=lambda ln: extract_locality_bin_sort_key(ln)[:3])


def bin_bucket_catalog_sort_key(lines: list[str]) -> tuple[str, str, str, str]:
    if not lines:
        return (_SORT_MISSING, _SORT_MISSING, _SORT_MISSING, "000000")
    tmp = list(lines)
    sort_stock_lines(tmp)
    return extract_locality_bin_sort_key(tmp[0])


def admin_import_lines_into_tier(tier_key: str, lines: list[str]) -> tuple[int, int]:
    """Append pipe-lines into stock by BIN. Returns (distinct_bins_touched, lines_added)."""
    if tier_key not in TIER_IDS:
        raise ValueError("invalid tier")
    load_stock_tiers()
    t = STOCK_BY_TIER[tier_key]
    bins_seen: set[str] = set()
    lines_added = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        b = extract_bin_prefix_from_line(line)
        if not b:
            continue
        t.setdefault(b, []).append(line)
        bins_seen.add(b)
        lines_added += 1
    for lines in t.values():
        sort_stock_lines(lines)
    save_stock_tiers()
    return len(bins_seen), lines_added


def admin_clear_bin(tier_key: str, bin_key: str) -> bool:
    load_stock_tiers()
    t = STOCK_BY_TIER.get(tier_key) or {}
    bk = str(bin_key).strip()
    if bk not in t:
        return False
    del t[bk]
    save_stock_tiers()
    return True


def stock_tiers_api_payload() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tid in TIER_IDS:
        tier_map = STOCK_BY_TIER.get(tid) or {}
        bins = [
            {"bin": b, "count": len(lines)}
            for b, lines in sorted(
                tier_map.items(),
                key=lambda item: bin_bucket_catalog_sort_key(item[1]),
            )
        ]
        out[tid] = {
            "bins": bins,
            "bank": (BANK_NAME_BY_TIER.get(tid) or "").strip(),
        }
    return out


@web.middleware
async def cors_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS_HEADERS)
    resp = await handler(request)
    for hk, hv in CORS_HEADERS.items():
        resp.headers[hk] = hv
    return resp


async def _leadbot_secret_denied(request: web.Request) -> web.Response | None:
    if not LEADBOT_API_SECRET:
        return None
    got = (request.headers.get("X-Leadbot-Secret") or "").strip()
    if got != LEADBOT_API_SECRET:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    return None


async def handle_stock_tiers(_request: web.Request) -> web.Response:
    return web.json_response(stock_tiers_api_payload())


async def handle_sync_groups(request: web.Request) -> web.Response:
    deny = await _leadbot_secret_denied(request)
    if deny is not None:
        return deny
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "Invalid body"}, status=400)
    groups = body.get("groups")
    tier = str(body.get("tier") or "")
    bank_raw = body.get("bank")
    if bank_raw is None or (isinstance(bank_raw, str) and not bank_raw.strip()):
        bank_raw = body.get("section")
    bank = str(bank_raw or "").strip()[:128]
    if not isinstance(groups, dict):
        return web.json_response({"ok": False, "error": "Missing groups"}, status=400)
    if tier not in TIER_IDS:
        return web.json_response({"ok": False, "error": "Invalid tier"}, status=400)
    clean: dict[str, list[str]] = {}
    for k, v in groups.items():
        bk = str(k)
        if isinstance(v, list):
            clean[bk] = [str(x) for x in v]
        else:
            clean[bk] = [str(v)]
    try:
        bins_touched, lines_added = merge_stock_groups(tier, clean, bank)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    payload: dict[str, Any] = {
        "ok": True,
        "bins_touched": bins_touched,
        "lines_added": lines_added,
        "bank": (BANK_NAME_BY_TIER.get(tier) or "").strip(),
    }
    return web.json_response(payload)


async def handle_sendout(request: web.Request) -> web.Response:
    deny = await _leadbot_secret_denied(request)
    if deny is not None:
        return deny
    if not ADMIN_USER_IDS:
        return web.json_response(
            {"ok": False, "error": "ADMIN_USER_IDS not set in .env"}, status=503
        )
    ptb_app: Application = request.app["ptb_app"]
    bot = ptb_app.bot
    summary_lines: list[str] = ["📤 <b>Stock sendout</b> <i>(state · city · ZIP · BIN)</i>", ""]
    full_text_parts: list[str] = []
    total_lines = 0
    for tid in TIER_IDS:
        tier_map = STOCK_BY_TIER.get(tid) or {}
        n_bins = len(tier_map)
        n_lines = sum(len(lines) for lines in tier_map.values())
        total_lines += n_lines
        bn = (BANK_NAME_BY_TIER.get(tid) or "").strip()
        bank_bit = f" · <i>{html.escape(bn)}</i>" if bn else ""
        summary_lines.append(
            f"• <b>{tid}</b>{bank_bit}: {n_bins} BIN(s), {n_lines} line(s)"
        )
        full_text_parts.append(f"\n=== slot:{tid} bank:{bn or '—'} ===\n")
        for bkey in sorted(
            tier_map.keys(),
            key=lambda bk: bin_bucket_catalog_sort_key(tier_map[bkey]),
        ):
            bucket = list(tier_map[bkey])
            sort_stock_lines(bucket)
            for line in bucket:
                full_text_parts.append(line)
    if total_lines == 0:
        return web.json_response(
            {"ok": False, "error": "No stock — run START in the HTML to sync groups first"},
            status=400,
        )
    summary_text = "\n".join(summary_lines)
    full_body = "\n".join(full_text_parts)
    use_pre = len(full_body) <= 3500
    failed = 0
    for aid in ADMIN_USER_IDS:
        try:
            await bot.send_message(chat_id=aid, text=summary_text, parse_mode="HTML")
            if use_pre:
                await bot.send_message(
                    chat_id=aid,
                    text=f"<pre>{html.escape(full_body)}</pre>",
                    parse_mode="HTML",
                )
            else:
                doc = io.BytesIO(full_body.encode("utf-8"))
                doc.name = "sendout_stock.txt"
                await bot.send_document(
                    chat_id=aid,
                    document=doc,
                    caption="📤 Full sendout dump",
                )
            await asyncio.sleep(0.05)
        except Exception:
            log.exception("Sendout failed for admin chat_id=%s", aid)
            failed += 1
    if failed == len(ADMIN_USER_IDS):
        return web.json_response(
            {"ok": False, "error": "Could not deliver to any admin (check bot / chat id)"},
            status=502,
        )
    return web.json_response({"ok": True})


_ROOT_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>Leadbot API</title>
<style>body{font-family:system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;
background:#111;color:#e5e5e5;line-height:1.5}code{background:#222;padding:.15rem .4rem;border-radius:4px}
a{color:#f97316}h1{font-size:1.25rem}.ok{color:#86efac}</style></head><body>
<h1>Leadbot HTTP API</h1>
<p class="ok">Server is running.</p>
<p>This URL is only the <strong>API</strong> for the stock sorter HTML tool — there is no web UI here.
Open your <code>deepseek_html_*.html</code> file locally (or host it yourself) and set its public API URL
to this deployment’s <strong>https</strong> base (Railway domain).</p>
<ul>
<li><code>GET /api/stock-tiers</code> — stock by tier (JSON)</li>
<li><code>POST /api/sync-groups</code> — sync after START in the HTML</li>
<li><code>POST /api/sendout</code> — send stock to Telegram admins</li>
</ul>
<p><a href="/api/stock-tiers">Try stock-tiers JSON</a></p>
</body></html>"""


async def handle_root(_request: web.Request) -> web.Response:
    return web.Response(text=_ROOT_HTML, content_type="text/html", charset="utf-8")


def _http_listen_port() -> int:
    """Public hosts (Railway) inject PORT — must match 'Target port' in Railway networking."""
    for key in ("PORT", "LEADBOT_HTTP_PORT"):
        raw = os.getenv(key, "").strip()
        if raw.isdigit():
            p = int(raw)
            log.info("HTTP API will listen on %s (from %s)", p, key)
            return p
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
        log.warning(
            "PORT not set — Railway routing usually expects 8080; binding 8080 "
            "(set PORT in Variables to match Public Networking → Target port)"
        )
        return 8080
    log.info("HTTP API will listen on 8787 (local default; set PORT or LEADBOT_HTTP_PORT to override)")
    return 8787


async def start_leadbot_http(ptb_application: Application) -> None:
    web_app = web.Application(middlewares=[cors_middleware])
    web_app["ptb_app"] = ptb_application
    web_app.router.add_get("/", handle_root)
    web_app.router.add_get("/api/stock-tiers", handle_stock_tiers)
    web_app.router.add_post("/api/sync-groups", handle_sync_groups)
    web_app.router.add_post("/api/sendout", handle_sendout)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = _http_listen_port()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(
        "Leadbot HTTP API on port %s — / + GET /api/stock-tiers, POST /api/sync-groups, /api/sendout",
        port,
    )


def resolve_header_image_path() -> Path | None:
    """Banner on /start: BOT_HEADER_IMAGE, or assets/*, or repo-root header.* (matches Docker layout)."""
    env = os.getenv("BOT_HEADER_IMAGE", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
        log.warning("BOT_HEADER_IMAGE set but not a file: %s", env)
    for name in ("header.png", "header.jpg", "header.jpeg", "header.webp"):
        for base in (ASSETS_DIR, ROOT_DIR):
            candidate = base / name
            if candidate.is_file():
                return candidate
    return None


async def post_init(ptb_application: Application) -> None:
    global ADMIN_USER_IDS
    load_dotenv()
    ADMIN_USER_IDS = _parse_admin_ids()
    if ADMIN_USER_IDS:
        log.info("ADMIN_USER_IDS loaded: %s admin account(s)", len(ADMIN_USER_IDS))
    else:
        log.warning("ADMIN_USER_IDS is empty — no Telegram admins until you set the variable")

    load_stock_tiers()
    if resolve_header_image_path() is None:
        log.warning(
            "No header banner for /start — add assets/header.png or header.png at project root, "
            "or set BOT_HEADER_IMAGE in env",
        )
    try:
        wi = await ptb_application.bot.get_webhook_info()
        if wi.url:
            log.warning(
                "Telegram webhook was active (%s); removing so getUpdates polling works",
                wi.url,
            )
            await ptb_application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        log.warning("Could not query/delete Telegram webhook", exc_info=True)
    await start_leadbot_http(ptb_application)


def load_known_users() -> set[int]:
    if not KNOWN_USERS_PATH.is_file():
        return set()
    try:
        data = json.loads(KNOWN_USERS_PATH.read_text(encoding="utf-8"))
        return {int(x) for x in data}
    except (OSError, ValueError, TypeError) as e:
        log.warning("Could not load %s: %s", KNOWN_USERS_PATH, e)
        return set()


def save_known_users(user_ids: set[int]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KNOWN_USERS_PATH.write_text(
        json.dumps(sorted(user_ids), indent=2),
        encoding="utf-8",
    )


known_user_ids: set[int] = load_known_users()

# Users who tapped "add TX link" and must send explorer URL next (mirrors context.user_data).
_awaiting_tx_link_user_ids: set[int] = set()


def mark_awaiting_tx_link(user_id: int) -> None:
    _awaiting_tx_link_user_ids.add(int(user_id))


def clear_awaiting_tx_link(user_id: int) -> None:
    _awaiting_tx_link_user_ids.discard(int(user_id))


class _AwaitingTxLinkMessageFilter(filters.MessageFilter):
    """Only messages from users who tapped “add TX link” (see `_awaiting_tx_link_user_ids`)."""

    def filter(self, message: Message) -> bool:
        if not message.from_user or not message.text:
            return False
        if message.text.strip().startswith("/"):
            return False
        return message.from_user.id in _awaiting_tx_link_user_ids


AWAITING_TX_LINK_FILTER = (
    filters.TEXT & filters.ChatType.PRIVATE & _AwaitingTxLinkMessageFilter()
)


def _default_payment_store() -> dict[str, Any]:
    return {"next_id": 1, "claims": []}


def load_payment_store() -> dict[str, Any]:
    if not PAYMENTS_PATH.is_file():
        return _default_payment_store()
    try:
        return json.loads(PAYMENTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as e:
        log.warning("Could not load payments: %s", e)
        return _default_payment_store()


def save_payment_store(store: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PAYMENTS_PATH.write_text(
        json.dumps(store, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def add_payment_claim(
    user: User,
    amount_usd: float,
    coin: str,
    pay_source: str,
    payment_base: str | None = None,
    tx_link: str = "",
) -> dict[str, Any]:
    store = load_payment_store()
    cid = int(store["next_id"])
    store["next_id"] = cid + 1
    bk = payment_base_key(payment_base)
    claim: dict[str, Any] = {
        "id": cid,
        "user_id": user.id,
        "username": user.username or "",
        "full_name": user.full_name or "",
        "amount_usd": float(amount_usd),
        "coin": coin,
        "pay_source": pay_source,
        "base_key": bk,
        "payment_base": payment_base_label(bk),
        "tx_link": (tx_link or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "resolved_at": None,
        "resolved_by": None,
    }
    store["claims"].append(claim)
    save_payment_store(store)
    return claim


def apply_claim_resolution(
    claim_id: int,
    status: str,
    admin_id: int,
) -> tuple[bool, str, dict[str, Any] | None]:
    if status not in ("accepted", "rejected"):
        return False, "Invalid status", None
    store = load_payment_store()
    for c in store["claims"]:
        if int(c["id"]) != claim_id:
            continue
        if c["status"] != "pending":
            return False, f"Claim #{claim_id} is already {c['status']}.", c
        c["status"] = status
        c["resolved_at"] = datetime.now(timezone.utc).isoformat()
        c["resolved_by"] = admin_id
        if status == "accepted":
            bal_user = ensure_user(int(c["user_id"]))
            amt = float(c["amount_usd"])
            bk = str(c.get("base_key") or "")
            if bk == "tony":
                bal_user["tony_bucks"] = float(bal_user.get("tony_bucks", 0.0)) + amt
            else:
                bal_user["jr_bucks"] = float(bal_user.get("jr_bucks", 0.0)) + amt
            bal_user["deposits"] = float(bal_user.get("deposits", 0.0)) + amt
        save_payment_store(store)
        return True, "Updated.", c
    return False, f"Claim #{claim_id} not found.", None


def payment_user_stats() -> dict[str, Any]:
    store = load_payment_store()
    claimed_users = {int(c["user_id"]) for c in store["claims"]}
    pending = sum(1 for c in store["claims"] if c["status"] == "pending")
    accepted = sum(1 for c in store["claims"] if c["status"] == "accepted")
    rejected = sum(1 for c in store["claims"] if c["status"] == "rejected")
    total_users = len(known_user_ids)
    browse_only = len(known_user_ids - claimed_users)
    return {
        "total_users": total_users,
        "users_ever_claimed": len(claimed_users),
        "users_browse_only": browse_only,
        "pending": pending,
        "accepted": accepted,
        "rejected": rejected,
        "total_claims": len(store["claims"]),
    }


def list_pending_claims(limit: int = 25) -> list[dict[str, Any]]:
    store = load_payment_store()
    pend = [c for c in store["claims"] if c["status"] == "pending"]
    pend.sort(key=lambda x: int(x["id"]), reverse=True)
    return pend[:limit]


def list_recent_claims(limit: int = 30) -> list[dict[str, Any]]:
    store = load_payment_store()
    allc = list(store["claims"])
    allc.sort(key=lambda x: int(x["id"]), reverse=True)
    return allc[:limit]


def claim_detail_html(claim: dict[str, Any]) -> str:
    uname = f"@{claim['username']}" if claim.get("username") else "—"
    extra = ""
    if claim.get("resolved_at"):
        extra = f"\nResolved: <code>{html.escape(str(claim['resolved_at']))}</code>"
    return (
        f"📥 <b>Payment claim</b> #{claim['id']}\n\n"
        f"User: {html.escape(str(claim.get('full_name') or '—'))} "
        f"({html.escape(uname)})\n"
        f"ID: <code>{claim['user_id']}</code>\n"
        f"Amount: <b>{fmt_usd(float(claim['amount_usd']))}</b> USD\n"
        f"Coin: <b>{str(claim.get('coin', '')).upper()}</b>\n"
        f"Base: <b>{html.escape(str(claim.get('payment_base') or '—'))}</b>\n"
        f"TX link: {html.escape(str(claim.get('tx_link') or '—')[:500])}\n"
        f"Flow: <b>{html.escape(str(claim.get('pay_source', '')))}</b>\n"
        f"Status: <b>{html.escape(str(claim.get('status', '')))}</b>\n"
        f"Created: <code>{html.escape(str(claim.get('created_at', '')))}</code>"
        f"{extra}"
    )


def format_claim_oneline(c: dict[str, Any]) -> str:
    un = f"@{c['username']}" if c.get("username") else "—"
    return (
        f"#{c['id']} {html.escape(str(c.get('full_name') or '—'))} ({html.escape(un)}) "
        f"{fmt_usd(float(c['amount_usd']))} <b>{html.escape(str(c.get('coin', '')).upper())}</b> "
        f"<i>{html.escape(str(c.get('status', '')))}</i>"
    )


async def notify_admins_new_claim(bot, claim: dict[str, Any]) -> None:
    if not ADMIN_USER_IDS:
        return
    text = claim_detail_html(claim)
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Accept", callback_data=f"adm_acc_{claim['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{claim['id']}"),
            ]
        ]
    )
    for aid in ADMIN_USER_IDS:
        try:
            await bot.send_message(
                chat_id=aid,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            await asyncio.sleep(0.04)
        except Exception:
            log.info("Admin notify failed aid=%s", aid, exc_info=True)


async def admin_claim_button_action(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
    admin_user: User,
) -> None:
    if not is_admin(admin_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    if data.startswith("adm_acc_"):
        status = "accepted"
        prefix = "adm_acc_"
    elif data.startswith("adm_rej_"):
        status = "rejected"
        prefix = "adm_rej_"
    else:
        return
    try:
        cid = int(data.removeprefix(prefix))
    except ValueError:
        await query.answer("Invalid claim id.", show_alert=True)
        return
    ok, msg, claim = apply_claim_resolution(cid, status, admin_user.id)
    if not ok or not claim:
        await query.answer(msg[:200], show_alert=True)
        return
    await query.answer("Saved.")
    try:
        await query.edit_message_text(
            claim_detail_html(claim),
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        log.warning("Could not edit admin claim message", exc_info=True)


def register_known_user(user_id: int) -> None:
    if user_id not in known_user_ids:
        known_user_ids.add(user_id)
        save_known_users(known_user_ids)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def ensure_user(user_id: int) -> dict[str, Any]:
    register_known_user(user_id)
    if user_id not in USERS:
        USERS[user_id] = {
            "jr_bucks": 0.0,
            "tony_bucks": 0.0,
            "deposits": 0.0,
            "spent": 0.0,
            "status": "active",
        }
    else:
        u = USERS[user_id]
        if "balance" in u and "jr_bucks" not in u:
            u["jr_bucks"] = float(u.pop("balance", 0.0))
        u.setdefault("jr_bucks", 0.0)
        u.setdefault("tony_bucks", 0.0)
    return USERS[user_id]


def shop_wallet_label(wallet_key: str | None) -> str:
    return "Tony Bucks" if wallet_key == "tony" else "JR Bucks"


def user_bucks_balance(u: dict[str, Any], wallet_key: str | None) -> float:
    if wallet_key == "tony":
        return float(u.get("tony_bucks", 0.0))
    return float(u.get("jr_bucks", 0.0))


def fmt_usd(n: float) -> str:
    return f"${n:,.2f}"


def format_start_caption(user_id: int) -> str:
    u = ensure_user(user_id)
    jr = user_bucks_balance(u, "jr")
    tb = user_bucks_balance(u, "tony")
    jr_s = str(int(jr)) if jr == int(jr) else f"{jr:.2f}"
    tb_s = str(int(tb)) if tb == int(tb) else f"{tb:.2f}"
    return (
        "Welcome.\n\n"
        f"<b>JR Bucks:</b> {jr_s}\n"
        f"<b>Tony Bucks:</b> {tb_s}\n\n"
        f"{CAPTION}"
    )


def hub_keyboard() -> InlineKeyboardMarkup:
    """Main menu: Balance | Top Up · Buy CCs | Cart · Profile · Bases."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💰 My Balance", callback_data="m_bal"),
                InlineKeyboardButton("💳 Top Up", callback_data="m_top"),
            ],
            [
                InlineKeyboardButton("💳 Buy CCs", callback_data="m_buy"),
                InlineKeyboardButton("🛒 My Cart", callback_data="m_cart"),
            ],
            [InlineKeyboardButton("👤 My Profile", callback_data="m_prof")],
            [InlineKeyboardButton("🏦 Base (BTC / LTC wallets)", callback_data="m_bases")],
        ]
    )


TIER_PRICES: dict[str, float] = {
    "random": 5.0,
    "70": 10.0,
    "80": 15.0,
    "90": 20.0,
    "100": 25.0,
}

BUY_CALLBACK_TO_TIER: dict[str, str] = {
    "buy_random": "random",
    "buy_70": "70",
    "buy_80": "80",
    "buy_90": "90",
    "buy_100": "100",
}

# Default labels when HTML has not set a bank name for that price slot yet.
TIER_PRICE_FALLBACK: dict[str, str] = {
    "random": "🎲 Random",
    "70": "$10 line",
    "80": "$15 line",
    "90": "$20 line",
    "100": "$25 line",
}


def tier_catalog_html_title(tier_key: str) -> str:
    load_stock_tiers()
    bn = (BANK_NAME_BY_TIER.get(tier_key) or "").strip()
    price_s = fmt_usd(TIER_PRICES[tier_key])
    if bn:
        return f"{html.escape(bn)} · {price_s}"
    return f"{html.escape(TIER_PRICE_FALLBACK.get(tier_key, tier_key))} · {price_s}"


def tier_shop_button_caption(tier_key: str) -> str:
    load_stock_tiers()
    bn = (BANK_NAME_BY_TIER.get(tier_key) or "").strip()
    price = fmt_usd(TIER_PRICES[tier_key])
    if bn:
        base = f"{bn} · {price}"
    else:
        base = f"{TIER_PRICE_FALLBACK.get(tier_key, tier_key)} · {price}"
    return base[:118]

BUY_MENU_ORDER: tuple[str, ...] = (
    "buy_random",
    "buy_70",
    "buy_80",
    "buy_90",
    "buy_100",
)


def tier_total_line_count(tier_key: str) -> int:
    t = STOCK_BY_TIER.get(tier_key) or {}
    return sum(len(lines) for lines in t.values())


def shop_wallet_select_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💵 JR Bucks (GetToTheMoneyJR)",
                    callback_data="shop_jr",
                )
            ],
            [
                InlineKeyboardButton(
                    "💵 Tony Bucks (TONY BASE)",
                    callback_data="shop_tony",
                )
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="shop_wallet_back")],
        ]
    )


def buy_menu_keyboard() -> InlineKeyboardMarkup:
    """Price-slot buttons (bank name from HTML when set); empty = Out of stock."""
    load_stock_tiers()
    rows: list[list[InlineKeyboardButton]] = []
    for cb in BUY_MENU_ORDER:
        tier_key = BUY_CALLBACK_TO_TIER[cb]
        cap = tier_shop_button_caption(tier_key)
        n = tier_total_line_count(tier_key)
        if n <= 0:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{cap} · Out of stock",
                        callback_data=f"oos:{tier_key}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{cap} ({n} lines)",
                        callback_data=cb,
                    )
                ]
            )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="buy_back")])
    return InlineKeyboardMarkup(rows)


BUY_CATALOG_PAGE_SIZE = 8
# Telegram allows up to 128 chars on inline button text; cap for readability.
BIN_BTN_TEXT_MAX = 128


def extract_city_state_from_line(line: str) -> tuple[str, str]:
    """Pipe format: card|mm|yy|cvv|name|address|city|state|... (matches HTML sorter)."""
    parts = line.split("|")
    if len(parts) < 8:
        return "?", "?"
    city = (parts[6] or "").strip().strip('"').strip()
    state = (parts[7] or "").strip().strip('"').strip().upper()
    if not city:
        city = "?"
    if not state:
        state = "?"
    elif len(state) > 3:
        state = state[:2].upper()
    else:
        state = state.upper()
    return city[:20], state[:2]


def primary_location_label(lines: list[str]) -> str:
    """Most common city, ST, ZIP from stock lines (matches chip preview)."""
    freq: dict[str, int] = {}
    for line in lines:
        parts = line.split("|")
        city, st = extract_city_state_from_line(line)
        z = (
            (parts[8] if len(parts) > 8 else "").strip().strip('"')
            if len(parts) > 8
            else ""
        )
        if city == "?" or st in ("", "?"):
            continue
        key = f"{city}, {st}"
        if z:
            key = f"{city}, {st} {z}"
        freq[key] = freq.get(key, 0) + 1
    if not freq:
        return ""
    return max(freq.items(), key=lambda x: (x[1], x[0]))[0]


def format_bin_row_button_text(
    bin_key: str, line_count: int, price: float, location: str
) -> str:
    price_s = fmt_usd(price)
    core = f"{bin_key} ×{line_count} · {price_s}"
    if not location:
        return core[:BIN_BTN_TEXT_MAX]
    extra = f" · {location}"
    if len(core + extra) <= BIN_BTN_TEXT_MAX:
        return core + extra
    room = BIN_BTN_TEXT_MAX - len(core) - 3
    if room < 4:
        return core[:BIN_BTN_TEXT_MAX]
    loc_short = location[:room] + ("…" if len(location) > room else "")
    return core + " · " + loc_short


def tier_catalog_text_and_keyboard(
    tier_key: str,
    user_id: int,
    page: int = 0,
    wallet_key: str = "jr",
) -> tuple[str, InlineKeyboardMarkup]:
    load_stock_tiers()
    u = ensure_user(user_id)
    price = TIER_PRICES[tier_key]
    head = tier_catalog_html_title(tier_key)
    wlabel = shop_wallet_label(wallet_key)
    bal = user_bucks_balance(u, wallet_key)
    tier_stock = STOCK_BY_TIER.get(tier_key) or {}
    bins_sorted = sorted(
        tier_stock.keys(),
        key=lambda bk: bin_bucket_catalog_sort_key(tier_stock[bk]),
    )
    total_bins = len(bins_sorted)
    total_lines = sum(len(v) for v in tier_stock.values())

    if total_bins == 0:
        text = (
            f"💳 <b>{head}</b> <i>({html.escape(wlabel)})</i>\n"
            f"Price: <b>{fmt_usd(price)}</b> per line · "
            f"{html.escape(wlabel)}: <b>{fmt_usd(bal)}</b>\n\n"
            "<b>Out of stock.</b> Nothing listed for this bank slot.\n"
            "Sync stock from the HTML sorter (set Bank name + START), then open Buy CCs again."
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="buy_tier_back")]]
        )
        return text, kb

    start = page * BUY_CATALOG_PAGE_SIZE
    chunk = bins_sorted[start : start + BUY_CATALOG_PAGE_SIZE]
    more_note = ""
    if total_bins > BUY_CATALOG_PAGE_SIZE:
        more_note = (
            f"\n<i>Showing {start + 1}–{start + len(chunk)} of {total_bins} BINs</i>"
        )

    text = (
        f"💳 <b>{head}</b> <i>({html.escape(wlabel)})</i>\n"
        f"Price: <b>{fmt_usd(price)}</b> per line · "
        f"Lines in stock: <b>{total_lines}</b>\n"
        f"{html.escape(wlabel)}: <b>{fmt_usd(bal)}</b>\n\n"
        "Tap a <b>BIN</b> to buy <b>one</b> line from that group."
        f"{more_note}"
    )

    rows: list[list[InlineKeyboardButton]] = []
    for bk in chunk:
        cnt = len(tier_stock[bk])
        loc = primary_location_label(tier_stock[bk])
        btn_text = format_bin_row_button_text(bk, cnt, price, loc)
        rows.append(
            [
                InlineKeyboardButton(
                    btn_text,
                    callback_data=f"bpr:{tier_key}:{bk}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                "« Prev", callback_data=f"bpg:{tier_key}:{page - 1}"
            )
        )
    if start + BUY_CATALOG_PAGE_SIZE < total_bins:
        nav_row.append(
            InlineKeyboardButton(
                "Next »", callback_data=f"bpg:{tier_key}:{page + 1}"
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="buy_tier_back")])
    return text, InlineKeyboardMarkup(rows)


async def handle_buy_product(
    query, user: User, data: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "bpr":
        await query.answer("Invalid selection.", show_alert=True)
        return
    _, tier_key, bin_key = parts
    if tier_key not in TIER_PRICES:
        await query.answer("Invalid tier.", show_alert=True)
        return
    load_stock_tiers()
    tier_stock = STOCK_BY_TIER.get(tier_key) or {}
    lines = tier_stock.get(bin_key)
    if not lines:
        await query.answer(
            "This BIN is empty or sold out. Refresh the list.",
            show_alert=True,
        )
        return
    price = TIER_PRICES[tier_key]
    wk = context.user_data.get("purchase_wallet") or "jr"
    wlabel = shop_wallet_label(wk)
    u = ensure_user(user.id)
    have = user_bucks_balance(u, wk)
    if have < price:
        await query.answer(
            f"Insufficient {wlabel}.\nYou have {fmt_usd(have)}.\n"
            f"This line costs {fmt_usd(price)}.\nTop up on the matching base (💳 Top Up).",
            show_alert=True,
        )
        return
    line = lines.pop(0)
    if not lines:
        del tier_stock[bin_key]
    save_stock_tiers()
    if wk == "tony":
        u["tony_bucks"] = float(u.get("tony_bucks", 0.0)) - price
    else:
        u["jr_bucks"] = float(u.get("jr_bucks", 0.0)) - price
    u["spent"] = float(u.get("spent", 0.0)) + price
    await query.answer("Purchased! Delivered below.", show_alert=False)
    escaped = html.escape(line)
    await query.message.reply_text(
        f"✅ <b>Delivered</b> · {tier_catalog_html_title(tier_key)}\n"
        f"{html.escape(wlabel)} · BIN <code>{html.escape(bin_key)}</code> · paid {fmt_usd(price)}\n\n"
        f"<code>{escaped}</code>",
        parse_mode="HTML",
    )


async def handle_buy_catalog_page(
    query, user: User, data: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "bpg":
        await query.answer("Invalid.", show_alert=True)
        return
    tier_key = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        await query.answer("Invalid page.", show_alert=True)
        return
    if tier_key not in TIER_PRICES or page < 0:
        await query.answer("Invalid.", show_alert=True)
        return
    wk = context.user_data.get("purchase_wallet") or "jr"
    text, kb = tier_catalog_text_and_keyboard(tier_key, user.id, page=page, wallet_key=wk)
    await edit_safe(query, text, kb)
    await query.answer()


def topup_amount_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("$10", callback_data="tu_10"),
                InlineKeyboardButton("$100", callback_data="tu_100"),
                InlineKeyboardButton("$200", callback_data="tu_200"),
            ],
            [
                InlineKeyboardButton("$500", callback_data="tu_500"),
                InlineKeyboardButton("$1,000", callback_data="tu_1000"),
                InlineKeyboardButton("Custom", callback_data="tu_custom"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="tu_back")],
        ]
    )


def pay_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("₿ BTC", callback_data="pay_btc"),
                InlineKeyboardButton("Ł LTC", callback_data="pay_ltc"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="pay_m_back")],
        ]
    )


def coin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📥 I paid — add TX link",
                    callback_data="pay_tx_step",
                )
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="pay_coin_back")],
        ]
    )


TOPUP_TEXT = """💰 <b>Top-Up</b>

Choose an amount to add:

• Minimum: <b>$10</b>
• Pay with <b>BTC</b> or <b>LTC</b> — pick a <b>base</b> first. Each base has its own addresses.

• <b>GetToTheMoneyJR</b> credits <b>JR Bucks</b>
• <b>TONY BASE</b> credits <b>Tony Bucks</b>

After the team confirms your payment on-chain, the matching balance updates."""


def profile_html(user: User) -> str:
    u = ensure_user(user.id)
    name = html.escape(user.full_name or "—")
    uname = user.username or "—"
    if uname != "—":
        uname_h = f'<a href="https://t.me/{html.escape(user.username or "")}">@{html.escape(user.username or "")}</a>'
    else:
        uname_h = "—"
    jr = user_bucks_balance(u, "jr")
    tb = user_bucks_balance(u, "tony")
    return (
        "👤 <b>Profile</b>\n\n"
        f"Name: <b>{name}</b>\n"
        f"Username: {uname_h}\n"
        f"Telegram ID: <code>{user.id}</code>\n\n"
        f"<b>JR Bucks:</b> {fmt_usd(jr)}\n"
        f"<b>Tony Bucks:</b> {fmt_usd(tb)}\n"
        f"Total Deposits: <b>{fmt_usd(u['deposits'])}</b>\n"
        f"Total Spent: <b>{fmt_usd(u['spent'])}</b>\n\n"
        f"Status: <b>{html.escape(str(u['status']))}</b>"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    ensure_user(uid)
    markup = hub_keyboard()
    caption = format_start_caption(uid)
    photo_path = resolve_header_image_path()
    if photo_path is not None:
        with photo_path.open("rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=caption,
                reply_markup=markup,
                parse_mode="HTML",
            )
    else:
        await update.message.reply_text(
            caption,
            reply_markup=markup,
            parse_mode="HTML",
        )


def base_select_text(amount: float) -> str:
    return (
        "🏦 <b>Select payment base</b>\n\n"
        f"Invoice: <b>{fmt_usd(amount)} USD</b>\n\n"
        "Each base uses <b>different</b> BTC and LTC deposit addresses.\n\n"
        "<b>GetToTheMoneyJR</b> — primary base\n"
        "<b>TONY BASE</b> — alternate base"
    )


def base_select_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "GetToTheMoneyJR", callback_data="base_gtjm"
                )
            ],
            [InlineKeyboardButton("TONY BASE", callback_data="base_tony")],
            [InlineKeyboardButton("⬅️ Back", callback_data="base_back")],
        ]
    )


def pay_method_text(amount: float, base_label: str = "") -> str:
    base_line = (
        f"\n<b>Base:</b> {html.escape(base_label)}\n"
        if base_label
        else "\n"
    )
    return (
        "💰 <b>SELECT PAYMENT METHOD</b>\n\n"
        f"Invoice Amount: <b>{fmt_usd(amount)} USD</b>"
        f"{base_line}"
        "<b>Cryptocurrency:</b> Choose <b>BTC</b> or <b>LTC</b>:"
    )


def coin_invoice_text(
    coin: str, amount: float, address: str, base_label: str = ""
) -> str:
    labels = {
        "btc": ("₿", "Bitcoin (BTC)"),
        "ltc": ("Ł", "Litecoin (LTC)"),
    }
    sym, title = labels[coin]
    addr = html.escape(address)
    bl = (
        f"<b>Base:</b> {html.escape(base_label)}\n\n"
        if base_label
        else ""
    )
    return (
        f"{sym} <b>{title}</b>\n\n"
        f"{bl}"
        f"Invoice: <b>{fmt_usd(amount)} USD</b>\n\n"
        "Send crypto to:\n"
        f"<code>{addr}</code>\n\n"
        "After you send, tap <b>I paid — add TX link</b> below, paste your explorer link, "
        "then use <b>Final submit</b> so the team can review and credit "
        "<b>JR Bucks</b> or <b>Tony Bucks</b> (matching this base)."
    )


async def delete_message_safe(msg) -> None:
    try:
        await msg.delete()
    except Exception:
        log.warning("Could not delete message", exc_info=True)


async def edit_safe(query, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.warning("Could not edit message", exc_info=True)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.message or not user:
        return
    data = query.data or ""

    if data.startswith("adm_acc_") or data.startswith("adm_rej_"):
        await admin_claim_button_action(query, context, data, user)
        return

    if data.startswith("bpr:"):
        ensure_user(user.id)
        await handle_buy_product(query, user, data, context)
        return
    if data.startswith("bpg:"):
        ensure_user(user.id)
        await handle_buy_catalog_page(query, user, data, context)
        return
    if data == "buy_tier_back":
        ensure_user(user.id)
        wk = context.user_data.get("purchase_wallet") or "jr"
        wlabel = shop_wallet_label(wk)
        await edit_safe(
            query,
            f"💳 <b>Select a bank line</b> <i>({html.escape(wlabel)})</i>",
            buy_menu_keyboard(),
        )
        await query.answer()
        return

    if data.startswith("oos:"):
        ensure_user(user.id)
        await query.answer(
            "Out of stock — add lines in the HTML sorter (Bank name + START), then tap Buy CCs again.",
            show_alert=True,
        )
        return

    await query.answer()
    ensure_user(user.id)

    if data == "m_bal":
        u = ensure_user(user.id)
        jr = user_bucks_balance(u, "jr")
        tb = user_bucks_balance(u, "tony")
        bal_text = (
            "💰 <b>My Balance</b>\n\n"
            f"<b>JR Bucks:</b> {fmt_usd(jr)}\n"
            f"<b>Tony Bucks:</b> {fmt_usd(tb)}\n"
            f"Total Deposits: <b>{fmt_usd(u['deposits'])}</b>\n"
            f"Total Spent: <b>{fmt_usd(u['spent'])}</b>"
        )
        await query.message.reply_text(
            bal_text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="bal_back")]]
            ),
            parse_mode="HTML",
        )
        return

    if data == "bal_back":
        await delete_message_safe(query.message)
        return

    if data == "m_prof":
        await query.message.reply_text(
            profile_html(user),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="prof_back")]]
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if data == "prof_back":
        await delete_message_safe(query.message)
        return

    if data == "m_top":
        await query.message.reply_text(
            TOPUP_TEXT,
            reply_markup=topup_amount_keyboard(),
            parse_mode="HTML",
        )
        return

    if data == "tu_back":
        await delete_message_safe(query.message)
        return

    if data == "tu_custom":
        await edit_safe(
            query,
            "Custom amount: minimum deposit is <b>$10</b>. Please pick a preset.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="tu_restart")]]
            ),
        )
        return

    if data == "tu_restart":
        clear_awaiting_tx_link(user.id)
        context.user_data.pop("pay_base", None)
        context.user_data.pop("awaiting_tx_link", None)
        context.user_data.pop("pending_tx_link", None)
        await edit_safe(query, TOPUP_TEXT, topup_amount_keyboard())
        return

    topup_map = {
        "tu_10": 10.0,
        "tu_100": 100.0,
        "tu_200": 200.0,
        "tu_500": 500.0,
        "tu_1000": 1000.0,
    }
    if data in topup_map:
        amt = topup_map[data]
        context.user_data["pending_invoice"] = amt
        context.user_data["pay_source"] = "topup"
        context.user_data.pop("pay_base", None)
        await edit_safe(query, base_select_text(amt), base_select_keyboard())
        return

    if data == "base_gtjm":
        context.user_data["pay_base"] = "gtjm"
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        await edit_safe(
            query,
            pay_method_text(amt, payment_base_label("gtjm")),
            pay_method_keyboard(),
        )
        return

    if data == "base_tony":
        context.user_data["pay_base"] = "tony"
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        await edit_safe(
            query,
            pay_method_text(amt, payment_base_label("tony")),
            pay_method_keyboard(),
        )
        return

    if data == "base_back":
        clear_awaiting_tx_link(user.id)
        context.user_data.pop("pay_base", None)
        context.user_data.pop("awaiting_tx_link", None)
        context.user_data.pop("pending_tx_link", None)
        if context.user_data.get("pay_source") == "topup":
            await edit_safe(query, TOPUP_TEXT, topup_amount_keyboard())
        return

    if data == "m_bases":
        await query.message.reply_text(
            "🏦 <b>Payment bases</b>\n\n"
            "<b>GetToTheMoneyJR</b> — uses <code>BASE_GTJM_BTC</code> / "
            "<code>BASE_GTJM_LTC</code> (falls back to <code>BTC_WALLET</code> / "
            "<code>LTC_WALLET</code> if unset).\n\n"
            "<b>TONY BASE</b> — uses <code>BASE_TONY_BTC</code> / "
            "<code>BASE_TONY_LTC</code>.\n\n"
            "When you <b>Top Up</b>, you choose a base, then <b>BTC</b> or <b>LTC</b>. "
            "Only BTC and LTC — no ETH.",
            parse_mode="HTML",
        )
        return

    if data in ("m_cart", "m_buy"):
        await query.message.reply_text(
            "💳 <b>Shop with which balance?</b>\n\n"
            "<b>JR Bucks</b> — spend on stock tied to <b>GetToTheMoneyJR</b>.\n"
            "<b>Tony Bucks</b> — spend on stock tied to <b>TONY BASE</b>.\n\n"
            "Pick JR or Tony wallet, then choose a bank line ($5–$25).",
            reply_markup=shop_wallet_select_keyboard(),
            parse_mode="HTML",
        )
        return

    if data == "buy_back":
        await delete_message_safe(query.message)
        return

    if data == "shop_wallet_back":
        await delete_message_safe(query.message)
        return

    if data == "shop_jr":
        context.user_data["purchase_wallet"] = "jr"
        await edit_safe(
            query,
            f"💳 <b>Select a bank line</b> <i>({html.escape(shop_wallet_label('jr'))})</i>",
            buy_menu_keyboard(),
        )
        return

    if data == "shop_tony":
        context.user_data["purchase_wallet"] = "tony"
        await edit_safe(
            query,
            f"💳 <b>Select a bank line</b> <i>({html.escape(shop_wallet_label('tony'))})</i>",
            buy_menu_keyboard(),
        )
        return

    if data in BUY_CALLBACK_TO_TIER:
        tier_key = BUY_CALLBACK_TO_TIER[data]
        wk = context.user_data.get("purchase_wallet") or "jr"
        text, kb = tier_catalog_text_and_keyboard(
            tier_key, user.id, page=0, wallet_key=wk
        )
        await edit_safe(query, text, kb)
        return

    if data == "pay_m_back":
        clear_awaiting_tx_link(user.id)
        context.user_data.pop("awaiting_tx_link", None)
        context.user_data.pop("pending_tx_link", None)
        src = context.user_data.get("pay_source")
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        if src == "topup":
            await edit_safe(query, base_select_text(amt), base_select_keyboard())
            return
        if src == "cart":
            wk = context.user_data.get("purchase_wallet") or "jr"
            await edit_safe(
                query,
                f"💳 <b>Select a bank line</b> <i>({html.escape(shop_wallet_label(wk))})</i>",
                buy_menu_keyboard(),
            )
        return

    if data in ("pay_btc", "pay_ltc"):
        coin = data.replace("pay_", "")
        context.user_data["pay_coin"] = coin
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        base_k = payment_base_key(context.user_data.get("pay_base"))
        addrs = addresses_for_payment_base(base_k)
        base_lbl = payment_base_label(base_k)
        await edit_safe(
            query,
            coin_invoice_text(coin, amt, addrs[coin], base_lbl),
            coin_keyboard(),
        )
        return

    if data == "pay_coin_back":
        clear_awaiting_tx_link(user.id)
        context.user_data.pop("awaiting_tx_link", None)
        context.user_data.pop("pending_tx_link", None)
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        base_k = payment_base_key(context.user_data.get("pay_base"))
        await edit_safe(
            query,
            pay_method_text(amt, payment_base_label(base_k)),
            pay_method_keyboard(),
        )
        return

    if data == "pay_tx_step":
        coin = str(context.user_data.get("pay_coin") or "btc")
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        base_k = payment_base_key(context.user_data.get("pay_base"))
        base_lbl = payment_base_label(base_k)
        context.user_data["awaiting_tx_link"] = True
        context.user_data.pop("pending_tx_link", None)
        mark_awaiting_tx_link(user.id)
        await edit_safe(
            query,
            "🔗 <b>Transaction link</b>\n\n"
            f"Invoice: <b>{fmt_usd(amt)} USD</b>\n"
            f"Base: <b>{html.escape(base_lbl)}</b> · Coin: <b>{html.escape(coin.upper())}</b>\n\n"
            "Send <b>one message</b> with a <b>blockchain explorer link</b> to your payment "
            "(mempool, blockchair, etc.).\n\n"
            "We’ll then show a <b>Final submit</b> button so admins can accept or reject.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="pay_tx_cancel")]]
            ),
        )
        return

    if data == "pay_tx_cancel":
        clear_awaiting_tx_link(user.id)
        context.user_data.pop("awaiting_tx_link", None)
        context.user_data.pop("pending_tx_link", None)
        coin = str(context.user_data.get("pay_coin") or "btc")
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        base_k = payment_base_key(context.user_data.get("pay_base"))
        addrs = addresses_for_payment_base(base_k)
        base_lbl = payment_base_label(base_k)
        await edit_safe(
            query,
            coin_invoice_text(coin, amt, addrs[coin], base_lbl),
            coin_keyboard(),
        )
        return

    if data == "pay_final_submit":
        tx = (context.user_data.get("pending_tx_link") or "").strip()
        if not tx:
            await query.answer(
                "No TX link saved. Paste an explorer link in chat first.",
                show_alert=True,
            )
            return
        coin = str(context.user_data.get("pay_coin") or "?")
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        src = str(context.user_data.get("pay_source") or "?")
        base_k = payment_base_key(context.user_data.get("pay_base"))
        claim = add_payment_claim(
            user, amt, coin, src, payment_base=base_k, tx_link=tx
        )
        await notify_admins_new_claim(context.bot, claim)
        clear_awaiting_tx_link(user.id)
        context.user_data.pop("awaiting_tx_link", None)
        context.user_data.pop("pending_tx_link", None)
        buck = "Tony Bucks" if base_k == "tony" else "JR Bucks"
        await edit_safe(
            query,
            "✅ <b>Submitted for review</b>\n\n"
            f"Claim: <b>#{claim['id']}</b>\n"
            f"Credits if accepted: <b>{buck}</b>\n"
            "An admin will verify on-chain and accept or reject.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="pay_done_back")]]
            ),
        )
        return

    if data == "pay_final_cancel":
        clear_awaiting_tx_link(user.id)
        context.user_data.pop("awaiting_tx_link", None)
        context.user_data.pop("pending_tx_link", None)
        coin = str(context.user_data.get("pay_coin") or "btc")
        amt = float(context.user_data.get("pending_invoice") or 0.0)
        base_k = payment_base_key(context.user_data.get("pay_base"))
        addrs = addresses_for_payment_base(base_k)
        base_lbl = payment_base_label(base_k)
        await edit_safe(
            query,
            coin_invoice_text(coin, amt, addrs[coin], base_lbl),
            coin_keyboard(),
        )
        return

    if data == "pay_done_back":
        clear_awaiting_tx_link(user.id)
        context.user_data.pop("awaiting_tx_link", None)
        context.user_data.pop("pending_tx_link", None)
        src = context.user_data.get("pay_source")
        context.user_data.pop("pay_base", None)
        if src == "topup":
            await edit_safe(query, TOPUP_TEXT, topup_amount_keyboard())
        elif src == "cart":
            wk = context.user_data.get("purchase_wallet") or "jr"
            await edit_safe(
                query,
                f"💳 <b>Select a bank line</b> <i>({html.escape(shop_wallet_label(wk))})</i>",
                buy_menu_keyboard(),
            )
        else:
            await delete_message_safe(query.message)
        return


async def handle_payment_tx_link_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    low = text.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        await update.message.reply_text(
            "Please send a full explorer link starting with "
            "<code>https://</code> or <code>http://</code>.",
            parse_mode="HTML",
        )
        return
    context.user_data["pending_tx_link"] = text
    context.user_data.pop("awaiting_tx_link", None)
    clear_awaiting_tx_link(uid)
    amt = float(context.user_data.get("pending_invoice") or 0.0)
    base_k = payment_base_key(context.user_data.get("pay_base"))
    base_lbl = payment_base_label(base_k)
    coin = str(context.user_data.get("pay_coin") or "?")
    buck = "Tony Bucks" if base_k == "tony" else "JR Bucks"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Final submit to admins",
                    callback_data="pay_final_submit",
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="pay_final_cancel")],
        ]
    )
    await update.message.reply_text(
        "✅ <b>TX link received</b>\n\n"
        f"Invoice: <b>{fmt_usd(amt)} USD</b>\n"
        f"Base: <b>{html.escape(base_lbl)}</b> · Coin: <b>{html.escape(coin.upper())}</b>\n"
        f"Credits if accepted: <b>{buck}</b>\n\n"
        f"Link:\n<code>{html.escape(text[:900])}</code>\n\n"
        "Tap <b>Final submit to admins</b> when ready.",
        reply_markup=kb,
        parse_mode="HTML",
    )


def _announce_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    msg = update.message
    if not msg:
        return None
    if msg.reply_to_message:
        r = msg.reply_to_message
        return (r.text or r.caption or "").strip() or None
    args = context.args
    if args:
        return " ".join(args).strip() or None
    return None


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    st = payment_user_stats()
    panel = (
        "🔐 <b>Admin portal</b>\n\n"
        "<b>/payportal</b> — payments: claims vs browsing-only users\n"
        "<b>/pendingclaims</b> — queue · <b>/allclaims</b> — history\n"
        "<b>/accept &lt;id&gt;</b> · <b>/reject &lt;id&gt;</b>\n\n"
        "<b>/users</b> — broadcast list size\n"
        "<b>/announce</b> — DM everyone (reply or text after command)\n"
        "<b>/myid</b> — any user: show Telegram id & admin yes/no\n\n"
        f"Subscribers: <b>{len(known_user_ids)}</b>\n"
        f"⏳ Pending claims: <b>{st['pending']}</b>"
    )
    await update.message.reply_text(panel, parse_mode="HTML")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    await update.message.reply_text(
        f"📊 <b>Broadcast list</b>\n\n"
        f"Users who used the bot: <b>{len(known_user_ids)}</b>",
        parse_mode="HTML",
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    u = update.effective_user
    admin_ok = is_admin(u.id)
    await update.message.reply_text(
        f"👤 <b>Your Telegram user id</b>\n<code>{u.id}</code>\n\n"
        f"Admin for this bot: <b>{'yes' if admin_ok else 'no'}</b>\n\n"
        "If that should be <b>yes</b>, add this number to "
        "<code>ADMIN_USER_IDS</code> in Railway (or .env), comma-separated, then redeploy.",
        parse_mode="HTML",
    )


async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not ADMIN_USER_IDS:
        await update.message.reply_text(
            "Set <code>ADMIN_USER_IDS</code> in <code>.env</code> first.",
            parse_mode="HTML",
        )
        return
    body = _announce_body(update, context)
    if not body:
        await update.message.reply_text(
            "Usage:\n"
            "/announce Your message here…\n"
            "Or reply to a message and send /announce (sends that message’s text).",
            parse_mode="HTML",
        )
        return
    if not known_user_ids:
        await update.message.reply_text("No subscribers yet (nobody has used /start).")
        return
    await update.message.reply_text(
        f"Sending to <b>{len(known_user_ids)}</b> users…",
        parse_mode="HTML",
    )
    ok, failed = 0, 0
    for uid in sorted(known_user_ids):
        try:
            await context.bot.send_message(chat_id=uid, text=body)
            ok += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
            log.info("Broadcast failed for chat_id=%s", uid, exc_info=True)
    await update.message.reply_text(
        f"✅ Delivered: <b>{ok}</b>\n❌ Failed: <b>{failed}</b>",
        parse_mode="HTML",
    )


async def cmd_payportal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    s = payment_user_stats()
    text = (
        "💳 <b>Payment portal</b>\n\n"
        f"👥 Users who opened the bot: <b>{s['total_users']}</b>\n"
        f"🧾 Users who submitted “payment sent” (any time): <b>{s['users_ever_claimed']}</b>\n"
        f"👀 Browsing only (never filed a claim): <b>{s['users_browse_only']}</b>\n\n"
        f"Claims — ⏳ <b>{s['pending']}</b> pending · "
        f"✅ <b>{s['accepted']}</b> accepted · "
        f"❌ <b>{s['rejected']}</b> rejected\n"
        f"Total rows: <b>{s['total_claims']}</b>\n\n"
        "<b>/pendingclaims</b> — pending queue\n"
        "<b>/allclaims</b> — recent claims (all statuses)\n"
        "<b>/accept 12</b> / <b>/reject 12</b> — by claim #"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_pendingclaims(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    lines = [format_claim_oneline(c) for c in list_pending_claims(30)]
    body = "\n".join(lines) if lines else "<i>No pending claims.</i>"
    await update.message.reply_text(
        "⏳ <b>Pending payment claims</b>\n\n" + body,
        parse_mode="HTML",
    )


async def cmd_allclaims(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    lim = 30
    if context.args:
        try:
            lim = max(1, min(80, int(context.args[0])))
        except ValueError:
            pass
    lines = [format_claim_oneline(c) for c in list_recent_claims(lim)]
    body = "\n".join(lines) if lines else "<i>No claims yet.</i>"
    await update.message.reply_text(
        f"📋 <b>Recent claims</b> (last {lim})\n\n" + body,
        parse_mode="HTML",
    )


async def cmd_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /accept &lt;claim_id&gt;", parse_mode="HTML"
        )
        return
    try:
        cid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Claim id must be a number.")
        return
    ok, msg, claim = apply_claim_resolution(cid, "accepted", update.effective_user.id)
    if ok and claim:
        await update.message.reply_text(claim_detail_html(claim), parse_mode="HTML")
    else:
        await update.message.reply_text(html.escape(msg), parse_mode="HTML")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /reject &lt;claim_id&gt;", parse_mode="HTML"
        )
        return
    try:
        cid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Claim id must be a number.")
        return
    ok, msg, claim = apply_claim_resolution(cid, "rejected", update.effective_user.id)
    if ok and claim:
        await update.message.reply_text(claim_detail_html(claim), parse_mode="HTML")
    else:
        await update.message.reply_text(html.escape(msg), parse_mode="HTML")


_conflict_log_at: float = 0.0


async def error_handler(_update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _conflict_log_at
    err = context.error
    if isinstance(err, Conflict):
        now = time.monotonic()
        if now - _conflict_log_at >= 45.0:
            _conflict_log_at = now
            log.error(
                "Telegram 409 Conflict: another process is polling this bot (mixed 200/409 in logs). "
                "Only ONE getUpdates client allowed. Check: PC running bot.py; a second Railway "
                "service/env with the same TELEGRAM_BOT_TOKEN; replicas > 1; staging+production "
                "both running. Stop extras, then /revoke token if it leaked."
            )
        return
    log.exception("Unhandled exception in handler", exc_info=err)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN in a .env file (see .env.example)."
        )
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("announce", cmd_announce))
    app.add_handler(CommandHandler("payportal", cmd_payportal))
    app.add_handler(CommandHandler("pendingclaims", cmd_pendingclaims))
    app.add_handler(CommandHandler("allclaims", cmd_allclaims))
    app.add_handler(CommandHandler("accept", cmd_accept))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(
        MessageHandler(AWAITING_TX_LINK_FILTER, handle_payment_tx_link_message)
    )
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(error_handler)
    if not ADMIN_USER_IDS:
        log.warning("ADMIN_USER_IDS is empty — set it in .env to use /admin and /announce")
    log.info("Bot running — press Ctrl+C to stop")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
