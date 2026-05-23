from collections import defaultdict
from datetime import date, datetime
import json
import logging
import os
import re
import sys
import time
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
logger = logging.getLogger(__name__)
app.mount("/static", StaticFiles(directory="static"), name="static")

TYPE_MAP = {
    "CESP": "Espèces",
    "CTRT": "Traite",
    "CCHQR": "Chèque",
}

VALID_SITES = {"SFX", "MAH", "NAB", "SSE", "TUN"}

# ── Source configuration (internal-network paths) ─────────────────────────────
# Accepts plain filesystem paths or file:// URIs with optional percent-encoding.
# Override via environment variables for alternate deployments.
DEFAULT_CURRENT_REGLEMENT_FILE = os.environ.get(
    "CURRENT_REGLEMENT_FILE",
    "file://172.16.100.34/Users/chokri.jdir/Desktop/TDB_SINDBAD/REGLEMENT.txt",
)
DEFAULT_HISTORY_REGLEMENTS_DIR = os.environ.get(
    "HISTORY_REGLEMENTS_DIR",
    "file://172.16.100.34/Users/chokri.jdir/Desktop/TDB_SINDBAD_Mens/R%C3%A9glements/",
)

# ── In-memory data cache ──────────────────────────────────────────────────────
_cache: dict = {
    "all_rows": [],          # All parsed rows (history + current) for date-range filtering
    "current_rows": [],      # Rows from the current-month file only (for default view)
    "source_files": [],      # All loaded source file paths
    "current_source_files": [],  # Only the current-month source file(s)
    "warnings": [],          # All warnings (history + current)
    "current_warnings": [],  # Warnings related to the current-month file only
    "loaded_at": None,        # Unix timestamp of last successful load
    "coverage_start": None,   # ISO date string of earliest row date
    "coverage_end": None,     # ISO date string of latest row date
    "history_file_count": 0,
    "source_diagnostics": {},
}

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


def is_file_uri(source: str) -> bool:
    return bool(source and source.startswith("file://"))


def get_file_uri_mount_root() -> str | None:
    mount_root = os.environ.get("FILE_URI_MOUNT_ROOT", "").strip()
    return mount_root or None


def resolve_source_path(source: str) -> tuple[str, dict]:
    if not is_file_uri(source):
        return source, {"path_strategy": "raw_path"}

    parsed = urlsplit(source)
    path_part = unquote(parsed.path or "")
    host = parsed.netloc
    is_remote = bool(host and host.lower() not in {"localhost", "127.0.0.1", "::1"})
    mount_root = get_file_uri_mount_root()

    if is_remote:
        if os.name == "nt":
            resolved = f"\\\\{host}{path_part.replace('/', '\\')}".rstrip("\\")
            return resolved, {"path_strategy": "windows_unc", "uri_host": host}
        if mount_root:
            segments = [
                seg
                for seg in path_part.split("/")
                if seg and seg not in {".", ".."}
            ]
            resolved = os.path.join(mount_root, *segments)
            return resolved, {
                "path_strategy": "mounted_fallback",
                "uri_host": host,
                "mount_root": mount_root,
                "path_sanitized": True,
            }
        resolved = f"//{host}{path_part}".rstrip("/")
        return resolved, {
            "path_strategy": "posix_unc_like",
            "uri_host": host,
            "mount_root": None,
        }

    if os.name == "nt":
        windows_path = path_part.replace("/", "\\")
        if re.match(r"^\\[A-Za-z]:\\", windows_path):
            windows_path = windows_path[1:]
        return windows_path, {"path_strategy": "windows_local_file_uri"}

    return path_part, {"path_strategy": "posix_local_file_uri"}


def file_uri_to_fs_path(uri: str) -> str:
    """Convert a file:// URI to an OS path for directory introspection helpers."""
    resolved, _ = resolve_source_path(uri)
    return resolved


