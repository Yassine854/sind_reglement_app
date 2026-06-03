import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from urllib.parse import quote, unquote, urlsplit

import aiofiles
from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook

app = FastAPI()
logger = logging.getLogger(__name__)
app.mount("/static", StaticFiles(directory="static"), name="static")

TYPE_MAP = {
    "CESP": "Espèces",
    "CTRT": "Traite",
    "CCHQR": "Chèque",
}

VALID_SITES = {"SFX", "MAH", "NAB", "SSE", "TUN"}

# ── Upload configuration ───────────────────────────────────────────────────────
MAX_TOTAL_UPLOAD_SIZE = int(os.environ.get("MAX_TOTAL_UPLOAD_SIZE", 500 * 1024 * 1024))  # 500 MB total
MAX_SINGLE_FILE_SIZE = int(os.environ.get("MAX_SINGLE_FILE_SIZE", 100 * 1024 * 1024))   # 100 MB per file
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", 8192))  # 8 KB


def format_size(bytes_size: int) -> str:
    """Format bytes to human-readable size (Ko, Mo, Go)."""
    if bytes_size < 1024:
        return f"{bytes_size} octets"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} Ko"
    elif bytes_size < 1024 * 1024 * 1024:
        return f"{bytes_size / (1024 * 1024):.1f} Mo"
    else:
        return f"{bytes_size / (1024 * 1024 * 1024):.2f} Go"

# ── Source configuration ───────────────────────────────────────────────────────
# Folder upload/import is the primary workflow. These env vars remain as an
# optional legacy fallback when no folder has been uploaded.
DEFAULT_CURRENT_REGLEMENT_FILE = os.environ.get(
    "CURRENT_REGLEMENT_FILE",
    "",
)
DEFAULT_HISTORY_REGLEMENTS_DIR = os.environ.get(
    "HISTORY_REGLEMENTS_DIR",
    "",
)
WINDOWS_SYNC_CACHE_DIR = os.environ.get(
    "WINDOWS_SYNC_CACHE_DIR",
    os.path.join(tempfile.gettempdir(), "sind_reglement_app", "reglements_cache"),
)

# ── In-memory data cache ──────────────────────────────────────────────────────
_cache: dict = {
    "all_rows": [],          # All parsed rows (history + current) for date-range filtering
    "current_rows": [],      # Rows from the current-month file only (for default view)
    "source_files": [],      # All loaded source file paths
    "current_source_files": [],  # Only the current-month source file(s)
    "article_lookup": {},
    "etatmarge_lookup": {},
    "etatmarge_warnings": [],
    "all_facture_lines": [],
    "all_big_factures": [],
    "current_big_factures": [],
    "facture_source_files": [],
    "current_facture_source_files": [],
    "warnings": [],          # All warnings (history + current)
    "current_warnings": [],  # Warnings related to the current-month file only
    "loaded_at": None,        # Unix timestamp of last successful load
    "coverage_start": None,   # ISO date string of earliest row date
    "coverage_end": None,     # ISO date string of latest row date
    "history_file_count": 0,
    "facture_coverage_start": None,
    "facture_coverage_end": None,
    "facture_history_file_count": 0,
    "source_diagnostics": {},
    "sync": {},
    "import_context": {},
}
IMPORT_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_import_tasks: set[asyncio.Task] = set()

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


def normalize_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).casefold()


def is_reglements_dir_name(name: str) -> bool:
    return normalize_token(name) == "reglements"


def is_reglement_text_filename(name: str) -> bool:
    base, ext = os.path.splitext(name or "")
    return ext.casefold() == ".txt" and "reglement" in normalize_token(base)


def is_factures_dir_name(name: str) -> bool:
    return normalize_token(name) == "factures"


def is_named_source_file(name: str, expected_name: str) -> bool:
    base, ext = os.path.splitext(name or "")
    normalized = normalize_token(base or name)
    return normalized == normalize_token(expected_name) and ext.casefold() in {"", ".txt", ".csv"}


def is_named_excel_source_file(name: str, expected_name: str) -> bool:
    base, ext = os.path.splitext(name or "")
    normalized = normalize_token(base or name)
    return normalized == normalize_token(expected_name) and ext.casefold() in {"", ".txt", ".csv", ".xlsx", ".xlsm"}


def normalize_article_code(value: str | None) -> str:
    return (value or "").strip().upper()


def discover_uploaded_sources(root_dir: str) -> dict:
    current_file: str | None = None
    history_dir: str | None = None
    article_file: str | None = None
    etatmarge_file: str | None = None
    current_facture_file: str | None = None
    factures_dir: str | None = None

    try:
        root_entries = sorted(os.listdir(root_dir), key=str.casefold)
    except OSError:
        root_entries = []

    for name in root_entries:
        candidate = os.path.join(root_dir, name)
        if not current_file and os.path.isfile(candidate) and name.casefold() == "reglement.txt":
            current_file = candidate
        if not history_dir and os.path.isdir(candidate) and is_reglements_dir_name(name):
            history_dir = candidate
        if not article_file and os.path.isfile(candidate) and is_named_source_file(name, "ARTICLE"):
            article_file = candidate
        if not etatmarge_file and os.path.isfile(candidate) and is_named_excel_source_file(name, "etatmarge"):
            etatmarge_file = candidate
        if not current_facture_file and os.path.isfile(candidate) and is_named_source_file(name, "FACTURE"):
            current_facture_file = candidate
        if not factures_dir and os.path.isdir(candidate) and is_factures_dir_name(name):
            factures_dir = candidate
        if current_file and history_dir and article_file and current_facture_file and factures_dir and etatmarge_file:
            break

    warnings: list[str] = []
    if not current_file:
        warnings.append("Le fichier attendu REGLEMENT.txt est introuvable dans le dossier importé.")
    if not history_dir:
        warnings.append("Le dossier attendu Réglements est introuvable dans le dossier importé.")

    return {
        "root_path": root_dir,
        "root_name": os.path.basename(os.path.normpath(root_dir)) or "Fichiers Sources",
        "current_path": current_file or "",
        "history_path": history_dir or "",
        "article_path": article_file or "",
        "etatmarge_path": etatmarge_file or "",
        "facture_path": current_facture_file or "",
        "factures_path": factures_dir or "",
        "current_found": bool(current_file),
        "history_found": bool(history_dir),
        "article_found": bool(article_file),
        "etatmarge_found": bool(etatmarge_file),
        "facture_found": bool(current_facture_file),
        "factures_found": bool(factures_dir),
        "warnings": warnings,
    }


