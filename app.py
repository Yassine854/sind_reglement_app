from collections import defaultdict
from datetime import date, datetime
import os
import re
import time
import uuid

from fastapi import FastAPI, File, Form, Query, UploadFile
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
DEFAULT_CURRENT_REGLEMENT_FILE = r"D:\TDB SINDBAD\Fichiers Sources\REGLEMENT.txt"
DEFAULT_HISTORY_REGLEMENTS_DIR = r"D:\TDB SINDBAD\Fichiers Sources\Réglements"

# ── In-memory session store (TTL ~15 min) ────────────────────────────────────
SESSION_TTL = 15 * 60  # seconds
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
        # Le 4e champ contient la date de règlement attendue ; on replie sur le 3e si besoin.
        reglement_date = parse_reglement_date(parts[3]) or parse_reglement_date(parts[2])
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

def cleanup_sessions() -> None:
    now = time.time()
    expired = [sid for sid, v in list(session_store.items()) if now - v["uploaded_at"] > SESSION_TTL]
    for sid in expired:
        del session_store[sid]


def get_session(session_id: str | None) -> dict | None:
    if not session_id or not isinstance(session_id, str):
        return None
    cleanup_sessions()
    sess = session_store.get(session_id)
    if sess is None:
        return None
    if time.time() - sess["uploaded_at"] > SESSION_TTL:
        del session_store[session_id]
        return None
    return sess



def build_upload_payload(
    rows: list[dict],
    filename: str,
    *,
    mode: str = "upload",
    source_files: list[str] | None = None,
    date_range: dict[str, str] | None = None,
    warnings: list[str] | None = None,
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
    }


@app.get("/api/default")
async def get_default_dashboard(session_id: str = None):
    sess = get_session(session_id)
    if sess and sess.get("current_rows"):
        rows = sess["current_rows"]
        filename = sess["current_filename"] or "REGLEMENT.txt"
        return JSONResponse(
            build_upload_payload(rows, filename, mode="default", source_files=[filename])
        )

    # Fallback: read from default file path (works locally, returns warning on Render)
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
        history_rows: list[dict] = sess.get("history_rows", [])
        all_rows = history_rows + current_rows
        warnings: list[str] = []
        if not current_rows and not history_rows:
            warnings.append("Aucun fichier chargé. Veuillez importer les fichiers pour filtrer par date.")
        elif not history_rows:
            warnings.append("Aucun fichier historique chargé. Le filtre date n'utilise que le fichier mensuel.")
        source_files = [f for f in ([sess.get("current_filename")] + sess.get("history_filenames", [])) if f]
    else:
        # Fallback: read from filesystem (for local use / backward-compat)
        history_files, warnings = list_reglement_files(DEFAULT_HISTORY_REGLEMENTS_DIR)
        all_rows, source_files, load_warnings = load_rows_from_paths(history_files + [DEFAULT_CURRENT_REGLEMENT_FILE])
        warnings = list(warnings) + load_warnings

    filtered_rows = filter_rows_by_date(all_rows, start, end)

    return JSONResponse(
        build_upload_payload(
            filtered_rows,
            label,
            mode="date_range",
            source_files=source_files,
            date_range={"start": start.isoformat(), "end": end.isoformat()},
            warnings=warnings,
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
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    rows = parse_lines(text)
    fname = file.filename or "REGLEMENT.txt"
    sid = session_id if session_id and isinstance(session_id, str) and session_id.strip() else str(uuid.uuid4())

    cleanup_sessions()
    if sid in session_store:
        session_store[sid]["current_rows"] = rows
        session_store[sid]["current_filename"] = fname
        session_store[sid]["uploaded_at"] = time.time()
    else:
        session_store[sid] = {
            "current_rows": rows,
            "current_filename": fname,
            "history_rows": [],
            "history_filenames": [],
            "uploaded_at": time.time(),
        }

    payload = build_upload_payload(rows, fname, mode="upload", source_files=[fname])
    payload["session_id"] = sid
    payload["ttl_minutes"] = 15
    return JSONResponse(payload)


@app.post("/api/upload/history")
async def upload_history(files: list[UploadFile] = File(...), session_id: str = Form(default=None)):
    all_rows: list[dict] = []
    filenames: list[str] = []

    for file in files:
        content = await file.read()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        fname = file.filename or "history.txt"
        all_rows.extend(parse_lines(text))
        filenames.append(fname)

    sid = session_id if session_id and isinstance(session_id, str) and session_id.strip() else str(uuid.uuid4())

    cleanup_sessions()
    if sid in session_store:
        session_store[sid]["history_rows"] = all_rows
        session_store[sid]["history_filenames"] = filenames
        session_store[sid]["uploaded_at"] = time.time()
    else:
        session_store[sid] = {
            "current_rows": [],
            "current_filename": None,
            "history_rows": all_rows,
            "history_filenames": filenames,
            "uploaded_at": time.time(),
        }

    label = f"{len(filenames)} fichier(s) historique"
    payload = build_upload_payload(all_rows, label, mode="upload", source_files=filenames)
    payload["session_id"] = sid
    payload["ttl_minutes"] = 15
    return JSONResponse(payload)


@app.get("/api/session/status")
async def session_status(session_id: str = None):
    sess = get_session(session_id)
    if not sess:
        return JSONResponse({"valid": False, "has_current": False, "has_history": False})
    remaining = max(0.0, SESSION_TTL - (time.time() - sess["uploaded_at"]))
    return JSONResponse({
        "valid": True,
        "has_current": bool(sess.get("current_rows")),
        "has_history": bool(sess.get("history_rows")),
        "current_filename": sess.get("current_filename"),
        "history_filenames": sess.get("history_filenames", []),
        "ttl_remaining_minutes": round(remaining / 60, 1),
    })