def inspect_source_path(source: str, *, expect_directory: bool) -> dict:
    diagnostic = {
        "configured_source": source or "",
        "resolved_path": None,
        "runtime_os": os.name,
        "runtime_platform": sys.platform,
        "path_strategy": None,
        "mount_root": get_file_uri_mount_root(),
        "exists": False,
        "is_directory": False,
        "is_file": False,
        "readable": False,
        "directory_exists": False,
        "error_kind": None,
        "os_error_detail": None,
    }

    if not source:
        diagnostic["error_kind"] = "not_configured"
        return diagnostic

    resolved_source, resolution_meta = resolve_source_path(source)
    diagnostic.update(resolution_meta)
    diagnostic["resolved_path"] = resolved_source

    try:
        diagnostic["exists"] = os.path.exists(resolved_source)
        diagnostic["is_directory"] = os.path.isdir(resolved_source)
        diagnostic["is_file"] = os.path.isfile(resolved_source)
        diagnostic["directory_exists"] = diagnostic["is_directory"]
        if diagnostic["exists"]:
            diagnostic["readable"] = os.access(resolved_source, os.R_OK)
    except OSError as exc:
        diagnostic["error_kind"] = "os_error"
        diagnostic["os_error_detail"] = str(exc)
        return diagnostic

    if not diagnostic["exists"]:
        diagnostic["error_kind"] = "missing"
        return diagnostic
    if not diagnostic["readable"]:
        diagnostic["error_kind"] = "access_denied"
        return diagnostic
    if expect_directory and not diagnostic["is_directory"]:
        diagnostic["error_kind"] = "not_directory"
        return diagnostic
    if not expect_directory and diagnostic["is_directory"]:
        diagnostic["error_kind"] = "is_directory"
        return diagnostic
    if not expect_directory and not diagnostic["is_file"]:
        diagnostic["error_kind"] = "not_file"
        return diagnostic

    return diagnostic


def get_source_label(source: str) -> str:
    if not source:
        return "—"
    if not is_file_uri(source):
        return os.path.basename(source)
    parsed = urlsplit(source)
    name = os.path.basename(unquote(parsed.path.rstrip("/")))
    return name or parsed.netloc or source

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



def read_text_file(source: str) -> tuple[str | None, str | None]:
    resolved_source = source
    try:
        if is_file_uri(source):
            resolved_source = file_uri_to_fs_path(source)
            with open(resolved_source, "rb") as f:
                content = f.read()
        else:
            with open(source, "rb") as f:
                content = f.read()
        try:
            return content.decode("utf-8"), None
        except UnicodeDecodeError:
            return content.decode("latin-1"), None
    except FileNotFoundError:
        return (
            None,
            f"Fichier introuvable : {resolved_source} "
            f"(source: {source}, runtime: {os.name}/{sys.platform})",
        )
    except PermissionError:
        return (
            None,
            f"Accès refusé : {resolved_source} "
            f"(source: {source}, runtime: {os.name}/{sys.platform})",
        )
    except IsADirectoryError:
        return (
            None,
            f"Chemin invalide (dossier) : {resolved_source} "
            f"(source: {source}, runtime: {os.name}/{sys.platform})",
        )
    except OSError as exc:
        return (
            None,
            f"Impossible de lire le fichier : {resolved_source} "
            f"(source: {source}, runtime: {os.name}/{sys.platform}, erreur: {exc.__class__.__name__})",
        )
    return (
        None,
        f"Impossible de décoder le fichier : {resolved_source} "
        f"(source: {source}, runtime: {os.name}/{sys.platform})",
    )



def list_reglement_files(directory: str) -> tuple[list[str], list[str]]:
    lookup_dir = file_uri_to_fs_path(directory) if is_file_uri(directory) else directory

    if not os.path.exists(lookup_dir):
        return [], [
            f"Dossier introuvable : {lookup_dir} "
            f"(source: {directory}, runtime: {os.name}/{sys.platform})"
        ]
    if not os.path.isdir(lookup_dir):
        return [], [
            f"Chemin invalide (pas un dossier) : {lookup_dir} "
            f"(source: {directory}, runtime: {os.name}/{sys.platform})"
        ]

    try:
        filenames = sorted(
            [filename for filename in os.listdir(lookup_dir) if filename.lower().endswith(".txt")],
            key=lambda filename: filename.lower(),
        )
    except PermissionError:
        return [], [
            f"Accès refusé au dossier : {lookup_dir} "
            f"(source: {directory}, runtime: {os.name}/{sys.platform})"
        ]
    except OSError as exc:
        return [], [
            f"Impossible de lire le dossier : {lookup_dir} "
            f"(source: {directory}, runtime: {os.name}/{sys.platform}, erreur: {exc.__class__.__name__})"
        ]
    if is_file_uri(directory):
        base_uri = directory if directory.endswith("/") else f"{directory}/"
        return [f"{base_uri}{quote(filename)}" for filename in filenames], []
    return [os.path.join(directory, filename) for filename in filenames], []



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
        # La date de règlement utilisée pour la couverture/recherche est le
        # 1er champ date (3e colonne, format YYYYMMDD). On ignore les autres
        # colonnes date pour éviter d'étendre la plage affichée/filtrée à tort.
        reglement_date = parse_reglement_date(parts[2])
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def date_to_iso(value: date | None) -> str | None:
    return value.isoformat() if isinstance(value, date) else None