def get_file_uri_mount_root() -> str | None:
    mount_root = os.environ.get("FILE_URI_MOUNT_ROOT", "").strip()
    return mount_root or None


def get_windows_sync_cache_dir() -> str:
    return os.path.abspath(WINDOWS_SYNC_CACHE_DIR)


def source_requires_windows_sync(source: str) -> bool:
    if not source:
        return False
    if is_file_uri(source):
        host = (urlsplit(source).netloc or "").strip().lower()
        return bool(host and host not in {"localhost", "127.0.0.1", "::1"})
    return source.startswith("\\\\") or source.startswith("//")


def sync_windows_local_sources(current_source: str, history_source: str) -> dict:
    cache_root = get_windows_sync_cache_dir()
    result = {
        "required": True,
        "mode": "windows_local_copy",
        "status": "idle",
        "message": "Synchronisation locale non exécutée.",
        "cache_root": cache_root,
        "local_current_path": None,
        "local_history_path": None,
        "copied_history_files": 0,
        "warnings": [],
    }

    if os.name != "nt":
        result["status"] = "unsupported_runtime"
        result["message"] = (
            "Synchronisation locale Windows requise. "
            "Lancez le backend sur Windows avec accès aux chemins UNC."
        )
        result["warnings"].append(result["message"])
        return result

    os.makedirs(cache_root, exist_ok=True)

    copied_any = False
    copied_history_files = 0
    current_cache_dir = os.path.join(cache_root, "monthly")
    history_cache_dir = os.path.join(cache_root, "history")

    if current_source:
        resolved_current, _ = resolve_source_path(current_source)
        monthly_name = os.path.basename(resolved_current) or "REGLEMENT.txt"
        local_current_path = os.path.join(current_cache_dir, monthly_name)
        try:
            if not os.path.isfile(resolved_current):
                result["warnings"].append(
                    f"Fichier mensuel introuvable pour la copie locale : {resolved_current}"
                )
            else:
                os.makedirs(current_cache_dir, exist_ok=True)
                shutil.copy2(resolved_current, local_current_path)
                result["local_current_path"] = local_current_path
                copied_any = True
        except OSError as exc:
            result["warnings"].append(
                f"Échec copie mensuelle locale ({resolved_current}) : "
                f"{exc.__class__.__name__}"
            )

    if history_source:
        resolved_history, _ = resolve_source_path(history_source)
        local_history_path = history_cache_dir
        history_cache_tmp = os.path.join(cache_root, "history_tmp")
        try:
            if not os.path.isdir(resolved_history):
                result["warnings"].append(
                    f"Dossier historique introuvable pour la copie locale : {resolved_history}"
                )
            else:
                if os.path.isdir(history_cache_tmp):
                    shutil.rmtree(history_cache_tmp)
                shutil.copytree(resolved_history, history_cache_tmp, dirs_exist_ok=True)
                if os.path.isdir(local_history_path):
                    shutil.rmtree(local_history_path)
                os.replace(history_cache_tmp, local_history_path)
                copied_history_files = len(
                    [
                        filename
                        for filename in os.listdir(local_history_path)
                        if filename.lower().endswith(".txt")
                    ]
                )
                result["local_history_path"] = local_history_path
                copied_any = True
        except OSError as exc:
            result["warnings"].append(
                f"Échec copie historique locale ({resolved_history}) : "
                f"{exc.__class__.__name__}"
            )

    result["copied_history_files"] = copied_history_files
    if copied_any:
        result["status"] = "success"
        result["message"] = "Synchronisation locale Windows terminée."
    elif result["warnings"]:
        result["status"] = "failed"
        result["message"] = "Synchronisation locale Windows échouée."
    else:
        result["status"] = "idle"
        result["message"] = "Aucune source configurée pour la synchronisation locale."
    return result


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
            mount_root_abs = os.path.abspath(mount_root)
            resolved_candidate = os.path.abspath(
                os.path.normpath(os.path.join(mount_root_abs, *segments))
            )
            if os.path.commonpath([mount_root_abs, resolved_candidate]) != mount_root_abs:
                resolved_candidate = mount_root_abs
            return resolved_candidate, {
                "path_strategy": "mounted_fallback",
                "uri_host": host,
                "mount_root": mount_root_abs,
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
            [
                filename
                for filename in os.listdir(lookup_dir)
                if is_reglement_text_filename(filename)
            ],
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


def parse_article_lines(text: str) -> dict[str, str]:
    articles: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 2 or not parts[0]:
            continue
        articles[parts[0]] = parts[1] or parts[0]
    return articles


def parse_facture_lines(text: str, article_lookup: dict[str, str] | None = None) -> list[dict]:
    lookup = article_lookup or {}
    results: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 15:
            continue

        facture_number = parts[0]
        facture_date = parse_reglement_date(parts[2])
        cam_value = parts[5] or ""
        cam_match = re.search(r"(CAM\d+)", cam_value) or re.search(r"(CAM\d+)", parts[1])
        cam = cam_match.group(1) if cam_match else None
        site = get_site(cam) if cam else normalize_site(parts[4])
        article_code = parts[7]

        try:
            package_quantity = float((parts[9] or "0").replace(",", "."))
            quantity = float((parts[10] or "0").replace(",", "."))
            amount = float((parts[-2] or "0").replace(",", "."))
        except ValueError:
            continue

        results.append(
            {
                "facture_number": facture_number,
                "reference": parts[1],
                "facture_date": facture_date,
                "facture_date_iso": facture_date.isoformat() if facture_date else None,
                "document_type": parts[3],
                "raw_site": parts[4],
                "site": site,
                "cam": cam,
                "client_code": parts[6],
                "article_code": article_code,
                "article_name": lookup.get(article_code, article_code),
                "unit": parts[8],
                "package_quantity": package_quantity,
                "quantity": quantity,
                "amount": amount,
                "raw": line,
            }
        )
    return results


def load_article_lookup(path: str) -> tuple[dict[str, str], list[str]]:
    if not path:
        return {}, []
    text, error = read_text_file(path)
    if error:
        return {}, [error]
    return parse_article_lines(text or ""), []


def parse_decimal(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def build_etatmarge_issue_message(filename: str, message: str) -> str:
    return f"{filename} : {message}"


def parse_etatmarge_rows_with_diagnostics(
    rows: list[list[object]],
) -> tuple[dict[str, float], list[str], list[str]]:
    filename = "etatmarge"
    has_data = any(
        any(str(cell).strip() for cell in row if cell is not None)
        for row in rows
    )
    if not has_data:
        return {}, [build_etatmarge_issue_message(filename, "fichier vide.")], []

    normalized_rows = [
        [normalize_token(str(cell)) if cell is not None else "" for cell in row]
        for row in rows
    ]
    header_index = -1
    article_index = -1
    tva_index = -1
    first_article_row = -1
    first_tva_row = -1

    for idx, normalized_cells in enumerate(normalized_rows):
        has_article = "article" in normalized_cells
        has_tva = "tva" in normalized_cells
        if has_article and first_article_row < 0:
            first_article_row = idx
        if has_tva and first_tva_row < 0:
            first_tva_row = idx
        if has_article and has_tva:
            header_index = idx
            article_index = normalized_cells.index("article")
            tva_index = normalized_cells.index("tva")
            break

    if header_index < 0:
        if first_article_row < 0 and first_tva_row < 0:
            return {}, [build_etatmarge_issue_message(filename, "ligne d'en-tête introuvable (colonnes Article / Tva non détectées).")], []
        if first_article_row >= 0 and first_tva_row < 0:
            return {}, [build_etatmarge_issue_message(filename, "colonne Tva manquante.")], []
        if first_tva_row >= 0 and first_article_row < 0:
            return {}, [build_etatmarge_issue_message(filename, "colonne Article manquante.")], []
        return {}, [build_etatmarge_issue_message(filename, "ligne d'en-tête invalide (Article et Tva détectés sur des lignes différentes).")], []

    lookup: dict[str, float] = {}
    invalid_tva_articles: list[str] = []
    for row in rows[header_index + 1 :]:
        if max(article_index, tva_index) >= len(row):
            continue
        article_code = normalize_article_code(str(row[article_index]) if row[article_index] is not None else "")
        if not article_code:
            continue
        raw_tva = row[tva_index]
        tva_rate = parse_decimal(raw_tva)
        if tva_rate is None:
            if str(raw_tva or "").strip():
                invalid_tva_articles.append(article_code)
            continue
        lookup[article_code] = tva_rate

    warnings: list[str] = []
    if invalid_tva_articles:
        sample = ", ".join(invalid_tva_articles[:5])
        suffix = "…" if len(invalid_tva_articles) > 5 else ""
        warnings.append(
            build_etatmarge_issue_message(
                filename,
                f"valeur TVA invalide ignorée pour article(s) {sample}{suffix}.",
            )
        )

    if not lookup:
        return {}, [build_etatmarge_issue_message(filename, "aucune ligne de données exploitable après l'en-tête.")], warnings
    return lookup, [], warnings


def parse_etatmarge_rows(rows: list[list[object]]) -> dict[str, float]:
    lookup, _, _ = parse_etatmarge_rows_with_diagnostics(rows)
    return lookup


def parse_etatmarge_text(text: str) -> dict[str, float]:
    rows: list[list[object]] = []
    for line in (text or "").splitlines():
        raw_line = line.strip()
        if not raw_line:
            continue
        if "\t" in raw_line:
            parts = [part.strip() for part in raw_line.split("\t")]
        elif ";" in raw_line:
            parts = [part.strip() for part in raw_line.split(";")]
        elif "|" in raw_line:
            parts = [part.strip() for part in raw_line.split("|")]
        else:
            continue
        rows.append(parts)
    return parse_etatmarge_rows(rows)


def load_etatmarge_lookup(path: str) -> tuple[dict[str, float], list[str]]:
    if not path:
        return {}, []

    filename = os.path.basename(path) or "etatmarge"
    _, ext = os.path.splitext(path)
    ext = ext.casefold()
    try:
        if ext in {".xlsx", ".xlsm"}:
            workbook = load_workbook(path, read_only=True, data_only=True)
            sheet = workbook.active
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            workbook.close()
            lookup, errors, warnings = parse_etatmarge_rows_with_diagnostics(rows)
            if errors:
                return {}, [build_etatmarge_issue_message(filename, err.split(" : ", 1)[-1]) for err in errors]
            return lookup, [build_etatmarge_issue_message(filename, warn.split(" : ", 1)[-1]) for warn in warnings]
        else:
            text, error = read_text_file(path)
            if error:
                return {}, [error]
            rows: list[list[object]] = []
            for line in (text or "").splitlines():
                raw_line = line.strip()
                if not raw_line:
                    continue
                if "\t" in raw_line:
                    parts = [part.strip() for part in raw_line.split("\t")]
                elif ";" in raw_line:
                    parts = [part.strip() for part in raw_line.split(";")]
                elif "|" in raw_line:
                    parts = [part.strip() for part in raw_line.split("|")]
                else:
                    continue
                rows.append(parts)
            lookup, errors, warnings = parse_etatmarge_rows_with_diagnostics(rows)
            if errors:
                return {}, [build_etatmarge_issue_message(filename, err.split(" : ", 1)[-1]) for err in errors]
            return lookup, [build_etatmarge_issue_message(filename, warn.split(" : ", 1)[-1]) for warn in warnings]
    except Exception as exc:
        return {}, [build_etatmarge_issue_message(filename, f"fichier Excel invalide ou illisible ({exc.__class__.__name__}).")]


def list_plain_files(directory: str) -> tuple[list[str], list[str]]:
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
            [
                filename
                for filename in os.listdir(lookup_dir)
                if os.path.isfile(os.path.join(lookup_dir, filename)) and not filename.startswith(".")
            ],
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


def load_facture_lines_from_paths(
    paths: list[str], article_lookup: dict[str, str]
) -> tuple[list[dict], list[str], list[str]]:
    rows: list[dict] = []
    source_files: list[str] = []
    warnings: list[str] = []

    for path in unique_paths(paths):
        text, error = read_text_file(path)
        if error:
            warnings.append(error)
            continue
        source_files.append(path)
        rows.extend(parse_facture_lines(text or "", article_lookup))

    return rows, source_files, warnings


def get_facture_bounds(rows: list[dict]) -> tuple[date | None, date | None]:
    dates = [r.get("facture_date") for r in rows if isinstance(r.get("facture_date"), date)]
    if not dates:
        return None, None
    return min(dates), max(dates)


def build_big_factures(rows: list[dict], etatmarge_lookup: dict[str, float] | None = None) -> list[dict]:
    tva_lookup = etatmarge_lookup or {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        facture_number = row.get("facture_number")
        if facture_number:
            grouped[facture_number].append(row)

    big_factures: list[dict] = []
    for facture_number, facture_rows in grouped.items():
        first = facture_rows[0]
        base_total = sum(line.get("amount", 0.0) for line in facture_rows)
        tva_total = 0.0
        for line in facture_rows:
            article_code = normalize_article_code(line.get("article_code"))
            tva_rate = tva_lookup.get(article_code, 0.0)
            if not tva_rate:
                continue
            tva_total += line.get("amount", 0.0) * tva_rate / 100.0
        timbre = 1.0
        total_amount = round(base_total + tva_total + timbre, 3)
        total_quantity = round(sum(line.get("quantity", 0.0) for line in facture_rows), 3)
        big_factures.append(
            {
                "facture_number": facture_number,
                "facture_date": first.get("facture_date"),
                "facture_date_iso": first.get("facture_date_iso"),
                "site": first.get("site") or get_site(first.get("cam")),
                "cam": first.get("cam"),
                "client_code": first.get("client_code"),
                "reference": first.get("reference"),
                "line_count": len(facture_rows),
                "total_amount": total_amount,
                "base_total": round(base_total, 3),
                "tva_total": round(tva_total, 3),
                "timbre": timbre,
                "total_quantity": total_quantity,
                "articles_count": len({line.get("article_code") for line in facture_rows if line.get("article_code")}),
                "lines": facture_rows,
            }
        )

    return sorted(
        big_factures,
        key=lambda item: (
            item.get("facture_date") or date.min,
            item.get("facture_number") or "",
        ),
        reverse=True,
    )


def filter_big_factures_by_date(
    rows: list[dict], start_date: date | None, end_date: date | None
) -> list[dict]:
    filtered: list[dict] = []
    for row in rows:
        facture_date = row.get("facture_date")
        if facture_date is None:
            continue
        if start_date is not None and facture_date < start_date:
            continue
        if end_date is not None and facture_date > end_date:
            continue
        filtered.append(row)
    return filtered


def build_cam_facture_payload(
    cam: str,
    factures: list[dict],
    *,
    mode: str,
    source_files: list[str] | None = None,
    date_range: dict[str, str] | None = None,
    warnings: list[str] | None = None,
) -> dict:
    cam_factures = [facture for facture in factures if facture.get("cam") == cam]
    cam_factures.sort(
        key=lambda item: (item.get("facture_date") or date.min, item.get("facture_number") or ""),
        reverse=True,
    )

    article_acc: dict[str, dict] = defaultdict(
        lambda: {"article_name": "", "quantity": 0.0, "amount": 0.0, "line_count": 0}
    )
    for facture in cam_factures:
        for line in facture.get("lines", []):
            article_code = line.get("article_code") or "—"
            article = article_acc[article_code]
            article["article_name"] = line.get("article_name") or article_code
            article["quantity"] += line.get("quantity", 0.0)
            article["amount"] += line.get("amount", 0.0)
            article["line_count"] += 1

    top_articles = sorted(
        [
            {
                "article_code": code,
                "article_name": values["article_name"],
                "quantity": round(values["quantity"], 3),
                "amount": round(values["amount"], 3),
                "line_count": values["line_count"],
            }
            for code, values in article_acc.items()
        ],
        key=lambda item: (item["amount"], item["quantity"]),
        reverse=True,
    )

    factures_payload = [
        {
            "facture_number": facture["facture_number"],
            "facture_date_iso": facture["facture_date_iso"],
            "client_code": facture["client_code"],
            "reference": facture["reference"],
            "line_count": facture["line_count"],
            "articles_count": facture["articles_count"],
            "total_quantity": facture["total_quantity"],
            "total_amount": facture["total_amount"],
            "top_articles_preview": [
                line.get("article_name") or line.get("article_code")
                for line in sorted(
                    facture.get("lines", []),
                    key=lambda item: item.get("amount", 0.0),
                    reverse=True,
                )[:3]
            ],
        }
        for facture in cam_factures
    ]

    return {
        "cam": cam,
        "site": get_site(cam),
        "mode": mode,
        "date_range": date_range,
        "source_files": source_files or [],
        "warnings": warnings or [],
        "nb_factures": len(cam_factures),
        "total_vente": round(sum(facture["total_amount"] for facture in cam_factures), 3),
        "top_articles": top_articles[:8],
        "factures": factures_payload,
    }



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
    """Read règlement files from uploaded-folder context (primary) or optional fallback paths."""
    import_context = _cache.get("import_context") or {}
    using_uploaded_folder = bool(import_context.get("active"))

    current_path = (import_context.get("current_path") if using_uploaded_folder else DEFAULT_CURRENT_REGLEMENT_FILE) or ""
    history_dir = (import_context.get("history_path") if using_uploaded_folder else DEFAULT_HISTORY_REGLEMENTS_DIR) or ""
    article_path = (import_context.get("article_path") if using_uploaded_folder else "") or ""
    etatmarge_path = (import_context.get("etatmarge_path") if using_uploaded_folder else "") or ""
    facture_path = (import_context.get("facture_path") if using_uploaded_folder else "") or ""
    factures_dir = (import_context.get("factures_path") if using_uploaded_folder else "") or ""
    warnings: list[str] = []
    if using_uploaded_folder:
        warnings.extend(import_context.get("warnings", []))

    current_diagnostic = inspect_source_path(current_path, expect_directory=False)
    history_diagnostic = inspect_source_path(history_dir, expect_directory=True)
    sync_info = {
        "required": False,
        "mode": "uploaded_folder" if using_uploaded_folder else "configured_paths",
        "status": "ready",
        "message": (
            "Chargement depuis le dossier importé."
            if using_uploaded_folder
            else "Chargement depuis les chemins configurés."
        ),
        "cache_root": import_context.get("root_path") if using_uploaded_folder else None,
        "local_current_path": current_path if using_uploaded_folder else None,
        "local_history_path": history_dir if using_uploaded_folder else None,
        "copied_history_files": 0,
    }

    if not current_path and not history_dir:
        if using_uploaded_folder:
            warnings.append(
                "Le dossier importé doit contenir REGLEMENT.txt et le sous-dossier Réglements."
            )
        else:
            warnings.append(
                "Aucune donnée importée. Importez le dossier racine Fichiers Sources."
            )
        _cache.update({
            "all_rows": [],
            "current_rows": [],
            "source_files": [],
            "current_source_files": [],
            "article_lookup": {},
            "etatmarge_lookup": {},
            "etatmarge_warnings": [],
            "all_facture_lines": [],
            "all_big_factures": [],
            "current_big_factures": [],
            "facture_source_files": [],
            "current_facture_source_files": [],
            "warnings": warnings,
            "current_warnings": [],
            "loaded_at": time.time(),
            "coverage_start": None,
            "coverage_end": None,
            "history_file_count": 0,
            "facture_coverage_start": None,
            "facture_coverage_end": None,
            "facture_history_file_count": 0,
            "source_diagnostics": {
                "current": current_diagnostic,
                "history": history_diagnostic,
            },
            "sync": sync_info,
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
    article_lookup, article_warnings = load_article_lookup(article_path)
    warnings.extend(article_warnings)
    etatmarge_lookup, etatmarge_warnings = load_etatmarge_lookup(etatmarge_path)
    warnings.extend(etatmarge_warnings)

    facture_history_files: list[str] = []
    if factures_dir:
        facture_history_files, facture_dir_warnings = list_plain_files(factures_dir)
        warnings.extend(facture_dir_warnings)

    facture_current_rows: list[dict] = []
    facture_current_sources: list[str] = []
    facture_current_warnings: list[str] = []
    if facture_path:
        (
            facture_current_rows,
            facture_current_sources,
            facture_current_warnings,
        ) = load_facture_lines_from_paths([facture_path], article_lookup)
        warnings.extend(facture_current_warnings)

    facture_history_rows: list[dict] = []
    facture_history_sources: list[str] = []
    if facture_history_files:
        (
            facture_history_rows,
            facture_history_sources,
            facture_history_warnings,
        ) = load_facture_lines_from_paths(facture_history_files, article_lookup)
        warnings.extend(facture_history_warnings)

    all_facture_lines = facture_history_rows + facture_current_rows
    current_big_factures = build_big_factures(facture_current_rows, etatmarge_lookup)
    all_big_factures = build_big_factures(all_facture_lines, etatmarge_lookup)
    facture_coverage_start, facture_coverage_end = get_facture_bounds(all_facture_lines)

    _cache.update({
        "all_rows": all_rows,
        "current_rows": current_rows,
        "source_files": all_sources,
        "current_source_files": current_sources,
        "article_lookup": article_lookup,
        "etatmarge_lookup": etatmarge_lookup,
        "etatmarge_warnings": etatmarge_warnings,
        "all_facture_lines": all_facture_lines,
        "all_big_factures": all_big_factures,
        "current_big_factures": current_big_factures,
        "facture_source_files": facture_history_sources + facture_current_sources,
        "current_facture_source_files": facture_current_sources,
        "warnings": warnings,
        "current_warnings": cur_warnings if current_path else [],
        "loaded_at": time.time(),
        "coverage_start": date_to_iso(coverage_start),
        "coverage_end": date_to_iso(coverage_end),
        "history_file_count": len(history_files),
        "facture_coverage_start": date_to_iso(facture_coverage_start),
        "facture_coverage_end": date_to_iso(facture_coverage_end),
        "facture_history_file_count": len(facture_history_files),
        "source_diagnostics": {
            "current": current_diagnostic,
            "history": history_diagnostic,
        },
        "sync": sync_info,
    })


def get_or_reload_cache() -> dict:
    """Return the cache, triggering a load from source paths if not yet loaded."""
    if _cache["loaded_at"] is None:
        import_context = _cache.get("import_context") or {}
        if import_context.get("status") == "processing":
            return _cache
        reload_cache()
    return _cache


def get_source_status() -> dict:
    """Return a status dict describing the current cache state for the UI."""
    cache = _cache
    loaded_at = cache["loaded_at"]
    import_context = cache.get("import_context", {})
    import_status = import_context.get("status") or ("ready" if loaded_at is not None else "idle")
    using_uploaded_folder = bool(import_context.get("active"))
    current_source = import_context.get("current_path") if using_uploaded_folder else DEFAULT_CURRENT_REGLEMENT_FILE
    history_source = import_context.get("history_path") if using_uploaded_folder else DEFAULT_HISTORY_REGLEMENTS_DIR
    article_source = import_context.get("article_path") if using_uploaded_folder else ""
    etatmarge_source = import_context.get("etatmarge_path") if using_uploaded_folder else ""
    facture_source = import_context.get("facture_path") if using_uploaded_folder else ""
    factures_source = import_context.get("factures_path") if using_uploaded_folder else ""
    return {
        "loaded": loaded_at is not None,
        "loaded_at": loaded_at,
        "coverage_start": cache["coverage_start"],
        "coverage_end": cache["coverage_end"],
        "source_file_count": len(cache["source_files"]),
        "history_file_count": cache["history_file_count"],
        "facture_source_file_count": len(cache.get("facture_source_files", [])),
        "facture_history_file_count": cache.get("facture_history_file_count", 0),
        "has_data": bool(cache["all_rows"]),
        "has_facture_data": bool(cache.get("all_big_factures")),
        "warnings": cache["warnings"],
        "current_source_label": (
            get_source_label(current_source)
            if current_source
            else "—"
        ),
        "history_source_label": (
            get_source_label(history_source)
            if history_source
            else "—"
        ),
        "article_source_label": (
            get_source_label(article_source)
            if article_source
            else "—"
        ),
        "etatmarge_source_label": (
            get_source_label(etatmarge_source)
            if etatmarge_source
            else "—"
        ),
        "facture_source_label": (
            get_source_label(facture_source)
            if facture_source
            else "—"
        ),
        "factures_source_label": (
            get_source_label(factures_source)
            if factures_source
            else "—"
        ),
        "source_mode": "uploaded_folder" if using_uploaded_folder else "configured_paths",
        "import_status": import_status,
        "import_error_message": import_context.get("error_message"),
        "import_context": {
            "active": bool(import_context.get("active")),
            "status": import_status,
            "uploaded_at": import_context.get("uploaded_at"),
            "error_message": import_context.get("error_message"),
            "root_name": import_context.get("root_name"),
            "root_path": import_context.get("root_path"),
        },
        "uploaded_root_name": import_context.get("root_name"),
        "uploaded_root_path": import_context.get("root_path"),
        "expected_current_name": "REGLEMENT.txt",
        "expected_history_name": "Réglements",
        "expected_article_name": "ARTICLE",
        "expected_etatmarge_name": "etatmarge",
        "expected_facture_name": "FACTURE",
        "expected_factures_name": "Factures",
        "current_found": import_context.get("current_found", bool(current_source)),
        "history_found": import_context.get("history_found", bool(history_source)),
        "article_found": import_context.get("article_found", bool(article_source)),
        "etatmarge_found": import_context.get("etatmarge_found", bool(etatmarge_source)),
        "facture_found": import_context.get("facture_found", bool(facture_source)),
        "factures_found": import_context.get("factures_found", bool(factures_source)),
        "facture_coverage_start": cache.get("facture_coverage_start"),
        "facture_coverage_end": cache.get("facture_coverage_end"),
        "etatmarge_warnings": cache.get("etatmarge_warnings", []),
        "etatmarge_lookup_size": len(cache.get("etatmarge_lookup", {})),
        "runtime_label": f"{os.name}/{sys.platform}",
        "current_diagnostic": cache.get("source_diagnostics", {}).get("current", {}),
        "history_diagnostic": cache.get("source_diagnostics", {}).get("history", {}),
        "sync_required": cache.get("sync", {}).get("required", False),
        "sync_mode": cache.get("sync", {}).get("mode", "direct_read"),
        "sync_status": cache.get("sync", {}).get("status", "not_required"),
        "sync_message": cache.get("sync", {}).get("message"),
        "local_cache_root": cache.get("sync", {}).get("cache_root"),
        "local_current_path": cache.get("sync", {}).get("local_current_path"),
        "local_history_path": cache.get("sync", {}).get("local_history_path"),
        "copied_history_files": cache.get("sync", {}).get("copied_history_files", 0),
    }


def build_import_results(status: dict) -> list[dict]:
    etatmarge_warnings = status.get("etatmarge_warnings", [])
    etatmarge_warning_msg = " ".join(etatmarge_warnings) if etatmarge_warnings else ""
    etatmarge_lookup_size = int(status.get("etatmarge_lookup_size") or 0)
    return [
        {
            "key": "current_reglement",
            "label": "REGLEMENT.txt",
            "required": True,
            "status": "ok" if status.get("current_found") else "missing",
            "kind": "ok" if status.get("current_found") else "err",
            "file": status.get("current_source_label") if status.get("current_found") else None,
            "message": (
                "Fichier chargé."
                if status.get("current_found")
                else "Fichier requis introuvable."
            ),
        },
        {
            "key": "history_reglements",
            "label": "Réglements (historique)",
            "required": True,
            "status": "ok" if status.get("history_found") else "missing",
            "kind": "ok" if status.get("history_found") else "err",
            "file": status.get("history_source_label") if status.get("history_found") else None,
            "message": (
                f"{status.get('history_file_count', 0)} fichier(s) chargé(s)."
                if status.get("history_found")
                else "Dossier requis introuvable."
            ),
        },
        {
            "key": "article",
            "label": "ARTICLE",
            "required": True,
            "status": "ok" if status.get("article_found") else "missing",
            "kind": "ok" if status.get("article_found") else "err",
            "file": status.get("article_source_label") if status.get("article_found") else None,
            "message": (
                "Fichier chargé."
                if status.get("article_found")
                else "Fichier requis introuvable."
            ),
        },
        {
            "key": "etatmarge",
            "label": "etatmarge",
            "required": False,
            "status": (
                "optional"
                if not status.get("etatmarge_found")
                else ("error" if etatmarge_warnings and etatmarge_lookup_size == 0 else ("warning" if etatmarge_warnings else "ok"))
            ),
            "kind": (
                "warn"
                if not status.get("etatmarge_found")
                else ("err" if etatmarge_warnings and etatmarge_lookup_size == 0 else ("warn" if etatmarge_warnings else "ok"))
            ),
            "file": status.get("etatmarge_source_label") if status.get("etatmarge_found") else None,
            "message": (
                "Fichier optionnel non fourni."
                if not status.get("etatmarge_found")
                else (etatmarge_warning_msg or "Fichier chargé.")
            ),
        },
        {
            "key": "facture",
            "label": "FACTURE",
            "required": True,
            "status": "ok" if status.get("facture_found") else "missing",
            "kind": "ok" if status.get("facture_found") else "err",
            "file": status.get("facture_source_label") if status.get("facture_found") else None,
            "message": (
                "Fichier chargé."
                if status.get("facture_found")
                else "Fichier requis introuvable."
            ),
        },
        {
            "key": "history_factures",
            "label": "Factures (historique)",
            "required": True,
            "status": "ok" if status.get("factures_found") else "missing",
            "kind": "ok" if status.get("factures_found") else "err",
            "file": status.get("factures_source_label") if status.get("factures_found") else None,
            "message": (
                f"{status.get('facture_history_file_count', 0)} fichier(s) chargé(s)."
                if status.get("factures_found")
                else "Dossier requis introuvable."
            ),
        },
    ]


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
    cache = get_or_reload_cache()
    import_context = cache.get("import_context", {})
    using_uploaded_folder = bool(import_context.get("active"))
    current_source = import_context.get("current_path") if using_uploaded_folder else DEFAULT_CURRENT_REGLEMENT_FILE
    label = f"Mois en cours · {get_source_label(current_source)}"
    default_warnings = cache["current_warnings"] if cache["current_source_files"] else cache["warnings"]

    return JSONResponse(
        build_upload_payload(
            cache["current_rows"],
            label,
            mode="default",
            source_files=cache["current_source_files"],
            warnings=default_warnings,
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


@app.get("/api/cam/{cam_code}")
async def get_cam_facture_detail(
    cam_code: str,
    start_date: str | None = Query(None, description="Date de début au format AAAA-MM-JJ"),
    end_date: str | None = Query(None, description="Date de fin au format AAAA-MM-JJ"),
):
    cam = cam_code.strip().upper()
    cache = get_or_reload_cache()
    warnings = list(cache["warnings"])

    if (start_date and not end_date) or (end_date and not start_date):
        return JSONResponse(
            {"detail": "Renseignez les deux dates pour filtrer les factures CAM."},
            status_code=400,
        )

    if start_date and end_date:
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
        factures = filter_big_factures_by_date(cache.get("all_big_factures", []), start, end)
        mode = "date_range"
        source_files = cache.get("facture_source_files", [])
        date_range = {"start": start.isoformat(), "end": end.isoformat()}
    else:
        factures = cache.get("current_big_factures", [])
        mode = "default"
        source_files = cache.get("current_facture_source_files", [])
        date_range = None

    if not cache.get("all_big_factures"):
        warnings.append(
            "Aucune donnée facture n'a été détectée. Importez ARTICLE, FACTURE et le dossier Factures pour activer l'analyse CAM."
        )

    return JSONResponse(
        build_cam_facture_payload(
            cam,
            factures,
            mode=mode,
            source_files=source_files,
            date_range=date_range,
            warnings=warnings,
        )
    )


@app.get("/api/status")
async def source_status():
    """Return metadata about the loaded data cache for the source status panel."""
    return JSONResponse(get_source_status())


async def process_import_async(import_root: str) -> None:
    """Reload cached data in a background worker after upload."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(IMPORT_EXECUTOR, reload_cache)
        import_context = _cache.get("import_context") or {}
        if import_context.get("active") and import_context.get("root_path") == import_root:
            import_context["status"] = "ready"
            import_context.pop("error_message", None)
    except Exception as exc:
        logger.exception("Import background processing failed")
        import_context = _cache.get("import_context") or {}
        if import_context.get("active") and import_context.get("root_path") == import_root:
            import_context["status"] = "error"
            import_context["error_message"] = str(exc)


@app.post("/api/refresh")
async def refresh_data():
    """Force a reload of all règlement data from the configured source paths."""
    reload_cache()
    return JSONResponse(get_source_status())


@app.post("/api/import-folder")
async def import_folder(files: list[UploadFile] = File(...)):
    if not files:
        return JSONResponse(
            {"detail": "Aucun fichier reçu. Sélectionnez le dossier Fichiers Sources."},
            status_code=400,
        )

    # Pre-validate total upload size before processing any files
    total_size = sum(upload.size or 0 for upload in files)

    if total_size > MAX_TOTAL_UPLOAD_SIZE:
        return JSONResponse(
            {
                "detail": (
                    f"La taille totale des fichiers ({format_size(total_size)}) "
                    f"dépasse la limite autorisée ({format_size(MAX_TOTAL_UPLOAD_SIZE)}). "
                    f"Réduisez le nombre de fichiers historiques et réessayez."
                ),
                "error": {
                    "code": "UPLOAD_SIZE_EXCEEDED",
                    "total_size": total_size,
                    "max_size": MAX_TOTAL_UPLOAD_SIZE,
                    "file_count": len(files),
                },
            },
            status_code=413,
        )

    import_root = tempfile.mkdtemp(prefix="sind_reglement_import_")
    uploaded_root_name: str | None = None
    saved_files = 0
    warnings: list[str] = []
    current_upload_name: str | None = None
    upload_results: list[dict] = []

    try:
        for upload in files:
            current_upload_name = upload.filename or "fichier_inconnu"
            raw_rel_path = (upload.filename or "").replace("\\", "/").strip("/")
            if not raw_rel_path:
                upload_results.append(
                    {
                        "file": current_upload_name,
                        "kind": "err",
                        "message": "Nom de fichier vide ou invalide.",
                    }
                )
                await upload.close()
                continue
            segments = [segment for segment in raw_rel_path.split("/") if segment not in {"", ".", ".."}]
            if not segments:
                upload_results.append(
                    {
                        "file": raw_rel_path,
                        "kind": "err",
                        "message": "Chemin de fichier invalide.",
                    }
                )
                await upload.close()
                continue
            if len(segments) > 1:
                uploaded_root_name = uploaded_root_name or segments[0]
                relative_segments = segments[1:]
            else:
                relative_segments = segments
            if not relative_segments:
                await upload.close()
                continue

            filename = relative_segments[-1]
            parent_segments = relative_segments[:-1]
            is_root_level = len(parent_segments) == 0
            in_reglements_dir = any(is_reglements_dir_name(segment) for segment in parent_segments)
            in_factures_dir = any(is_factures_dir_name(segment) for segment in parent_segments)
            is_monthly_file = filename.casefold() == "reglement.txt"
            is_history_file = in_reglements_dir and is_reglement_text_filename(filename)
            is_article_file = is_root_level and is_named_source_file(filename, "ARTICLE")
            is_etatmarge_file = is_root_level and is_named_excel_source_file(filename, "etatmarge")
            is_current_facture_file = is_root_level and is_named_source_file(filename, "FACTURE")
            is_history_facture_file = in_factures_dir
            if not (
                is_monthly_file
                or is_history_file
                or is_article_file
                or is_etatmarge_file
                or is_current_facture_file
                or is_history_facture_file
            ):
                upload_results.append(
                    {
                        "file": raw_rel_path,
                        "kind": "warn",
                        "message": "Fichier ignoré (hors périmètre de l'import).",
                    }
                )
                await upload.close()
                continue

            destination_path = os.path.join(import_root, *relative_segments)
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            file_size = 0
            async with aiofiles.open(destination_path, "wb") as out:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    file_size += len(chunk)
                    if file_size > MAX_SINGLE_FILE_SIZE:
                        raise ValueError(
                            f"Le fichier '{raw_rel_path}' ({format_size(file_size)}) "
                            f"dépasse la limite par fichier ({format_size(MAX_SINGLE_FILE_SIZE)})."
                        )
                    await out.write(chunk)
            saved_files += 1
            upload_results.append(
                {
                    "file": raw_rel_path,
                    "kind": "ok",
                    "message": "Fichier reçu.",
                }
            )
            await upload.close()

        if saved_files == 0:
            shutil.rmtree(import_root, ignore_errors=True)
            return JSONResponse(
                {
                    "detail": "Le dossier importé est vide ou invalide.",
                    "error": {
                        "code": "EMPTY_IMPORT",
                        "message": "Aucun fichier exploitable n'a été reçu.",
                    },
                    "upload_file_results": upload_results,
                },
                status_code=400,
            )

        previous_context = _cache.get("import_context") or {}
        previous_root = previous_context.get("root_path")
        if previous_root and previous_root != import_root:
            shutil.rmtree(previous_root, ignore_errors=True)

        discovered = discover_uploaded_sources(import_root)
        expected_root_name = "Fichiers Sources"
        if uploaded_root_name:
            discovered["root_name"] = uploaded_root_name
            if normalize_token(uploaded_root_name) != normalize_token(expected_root_name):
                warnings.append(
                    f"Dossier racine détecté: {uploaded_root_name}. "
                    f"Le dossier attendu est {expected_root_name}."
                )
        else:
            discovered["root_name"] = expected_root_name

        discovered["warnings"] = warnings + discovered.get("warnings", [])
        _cache.update({
            "all_rows": [],
            "current_rows": [],
            "source_files": [],
            "current_source_files": [],
            "article_lookup": {},
            "etatmarge_lookup": {},
            "etatmarge_warnings": [],
            "all_facture_lines": [],
            "all_big_factures": [],
            "current_big_factures": [],
            "facture_source_files": [],
            "current_facture_source_files": [],
            "warnings": [],
            "current_warnings": [],
            "loaded_at": None,
            "coverage_start": None,
            "coverage_end": None,
            "history_file_count": 0,
            "facture_coverage_start": None,
            "facture_coverage_end": None,
            "facture_history_file_count": 0,
            "source_diagnostics": {},
            "sync": {},
        })
        _cache["import_context"] = {
            **discovered,
            "active": True,
            "uploaded_at": time.time(),
        }
        payload = get_source_status()
        payload["import_results"] = build_import_results(payload)
        payload["upload_file_results"] = upload_results
        payload["uploaded_files"] = saved_files
        return JSONResponse(payload)
    except Exception as exc:
        shutil.rmtree(import_root, ignore_errors=True)
        return JSONResponse(
            {
                "detail": "Import interrompu. Vérifiez le format des fichiers et réessayez.",
                "error": {
                    "code": "IMPORT_FAILED",
                    "file": current_upload_name,
                    "message": f"{exc.__class__.__name__}: {exc}",
                },
                "upload_file_results": upload_results,
            },
            status_code=500,
        )
