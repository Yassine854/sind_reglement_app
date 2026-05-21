from collections import defaultdict
from datetime import date, datetime
import json
import os
import re
import time
import uuid

from fastapi import BackgroundTasks, FastAPI, File, Form, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

TYPE_MAP = {
    "CESP": "Espèces",
    "CTRT": "Traite",
    "CCHQR": "Chèque",
}

VALID_SITES = {"SFX", "MAH", "NAB", "SSE", "TUN"}
# Local-dev fallback paths (empty by default for hosted/Render deployment).
# Set via environment variables; leave blank on Render.
DEFAULT_CURRENT_REGLEMENT_FILE = os.environ.get("DEFAULT_REGLEMENT_FILE", "")
DEFAULT_HISTORY_REGLEMENTS_DIR = os.environ.get("DEFAULT_HISTORY_DIR", "")

# ── Session storage (7 days) ──────────────────────────────────────────────────
SESSION_TTL = 7 * 24 * 60 * 60  # seconds
SESSION_STORAGE_FILE = os.path.join("data", "uploaded_sessions.json")
session_store: dict[str, dict] = {}

CAM_SITE_MAP: dict[str, str] = {
    # SFX
    "CAM01": "SFX", "CAM02": "SFX", "CAM03": "SFX", "CAM04": "SFX",
    "CAM05": "SFX", "CAM06": "SFX", "CAM07": "SFX", "CAM36": "SFX",
    "CAM37": "SFX", "CAM38": "SFX", "CAM48": "SFX", "CAM49": "SFX",
    "CAM58": "SFX", "CAM59": "SFX",
    # MAH
    "CAM40": "MAH", "CAM41": "MAH", "CAM42": "MAH", "CAM43": "MAH",
    "CAM44": "MAH", "CAM45": "MAH", "CAM57": "MAH",
    # NAB
    "CAM50": "NAB", "CAM51": "NAB", "CAM52": "NAB", "CAM53": "NAB",
    "CAM54": "NAB",
    # SSE
    "CAM08": "SSE", "CAM09": "SSE", "CAM10": "SSE", "CAM11": "SSE",
    "CAM12": "SSE", "CAM13": "SSE", "CAM14": "SSE", "CAM15": "SSE",
    "CAM39": "SSE", "CAM46": "SSE", "CAM47": "SSE",
    # TUN
    "CAM16": "TUN", "CAM17": "TUN", "CAM18": "TUN", "CAM19": "TUN",
    "CAM20": "TUN", "CAM21": "TUN", "CAM22": "TUN", "CAM23": "TUN",
    "CAM24": "TUN", "CAM25": "TUN", "CAM26": "TUN", "CAM27": "TUN",
    "CAM29": "TUN", "CAM30": "TUN", "CAM31": "TUN",
}


def get_site(cam: str | None) -> str:
    if cam is None:
        return "Inconnu"
    return CAM_SITE_MAP.get(cam, "Inconnu")



def normalize_site(site: str | None) -> str:
    if site is None:
        return "Inconnu"
    site = site.strip().upper()
    return site if site in VALID_SITES else "Inconnu"