def get_rows_bounds(rows: list[dict]) -> tuple[date | None, date | None]:
    dates = [r.get("reglement_date") for r in rows if isinstance(r.get("reglement_date"), date)]
    if not dates:
        return None, None
    return min(dates), max(dates)


# ── Cache management ──────────────────────────────────────────────────────────

def reload_cache() -> None:
    """Read all règlement files from the configured source paths and populate
    the in-memory cache.  Existing cached data is replaced atomically.

    Configured ``file://`` URIs (including remote ones such as
    ``file://172.16.100.34/...``) are resolved to OS/network-share-readable
    paths by :func:`file_uri_to_fs_path` before reading.  On POSIX systems a
    remote URI host component is treated as a UNC host (``//host/path``); on
    Windows it becomes ``\\host\\path``.

    All source access happens exclusively on the backend.  The frontend must
    never fetch ``file://`` resources directly.
    """
    current_uri = DEFAULT_CURRENT_REGLEMENT_FILE
    history_uri = DEFAULT_HISTORY_REGLEMENTS_DIR

    current_path = current_uri or ""
    history_dir = history_uri or ""

    warnings: list[str] = []
    current_diagnostic = inspect_source_path(current_path, expect_directory=False)
    history_diagnostic = inspect_source_path(history_dir, expect_directory=True)

    if not current_path and not history_dir:
        warnings.append(
            "Aucun fichier configuré. "
            "Définissez CURRENT_REGLEMENT_FILE et HISTORY_REGLEMENTS_DIR."
        )
        _cache.update({
            "all_rows": [],
            "current_rows": [],
            "source_files": [],
            "current_source_files": [],
            "warnings": warnings,
            "current_warnings": [],
            "loaded_at": time.time(),
            "coverage_start": None,
            "coverage_end": None,
            "history_file_count": 0,
            "source_diagnostics": {
                "current": current_diagnostic,
                "history": history_diagnostic,
            },
        })
        return

    history_files: list[str] = []
    if history_dir:
        history_files, dir_warnings = list_reglement_files(history_dir)
        warnings.extend(dir_warnings)

    # Load history rows and current rows separately so the default view can
    # show only the current-month file while range queries use all data.
    history_rows: list[dict] = []
    history_sources: list[str] = []
    if history_files:
        history_rows, history_sources, hist_warnings = load_rows_from_paths(history_files)
        warnings.extend(hist_warnings)

    current_rows: list[dict] = []
    current_sources: list[str] = []
    cur_warnings: list[str] = []
    if current_path:
        current_rows, current_sources, cur_warnings = load_rows_from_paths([current_path])
        warnings.extend(cur_warnings)

    all_rows = history_rows + current_rows
    all_sources = history_sources + current_sources
    coverage_start, coverage_end = get_rows_bounds(all_rows)

    _cache.update({
        "all_rows": all_rows,
        "current_rows": current_rows,
        "source_files": all_sources,
        "current_source_files": current_sources,
        "warnings": warnings,
        "current_warnings": cur_warnings if current_path else [],
        "loaded_at": time.time(),
        "coverage_start": date_to_iso(coverage_start),
        "coverage_end": date_to_iso(coverage_end),
        "history_file_count": len(history_files),
        "source_diagnostics": {
            "current": current_diagnostic,
            "history": history_diagnostic,
        },
    })


def get_or_reload_cache() -> dict:
    """Return the cache, triggering a load from source paths if not yet loaded."""
    if _cache["loaded_at"] is None:
        reload_cache()
    return _cache


def get_source_status() -> dict:
    """Return a status dict describing the current cache state for the UI."""
    cache = get_or_reload_cache()
    loaded_at = cache["loaded_at"]
    return {
        "loaded": loaded_at is not None,
        "loaded_at": loaded_at,
        "coverage_start": cache["coverage_start"],
        "coverage_end": cache["coverage_end"],
        "source_file_count": len(cache["source_files"]),
        "history_file_count": cache["history_file_count"],
        "has_data": bool(cache["all_rows"]),
        "warnings": cache["warnings"],
        "current_source_label": (
            get_source_label(DEFAULT_CURRENT_REGLEMENT_FILE)
            if DEFAULT_CURRENT_REGLEMENT_FILE
            else "—"
        ),
        "history_source_label": (
            get_source_label(DEFAULT_HISTORY_REGLEMENTS_DIR)
            if DEFAULT_HISTORY_REGLEMENTS_DIR
            else "—"
        ),
        "runtime_label": f"{os.name}/{sys.platform}",
        "current_diagnostic": cache.get("source_diagnostics", {}).get("current", {}),
        "history_diagnostic": cache.get("source_diagnostics", {}).get("history", {}),
    }


@app.on_event("startup")
async def startup() -> None:
    reload_cache()


def build_upload_payload(
    rows: list[dict],
    filename: str,
    *,
    mode: str = "default",
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


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/default")
async def get_default_dashboard():
    if not DEFAULT_CURRENT_REGLEMENT_FILE:
        return JSONResponse(
            build_upload_payload(
                [],
                "Aucun fichier configuré",
                mode="default",
                warnings=["Aucun fichier configuré. "
                          "Définissez CURRENT_REGLEMENT_FILE."],
            )
        )

    cache = get_or_reload_cache()
    current_source = DEFAULT_CURRENT_REGLEMENT_FILE
    label = f"Mois en cours · {get_source_label(current_source)}"

    return JSONResponse(
        build_upload_payload(
            cache["current_rows"],
            label,
            mode="default",
            source_files=cache["current_source_files"],
            warnings=cache["current_warnings"],
        )
    )


@app.get("/api/range")
async def get_dashboard_for_range(
    start_date: str = Query(..., description="Date de début au format AAAA-MM-JJ"),
    end_date: str = Query(..., description="Date de fin au format AAAA-MM-JJ"),
):
    try:
        start = parse_iso_date(start_date)
        end = parse_iso_date(end_date)
    except ValueError:
        return JSONResponse({"detail": "Date invalide. Format attendu AAAA-MM-JJ."}, status_code=400)

    if start > end:
        return JSONResponse(
            {"detail": "La date de début doit être antérieure ou égale à la date de fin."},
            status_code=400,
        )

    label = f"Période du {start.strftime('%d/%m/%Y')} au {end.strftime('%d/%m/%Y')}"

    cache = get_or_reload_cache()

    if not DEFAULT_HISTORY_REGLEMENTS_DIR and not DEFAULT_CURRENT_REGLEMENT_FILE:
        return JSONResponse(
            build_upload_payload(
                [],
                label,
                mode="date_range",
                date_range={"start": start.isoformat(), "end": end.isoformat()},
                warnings=["Aucun fichier configuré. "
                          "Définissez CURRENT_REGLEMENT_FILE et HISTORY_REGLEMENTS_DIR."],
            )
        )

    filtered_rows = filter_rows_by_date(cache["all_rows"], start, end)

    return JSONResponse(
        build_upload_payload(
            filtered_rows,
            label,
            mode="date_range",
            source_files=cache["source_files"],
            date_range={"start": start.isoformat(), "end": end.isoformat()},
            warnings=cache["warnings"],
        )
    )


@app.get("/api/filter")
async def get_dashboard_for_filter(
    start_date: str = Query(..., alias="start", description="Date de début au format AAAA-MM-JJ"),
    end_date: str = Query(..., alias="end", description="Date de fin au format AAAA-MM-JJ"),
):
    return await get_dashboard_for_range(start_date=start_date, end_date=end_date)


@app.get("/api/status")
async def source_status():
    """Return metadata about the loaded data cache for the source status panel."""
    return JSONResponse(get_source_status())


@app.post("/api/refresh")
async def refresh_data():
    """Force a reload of all règlement data from the configured source paths."""
    reload_cache()
    return JSONResponse(get_source_status())