def parse_reglement_date(value: str | None) -> date | None:
    if value is None:
        return None
    value = value.strip()
    if not re.fullmatch(r"\d{8}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None



def read_text_file(path: str) -> tuple[str | None, str | None]:
    try:
        with open(path, "rb") as f:
            content = f.read()
        try:
            return content.decode("utf-8"), None
        except UnicodeDecodeError:
            return content.decode("latin-1"), None
    except FileNotFoundError:
        return None, f"Fichier introuvable : {path}"
    except IsADirectoryError:
        return None, f"Chemin invalide (dossier) : {path}"
    except OSError:
        return None, f"Impossible de lire le fichier : {path}"
    return None, f"Impossible de décoder le fichier : {path}"



def list_reglement_files(directory: str) -> tuple[list[str], list[str]]:
    if not os.path.exists(directory):
        return [], [f"Dossier introuvable : {directory}"]
    if not os.path.isdir(directory):
        return [], [f"Chemin invalide (pas un dossier) : {directory}"]

    try:
        files = sorted(
            [
                os.path.join(directory, filename)
                for filename in os.listdir(directory)
                if filename.lower().endswith(".txt")
            ],
            key=lambda path: path.lower(),
        )
    except OSError:
        return [], [f"Impossible de lire le dossier : {directory}"]
    return files, []



def unique_paths(paths: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered



def load_rows_from_paths(paths: list[str]) -> tuple[list[dict], list[str], list[str]]:
    rows: list[dict] = []
    source_files: list[str] = []
    warnings: list[str] = []

    for path in unique_paths(paths):
        text, error = read_text_file(path)
        if error:
            warnings.append(error)
            continue
        source_files.append(path)
        rows.extend(parse_lines(text or ""))

    return rows, source_files, warnings



def filter_rows_by_date(rows: list[dict], start_date: date | None, end_date: date | None) -> list[dict]:
    filtered: list[dict] = []
    for row in rows:
        reglement_date = row.get("reglement_date")
        if reglement_date is None:
            continue
        if start_date is not None and reglement_date < start_date:
            continue
        if end_date is not None and reglement_date > end_date:
            continue
        filtered.append(row)
    return filtered



def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Date invalide. Format attendu AAAA-MM-JJ.") from exc



def parse_lines(text: str):
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) < 11:
            continue

        code = parts[0]
        ref2 = parts[1]
        site = parts[4]
        # Le 4e champ contient la date de règlement attendue (format YYYYMMDD).
        # On ne replie plus sur un autre champ: la couverture affichée doit
        # refléter strictement cette colonne métier. Si la valeur est invalide,
        # la ligne reste parsée mais n'influence ni la couverture ni les filtres par date.
        reglement_date = parse_reglement_date(parts[3])
        try:
            amount = float(parts[-1].replace(",", "."))
        except ValueError:
            continue

        prefix = None
        for candidate in TYPE_MAP:
            if code.startswith(candidate):
                prefix = candidate
                break
        if prefix is None:
            continue

        cam_match = re.search(r"(CAM\d+)", ref2)
        cam = cam_match.group(1) if cam_match else None

        results.append(
            {
                "code": code,
                "cam": cam,
                "site": normalize_site(site),
                "type_key": prefix,
                "type_label": TYPE_MAP[prefix],
                "amount": amount,
                "reglement_date": reglement_date,
                "reglement_date_iso": reglement_date.isoformat() if reglement_date else None,
                "raw": line,
            }
        )
    return results


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


# ── Session helpers ───────────────────────────────────────────────────────────

def date_to_iso(value: date | None) -> str | None:
    return value.isoformat() if isinstance(value, date) else None


def parse_iso_date_optional(value: str | None) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return parse_iso_date(value)
    except ValueError:
        return None


def serialize_row(row: dict) -> dict:
    return {
        **row,
        "reglement_date": date_to_iso(row.get("reglement_date")),
    }


def deserialize_row(row: dict) -> dict:
    reglement_date = parse_iso_date_optional(row.get("reglement_date"))
    return {
        **row,
        "reglement_date": reglement_date,
        "reglement_date_iso": reglement_date.isoformat() if reglement_date else None,
    }


def get_rows_bounds(rows: list[dict]) -> tuple[date | None, date | None]:
    dates = [r.get("reglement_date") for r in rows if isinstance(r.get("reglement_date"), date)]
    if not dates:
        return None, None
    return min(dates), max(dates)


def decode_uploaded_content(content: bytes) -> str:
    """Decode uploaded text using UTF-8 first, then Latin-1 fallback."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def merge_history_batch(sess: dict, rows: list[dict], filenames: list[str], now: float) -> dict:
    """Append one uploaded history batch to the session.

    Each uploaded file is stored as an independent batch.  Overlap-based removal
    is intentionally absent: settlement dates from real-world règlement files can
    span several months ahead of the transaction date, so a naïve date-range
    intersection check would silently delete valid batches uploaded earlier in the
    same session.  Callers that need to start fresh (e.g. a new upload session)
    must clear ``sess["history_batches"]`` themselves via the
    ``clear_history_before`` flag before the first call.

    Args:
        sess: Session dictionary containing current and historical uploaded data.
        rows: Parsed rows extracted from the uploaded historical file(s).
        filenames: Source filenames represented by this batch.
        now: Current unix timestamp used for retention metadata.

    Returns:
        Dict with "start" and "end" ISO dates for the uploaded coverage range.
    """
    upload_min_date, upload_max_date = get_rows_bounds(rows)
    upload_min_iso = date_to_iso(upload_min_date)
    upload_max_iso = date_to_iso(upload_max_date)

    history_batches = sess.get("history_batches", [])
    history_batches.append(
        {
            "rows": rows,
            "filenames": filenames,
            "uploaded_at": now,
            "expires_at": now + SESSION_TTL,
            "min_date": upload_min_iso,
            "max_date": upload_max_iso,
        }
    )
    sess["history_batches"] = history_batches
    sess["last_updated_at"] = now
    return {"start": upload_min_iso, "end": upload_max_iso}


def get_or_create_session(session_id: str | None) -> tuple[str, dict]:
    sid = session_id if session_id and isinstance(session_id, str) and session_id.strip() else str(uuid.uuid4())
    sess = session_store.get(sid)
    if sess is None:
        sess = {
            "current_rows": [],
            "current_filename": None,
            "current_uploaded_at": None,
            "current_expires_at": None,
            "history_batches": [],
            "last_updated_at": None,
        }
        session_store[sid] = sess
    return sid, sess


def get_session_rows(sess: dict) -> tuple[list[dict], list[str]]:
    history_rows: list[dict] = []
    history_filenames: list[str] = []
    for batch in sess.get("history_batches", []):
        history_rows.extend(batch.get("rows", []))
        history_filenames.extend(batch.get("filenames", []))
    return history_rows, history_filenames


def get_session_coverage(sess: dict) -> tuple[date | None, date | None]:
    """Compute displayed coverage from true min/max règlement dates in parsed rows."""
    starts: list[date] = []
    ends: list[date] = []

    current_rows = sess.get("current_rows", [])
    if current_rows:
        batch_min, batch_max = get_rows_bounds(current_rows)
        if batch_min:
            starts.append(batch_min)
        if batch_max:
            ends.append(batch_max)

    for batch in sess.get("history_batches", []):
        batch_min = parse_iso_date_optional(batch.get("min_date"))
        batch_max = parse_iso_date_optional(batch.get("max_date"))
        if batch_min:
            starts.append(batch_min)
        if batch_max:
            ends.append(batch_max)

    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def save_sessions() -> None:
    os.makedirs(os.path.dirname(SESSION_STORAGE_FILE), exist_ok=True)
    serializable: dict[str, dict] = {}
    for sid, sess in session_store.items():
        history_batches: list[dict] = []
        for batch in sess.get("history_batches", []):
            history_batches.append(
                {
                    "rows": [serialize_row(r) for r in batch.get("rows", [])],
                    "filenames": batch.get("filenames", []),
                    "uploaded_at": batch.get("uploaded_at"),
                    "expires_at": batch.get("expires_at"),
                    "min_date": batch.get("min_date"),
                    "max_date": batch.get("max_date"),
                }
            )
        serializable[sid] = {
            "current_rows": [serialize_row(r) for r in sess.get("current_rows", [])],
            "current_filename": sess.get("current_filename"),
            "current_uploaded_at": sess.get("current_uploaded_at"),
            "current_expires_at": sess.get("current_expires_at"),
            "history_batches": history_batches,
            "last_updated_at": sess.get("last_updated_at"),
        }
    with open(SESSION_STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f)


def load_sessions() -> None:
    if not os.path.exists(SESSION_STORAGE_FILE):
        return
    try:
        with open(SESSION_STORAGE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    for sid, sess in raw.items():
        history_batches: list[dict] = []
        for batch in sess.get("history_batches", []):
            history_batches.append(
                {
                    "rows": [deserialize_row(r) for r in batch.get("rows", []) if isinstance(r, dict)],
                    "filenames": batch.get("filenames", []),
                    "uploaded_at": batch.get("uploaded_at"),
                    "expires_at": batch.get("expires_at"),
                    "min_date": batch.get("min_date"),
                    "max_date": batch.get("max_date"),
                }
            )
        session_store[sid] = {
            "current_rows": [deserialize_row(r) for r in sess.get("current_rows", []) if isinstance(r, dict)],
            "current_filename": sess.get("current_filename"),
            "current_uploaded_at": sess.get("current_uploaded_at"),
            "current_expires_at": sess.get("current_expires_at"),
            "history_batches": history_batches,
            "last_updated_at": sess.get("last_updated_at"),
        }


def summarize_session(sess: dict) -> dict:
    now = time.time()
    history_rows, history_filenames = get_session_rows(sess)
    coverage_start, coverage_end = get_session_coverage(sess)
    current_exp = sess.get("current_expires_at")
    expiries = [
        exp
        for exp in [current_exp] + [batch.get("expires_at") for batch in sess.get("history_batches", [])]
        if isinstance(exp, (int, float))
    ]
    expires_at = max(expiries) if expiries else None
    remaining_seconds = max(0.0, expires_at - now) if expires_at is not None else 0.0
    return {
        "valid": True,
        "has_current": bool(sess.get("current_rows")),
        "has_history": bool(history_rows),
        "current_filename": sess.get("current_filename"),
        "history_filenames": history_filenames,
        "coverage_start": date_to_iso(coverage_start),
        "coverage_end": date_to_iso(coverage_end),
        "stored_file_count": (1 if sess.get("current_filename") else 0) + len(history_filenames),
        "stored_batch_count": (1 if sess.get("current_rows") else 0) + len(sess.get("history_batches", [])),
        "last_updated_at": sess.get("last_updated_at"),
        "expires_at": expires_at,
        "ttl_remaining_minutes": round(remaining_seconds / 60, 1),
        "ttl_remaining_days": round(remaining_seconds / 86400, 2),
        "retention_days": 7,
    }


def cleanup_sessions() -> None:
    now = time.time()
    changed = False
    for sid, sess in list(session_store.items()):
        current_expires = sess.get("current_expires_at")
        if current_expires and now > current_expires:
            sess["current_rows"] = []
            sess["current_filename"] = None
            sess["current_uploaded_at"] = None
            sess["current_expires_at"] = None
            changed = True
        batches = sess.get("history_batches", [])
        filtered_batches = [
            batch for batch in batches
            if not batch.get("expires_at") or now <= batch.get("expires_at")
        ]
        if len(filtered_batches) != len(batches):
            sess["history_batches"] = filtered_batches
            changed = True
        if not sess.get("current_rows") and not sess.get("history_batches"):
            del session_store[sid]
            changed = True
            continue
    if changed:
        save_sessions()


def get_session(session_id: str | None) -> dict | None:
    if not session_id or not isinstance(session_id, str):
        return None
    cleanup_sessions()
    return session_store.get(session_id)



def build_upload_payload(
    rows: list[dict],
    filename: str,
    *,
    mode: str = "upload",
    source_files: list[str] | None = None,
    date_range: dict[str, str] | None = None,
    warnings: list[str] | None = None,
    storage_status: dict | None = None,
) -> dict:
    cam_data: dict[str, dict] = defaultdict(
        lambda: {
            "total": 0.0,
            "count": 0,
            "by_type": defaultdict(lambda: {"amount": 0.0, "count": 0}),
        }
    )

    cam_rows = []

    for r in rows:
        cam = r["cam"]
        if cam is not None:
            d = cam_data[cam]
            d["total"] += r["amount"]
            d["count"] += 1
            t = d["by_type"][r["type_label"]]
            t["amount"] += r["amount"]
            t["count"] += 1
            cam_rows.append(r)

    ranked = sorted(
        [
            {
                "cam": cam,
                "site": get_site(cam),
                "total": round(v["total"], 3),
                "total_count": v["count"],
                "esp": round(v["by_type"].get("Espèces", {}).get("amount", 0.0), 3),
                "trt": round(v["by_type"].get("Traite", {}).get("amount", 0.0), 3),
                "chq": round(v["by_type"].get("Chèque", {}).get("amount", 0.0), 3),
                "esp_count": v["by_type"].get("Espèces", {}).get("count", 0),
                "trt_count": v["by_type"].get("Traite", {}).get("count", 0),
                "chq_count": v["by_type"].get("Chèque", {}).get("count", 0),
                "by_type": {
                    k: {"amount": round(vt["amount"], 3), "count": vt["count"]}
                    for k, vt in v["by_type"].items()
                },
            }
            for cam, v in cam_data.items()
        ],
        key=lambda x: x["total"],
        reverse=True,
    )

    for i, item in enumerate(ranked, 1):
        item["rank"] = i

    all_types_summary = defaultdict(lambda: {"amount": 0.0, "count": 0})
    for r in rows:
        all_types_summary[r["type_label"]]["amount"] += r["amount"]
        all_types_summary[r["type_label"]]["count"] += 1

    sites_acc: dict[str, dict] = defaultdict(
        lambda: {
            "amount": 0.0,
            "count": 0,
            "cams": set(),
            "esp_count": 0,
            "trt_count": 0,
            "chq_count": 0,
            "esp_amount": 0.0,
            "trt_amount": 0.0,
            "chq_amount": 0.0,
        }
    )
    for r in rows:
        cam = r["cam"]
        site = get_site(cam) if cam is not None else r["site"]
        sites_acc[site]["amount"] += r["amount"]
        sites_acc[site]["count"] += 1
        if cam is not None:
            sites_acc[site]["cams"].add(cam)
        label = r["type_label"]
        if label == "Espèces":
            sites_acc[site]["esp_count"] += 1
            sites_acc[site]["esp_amount"] += r["amount"]
        elif label == "Traite":
            sites_acc[site]["trt_count"] += 1
            sites_acc[site]["trt_amount"] += r["amount"]
        elif label == "Chèque":
            sites_acc[site]["chq_count"] += 1
            sites_acc[site]["chq_amount"] += r["amount"]

    sites_summary = {
        k: {
            "amount": round(v["amount"], 3),
            "count": v["count"],
            "cam_count": len(v["cams"]),
            "esp_count": v["esp_count"],
            "trt_count": v["trt_count"],
            "chq_count": v["chq_count"],
            "esp_amount": round(v["esp_amount"], 3),
            "trt_amount": round(v["trt_amount"], 3),
            "chq_amount": round(v["chq_amount"], 3),
        }
        for k, v in sorted(sites_acc.items())
    }

    return {
        "rows": ranked,
        "grand_total": round(sum(r["amount"] for r in rows), 3),
        "grand_count": len(rows),
        "lines_parsed": len(rows),
        "active_cams": len(cam_data),
        "skipped_rows": len([r for r in rows if r["cam"] is None and r["site"] == "Inconnu"]),
        "rows_without_cam": len([r for r in rows if r["cam"] is None]),
        "rows_without_cam_with_site": len([r for r in rows if r["cam"] is None and r["site"] != "Inconnu"]),
        "rows_with_cam": len(cam_rows),
        "types_summary": {
            k: {"amount": round(v["amount"], 3), "count": v["count"]}
            for k, v in all_types_summary.items()
        },
        "sites_summary": sites_summary,
        "filename": filename,
        "source_label": filename,
        "mode": mode,
        "source_files": source_files or [],
        "date_range": date_range,
        "warnings": warnings or [],
        "storage_status": storage_status,
    }


load_sessions()


@app.get("/api/default")
async def get_default_dashboard(session_id: str = None):
    sess = get_session(session_id)
    if sess and sess.get("current_rows"):
        rows = sess["current_rows"]
        filename = sess["current_filename"] or "REGLEMENT.txt"
        storage_status = summarize_session(sess)
        return JSONResponse(
            build_upload_payload(
                rows,
                filename,
                mode="default",
                source_files=[filename],
                storage_status=storage_status,
            )
        )

    # No session and no local path configured: show hosted-friendly empty state
    if not DEFAULT_CURRENT_REGLEMENT_FILE:
        return JSONResponse(
            build_upload_payload(
                [],
                "Aucun fichier importé",
                mode="default",
                warnings=[
                    "Aucun fichier du mois courant importé. "
                    "Utilisez le bouton ci-dessous pour importer un fichier."
                ],
            )
        )

    # Fallback for local development when a path is explicitly configured
    current_file = DEFAULT_CURRENT_REGLEMENT_FILE
    rows, source_files, warnings = load_rows_from_paths([current_file])
    label = f"Mois en cours · {os.path.basename(current_file)}"
    return JSONResponse(
        build_upload_payload(
            rows,
            label,
            mode="default",
            source_files=source_files,
            warnings=warnings,
            storage_status=None,
        )
    )


@app.get("/api/range")
async def get_dashboard_for_range(
    start_date: str = Query(..., description="Date de début au format AAAA-MM-JJ"),
    end_date: str = Query(..., description="Date de fin au format AAAA-MM-JJ"),
    session_id: str = None,
):
    try:
        start = parse_iso_date(start_date)
        end = parse_iso_date(end_date)
    except ValueError:
        return JSONResponse({"detail": "Date invalide. Format attendu AAAA-MM-JJ."}, status_code=400)

    if start > end:
        return JSONResponse({"detail": "La date de début doit être antérieure ou égale à la date de fin."}, status_code=400)

    label = f"Période du {start.strftime('%d/%m/%Y')} au {end.strftime('%d/%m/%Y')}"

    sess = get_session(session_id)
    if sess is not None:
        # Use session-based uploaded data
        current_rows: list[dict] = sess.get("current_rows", [])
        history_rows, history_filenames = get_session_rows(sess)
        all_rows = history_rows + current_rows
        warnings: list[str] = []
        if not current_rows and not history_rows:
            warnings.append("Aucun fichier chargé. Veuillez importer les fichiers pour filtrer par date.")
        elif not history_rows:
            warnings.append("Aucun fichier historique chargé. Le filtre date n'utilise que le fichier mensuel.")
        source_files = [f for f in ([sess.get("current_filename")] + history_filenames) if f]
        storage_status = summarize_session(sess)
    else:
        # No session: show hosted-friendly message when no local paths are configured
        if not DEFAULT_HISTORY_REGLEMENTS_DIR and not DEFAULT_CURRENT_REGLEMENT_FILE:
            return JSONResponse(
                build_upload_payload(
                    [],
                    label,
                    mode="date_range",
                    date_range={"start": start.isoformat(), "end": end.isoformat()},
                    warnings=[
                        "Aucun fichier importé. "
                        "Veuillez importer les fichiers pour filtrer par date."
                    ],
                )
            )
        # Fallback for local development when paths are explicitly configured
        history_files, warnings = list_reglement_files(DEFAULT_HISTORY_REGLEMENTS_DIR)
        all_rows, source_files, load_warnings = load_rows_from_paths(history_files + [DEFAULT_CURRENT_REGLEMENT_FILE])
        warnings = list(warnings) + load_warnings
        storage_status = None

    filtered_rows = filter_rows_by_date(all_rows, start, end)

    return JSONResponse(
        build_upload_payload(
            filtered_rows,
            label,
            mode="date_range",
            source_files=source_files,
            date_range={"start": start.isoformat(), "end": end.isoformat()},
            warnings=warnings,
            storage_status=storage_status,
        )
    )


@app.get("/api/filter")
async def get_dashboard_for_filter(
    start_date: str = Query(..., alias="start", description="Date de début au format AAAA-MM-JJ"),
    end_date: str = Query(..., alias="end", description="Date de fin au format AAAA-MM-JJ"),
    session_id: str = None,
):
    return await get_dashboard_for_range(start_date=start_date, end_date=end_date, session_id=session_id)


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    rows = parse_lines(text)
    return JSONResponse(
        build_upload_payload(rows, file.filename, mode="upload", source_files=[file.filename])
    )


@app.post("/api/upload/current")
async def upload_current(file: UploadFile = File(...), session_id: str = Form(default=None)):
    content = await file.read()
    text = decode_uploaded_content(content)

    rows = parse_lines(text)
    fname = file.filename or "REGLEMENT.txt"
    cleanup_sessions()
    sid, sess = get_or_create_session(session_id)
    now = time.time()
    sess["current_rows"] = rows
    sess["current_filename"] = fname
    sess["current_uploaded_at"] = now
    sess["current_expires_at"] = now + SESSION_TTL
    sess["last_updated_at"] = now
    save_sessions()

    payload = build_upload_payload(
        rows,
        fname,
        mode="upload",
        source_files=[fname],
        storage_status=summarize_session(sess),
    )
    payload["session_id"] = sid
    payload["retention_days"] = 7
    return JSONResponse(payload)


@app.post("/api/upload/history-file")
async def upload_history_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session_id: str = Form(default=None),
    clear_history_before: str = Form(default=None),
):
    fname = file.filename or "history.txt"
    sid, sess = get_or_create_session(session_id)
    content = await file.read()
    rows = parse_lines(decode_uploaded_content(content))

    if not rows:
        return JSONResponse(
            {
                "success": False,
                "session_id": sid,
                "filename": fname,
                "error": f"Aucune ligne valide trouvée dans {fname}. Vérifiez le format texte ';' attendu.",
            }
        )

    # Clear all existing history batches when starting a fresh batch upload
    if isinstance(clear_history_before, str) and clear_history_before.lower() in ("true", "1", "yes"):
        sess["history_batches"] = []

    now = time.time()
    uploaded_coverage = merge_history_batch(sess, rows, [fname], now)
    # Persist to disk in the background so the response returns immediately
    background_tasks.add_task(save_sessions)
    return JSONResponse(
        {
            "success": True,
            "session_id": sid,
            "filename": fname,
            "row_count": len(rows),
            "uploaded_coverage": uploaded_coverage,
            "retention_days": 7,
            "storage_status": summarize_session(sess),
        }
    )


@app.post("/api/upload/history")
async def upload_history(files: list[UploadFile] = File(...), session_id: str = Form(default=None)):
    cleanup_sessions()
    sid, sess = get_or_create_session(session_id)
    all_rows: list[dict] = []
    filenames: list[str] = []
    uploaded_coverage = {"start": None, "end": None}
    for file in files:
        content = await file.read()
        rows = parse_lines(decode_uploaded_content(content))
        if not rows:
            continue
        fname = file.filename or "history.txt"
        now = time.time()
        uploaded_coverage = merge_history_batch(sess, rows, [fname], now)
        all_rows.extend(rows)
        filenames.append(fname)

    if not all_rows:
        return JSONResponse(
            {
                "session_id": sid,
                "retention_days": 7,
                "uploaded_coverage": {"start": None, "end": None},
                "warnings": ["Aucun fichier historique valide n'a été importé."],
                "source_files": [],
                "storage_status": summarize_session(sess),
            }
        )

    save_sessions()

    label = f"{len(filenames)} fichier(s) historique"
    payload = build_upload_payload(
        all_rows,
        label,
        mode="upload",
        source_files=filenames,
        storage_status=summarize_session(sess),
    )
    payload["session_id"] = sid
    payload["retention_days"] = 7
    payload["uploaded_coverage"] = uploaded_coverage
    return JSONResponse(payload)


@app.get("/api/session/status")
async def session_status(session_id: str = None):
    sess = get_session(session_id)
    if not sess:
        return JSONResponse({"valid": False, "has_current": False, "has_history": False})
    return JSONResponse(summarize_session(sess))
