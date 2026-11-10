#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sonar_fixer.py - Corrige des issues SonarQube une règle à la fois,
avec le LLM interne (API compatible OpenAI : Mistral / Devstral).

Pour chaque issue :
  le LLM propose un remplacement de lignes -> contrôles -> écriture
  -> compilation -> gardé si OK, fichier restauré sinon.

Aucune opération git : les fichiers sont modifiés directement, avec une
sauvegarde des originaux et une commande "undo" pour tout annuler.

Aucune dépendance : Python 3.8+ standard uniquement.

Commandes :
  python sonar_fixer.py check
  python sonar_fixer.py stats [--top 30]
  python sonar_fixer.py fix --rule java:S1155 [--max 20] [--dry-run] [--retry]
  python sonar_fixer.py undo [--run NOM_DU_DOSSIER]
"""
from __future__ import annotations

import argparse
import base64
import codecs
import csv
import datetime
import difflib
import hashlib
import html
import json
import locale
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPORTS_DIR = TOOL_DIR / "reports"
STATE_DIR = TOOL_DIR / "state"
BOM = b"\xef\xbb\xbf"
LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")
SPINNER = "|/-\\"


class FixerError(Exception):
    """Erreur bloquante avec un message clair pour l'utilisateur."""


class HttpError(FixerError):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class EncodingProblem(Exception):
    pass


# ---------------------------------------------------------------------------
# Affichage : lignes permanentes + une ligne d'état qui se met à jour
# ---------------------------------------------------------------------------

for _stream in (sys.stdout, sys.stderr):
    try:  # un caractère non affichable devient '?' au lieu de planter
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


def _console_supports(chars: str) -> bool:
    try:
        chars.encode(getattr(sys.stdout, "encoding", None) or "ascii")
        return True
    except Exception:
        return False


BAR_FULL, BAR_EMPTY = ("█", "░") if _console_supports("█░") else ("#", "-")


def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


class Console:
    def __init__(self):
        self.lock = threading.RLock()
        self.tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self.status_fn = None
        self.heartbeat_fn = None
        self._drawn = 0
        self._stop = threading.Event()
        self._thread = None

    def _width(self) -> int:
        return max(40, shutil.get_terminal_size((100, 20)).columns - 1)

    def _clear(self) -> None:
        if self._drawn:
            sys.stdout.write("\r" + " " * self._drawn + "\r")
            self._drawn = 0

    def _draw(self) -> None:
        if self.tty and self.status_fn:
            text = self.status_fn()[: self._width()]
            sys.stdout.write("\r" + text + " " * max(0, self._drawn - len(text)))
            self._drawn = len(text)
            sys.stdout.flush()

    def print(self, msg: str = "") -> None:
        with self.lock:
            self._clear()
            sys.stdout.write(msg + "\n")
            self._draw()
            sys.stdout.flush()

    def start_status(self, status_fn, heartbeat_fn=None) -> None:
        self.stop_status()
        self.status_fn, self.heartbeat_fn = status_fn, heartbeat_fn
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        last_beat = time.time()
        while not self._stop.wait(0.5):
            with self.lock:
                if self.tty:
                    self._draw()
                elif self.heartbeat_fn and time.time() - last_beat >= 30:
                    last_beat = time.time()
                    msg = self.heartbeat_fn()
                    if msg:
                        sys.stdout.write(msg + "\n")
                        sys.stdout.flush()

    def stop_status(self) -> None:
        if self._thread:
            self._stop.set()
            self._thread.join(2)
            self._thread = None
        with self.lock:
            self._clear()
            self.status_fn = self.heartbeat_fn = None
            sys.stdout.flush()


CONSOLE = Console()


def log(msg: str = "") -> None:
    CONSOLE.print(msg)


class Spinner:
    """Ligne d'état simple : texte + chrono (pour check, stats...)."""

    def __init__(self, text: str):
        self.text, self.t0, self.i = text, time.time(), 0

    def set(self, text: str) -> None:
        self.text = text

    def status(self) -> str:
        self.i += 1
        return f"  {SPINNER[self.i % 4]} {self.text} ({fmt_dur(time.time() - self.t0)})"

    def heartbeat(self) -> str:
        return f"    ... {self.text} ({fmt_dur(time.time() - self.t0)})"

    def __enter__(self):
        CONSOLE.start_status(self.status, self.heartbeat)
        return self

    def __exit__(self, *exc):
        CONSOLE.stop_status()
        return False


class RunProgress:
    """Barre de progression du 'fix'."""

    def __init__(self, total: int):
        self.total, self.done = total, 0
        self.counts = {"OK": 0, "SKIP": 0, "REJECT": 0}
        self.label, self.step = "", "démarrage"
        self.t0 = self.step_t = self.issue_t = time.time()
        self.durations: list = []
        self.i = 0

    def begin(self, label: str) -> None:
        self.label, self.issue_t = label, time.time()

    def set_step(self, step: str) -> None:
        self.step, self.step_t = step, time.time()

    def end(self, status: str) -> None:
        self.done += 1
        self.counts[status] += 1
        self.durations.append(time.time() - self.issue_t)

    def eta(self) -> str:
        if not self.durations:
            return "estimation après la 1re issue"
        recent = self.durations[-10:]
        return "reste ~" + fmt_dur(sum(recent) / len(recent) * (self.total - self.done))

    def status(self) -> str:
        self.i += 1
        pct = self.done / self.total if self.total else 1
        filled = int(20 * pct)
        bar = BAR_FULL * filled + BAR_EMPTY * (20 - filled)
        c = self.counts
        return (f"[{bar}] {self.done}/{self.total} {int(pct * 100)}% | OK {c['OK']} SKIP {c['SKIP']} "
                f"REJECT {c['REJECT']} | {self.eta()} | {self.label} : {self.step} "
                f"{SPINNER[self.i % 4]} {fmt_dur(time.time() - self.step_t)}")

    def heartbeat(self) -> str:
        return (f"    ... en cours : {self.label} - {self.step} depuis {fmt_dur(time.time() - self.step_t)}"
                f" ({self.done}/{self.total} traitées, {self.eta()})")


def interruptible(fn, *args, **kwargs):
    """Exécute fn dans un thread pour que Ctrl+C reste réactif (Windows compris)."""
    box: dict = {}

    def target():
        try:
            box["r"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.2)
    if "e" in box:
        raise box["e"]
    return box.get("r")


# ---------------------------------------------------------------------------
# Texte : décodage robuste (accents)
# ---------------------------------------------------------------------------

def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def decode_output(data: bytes) -> str:
    """Sortie de compilation : UTF-8, sinon encodage de la console Windows."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "cp1252", "replace")


def non_ascii_changes(old_lines: list, new_lines: list) -> tuple:
    old = Counter(ch for ln in old_lines for ch in ln if ord(ch) > 127)
    new = Counter(ch for ln in new_lines for ch in ln if ord(ch) > 127)
    return old - new, new - old


def show_chars(counter: Counter) -> str:
    return " ".join(sorted(counter))[:40]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    """Fichier .env simple (CLE=valeur), UTF-8 ou ANSI (Bloc-notes). Les variables déjà définies gagnent."""
    if not path.exists():
        return
    raw = path.read_bytes()
    if raw.startswith(BOM):
        raw = raw[3:]
    for line in decode_bytes(raw).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        # retire les guillemets autour de la valeur entière seulement
        # (BUILD_CMD="C:\a b\mvn" -f "x" doit garder ses guillemets)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" and value[0] not in value[1:-1]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


class Config:
    REQUIRED = ["SONAR_URL", "SONAR_TOKEN", "SONAR_PROJECT_KEY",
                "MISTRAL_URL", "MISTRAL_MODEL", "REPO_PATH"]

    def __init__(self) -> None:
        env = os.environ
        missing = [k for k in self.REQUIRED if not env.get(k, "").strip()]
        if missing:
            raise FixerError("Variables manquantes dans .env : " + ", ".join(missing)
                             + "\n-> Copie .env.example en .env (même dossier que le script) et remplis-le.")
        self.sonar_url = env["SONAR_URL"].strip().rstrip("/")
        self.sonar_token = env["SONAR_TOKEN"].strip()
        self.project = env["SONAR_PROJECT_KEY"].strip()
        self.sonar_branch = env.get("SONAR_BRANCH", "").strip()
        self.mistral_url = env["MISTRAL_URL"].strip()
        self.mistral_key = env.get("MISTRAL_API_KEY", "").strip()
        self.model = env["MISTRAL_MODEL"].strip()
        self.repo = Path(env["REPO_PATH"].strip()).expanduser().resolve()
        self.source_root = env.get("SOURCE_ROOT", "").strip().strip("/\\")
        self.build_cmd = env.get("BUILD_CMD", "").strip()
        if self.build_cmd.lower() in ("none", "aucun", "aucune", "non", "-"):
            self.build_cmd = ""  # pas de compilation : l'utilisateur compile lui-même
        self.test_cmd = env.get("TEST_CMD", "").strip()
        self.ca_bundle = env.get("CA_BUNDLE", "").strip()
        self.encoding = env.get("SOURCE_ENCODING", "").strip()
        self.context_lines = self._int("CONTEXT_LINES", 30)
        self.max_changed = self._int("MAX_CHANGED_LINES", 40)
        self.build_timeout = self._int("BUILD_TIMEOUT", 1800)
        self.llm_timeout = self._int("LLM_TIMEOUT", 180)
        self.llm_max_tokens = self._int("LLM_MAX_TOKENS", 2048)
        self.verify_hash = env.get("VERIFY_LINE_HASH", "true").strip().lower() not in ("0", "false", "no", "non")
        for url, name in ((self.sonar_url, "SONAR_URL"), (self.mistral_url, "MISTRAL_URL")):
            if not re.match(r"https?://", url):
                raise FixerError(f"{name} doit commencer par http:// ou https:// (valeur : {url})")
        if not self.repo.is_dir():
            raise FixerError(f"REPO_PATH introuvable : {self.repo}")
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise FixerError(f"CA_BUNDLE introuvable : {self.ca_bundle}")
        if self.encoding:
            try:
                self.encoding = codecs.lookup(self.encoding).name
            except LookupError:
                raise FixerError(f"SOURCE_ENCODING inconnu : {self.encoding} (ex : utf-8, cp1252)")

    @staticmethod
    def _int(key: str, default: int) -> int:
        value = os.environ.get(key, "").strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            raise FixerError(f"{key} doit être un nombre (valeur : {value})")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def make_ssl_context(ca_bundle: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # magasin système (Windows inclus)
    if ca_bundle:
        try:
            ctx.load_verify_locations(ca_bundle)  # + certificat d'entreprise
        except (ssl.SSLError, OSError) as e:
            raise FixerError(f"CA_BUNDLE illisible ({ca_bundle}) : {e}. Il faut un fichier .pem (texte).")
    return ctx


def http_request(url: str, ctx: ssl.SSLContext, headers: dict, body: bytes = None,
                 timeout: int = 60, retries: int = 3) -> bytes:
    last_err = None
    short = url.split("?")[0]
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = decode_bytes(e.read() or b"")[:400]
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                last_err = f"HTTP {e.code}"
                time.sleep(3 * attempt)
                continue
            hint = ""
            if e.code == 401:
                hint = "\n-> Jeton refusé : vérifie le token (SONAR_TOKEN ou MISTRAL_API_KEY)."
            elif e.code == 403:
                hint = "\n-> Accès refusé : le token n'a pas les droits sur ce projet."
            elif e.code == 404:
                hint = "\n-> Adresse introuvable : vérifie l'URL et la clé de projet / le nom du modèle."
            raise HttpError(e.code, f"HTTP {e.code} sur {short} : {detail}{hint}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
            last_err = str(getattr(e, "reason", e))
            if attempt < retries:
                time.sleep(3 * attempt)
                continue
    up = (last_err or "").upper()
    hint = ""
    if "CERTIFICATE" in up:
        hint = "\n-> Problème de certificat : renseigne CA_BUNDLE dans .env (certificat de l'entreprise, .pem)."
    elif "WRONG_VERSION_NUMBER" in up:
        hint = "\n-> Vérifie http:// ou https:// dans l'URL."
    elif "PROXY" in up or "TUNNEL" in up:
        hint = "\n-> Proxy d'entreprise : ajoute l'hôte dans la variable d'environnement NO_PROXY."
    elif "TIMED OUT" in up or "TIMEOUT" in up:
        hint = "\n-> Délai dépassé : serveur lent ou injoignable (VPN ? proxy ?)."
    elif "GETADDRINFO" in up or "NAME OR SERVICE" in up or "NODENAME" in up:
        hint = "\n-> Nom de serveur inconnu : vérifie l'URL (et le VPN)."
    raise FixerError(f"Impossible de joindre {short} : {last_err}{hint}")


def parse_json(raw: bytes, what: str):
    try:
        return json.loads(decode_bytes(raw))
    except ValueError:
        raise FixerError(f"{what} : réponse non JSON (mauvaise URL ?) : {decode_bytes(raw)[:200]!r}")


# ---------------------------------------------------------------------------
# SonarQube
# ---------------------------------------------------------------------------

class Sonar:
    def __init__(self, cfg: Config, ctx: ssl.SSLContext):
        self.cfg, self.ctx = cfg, ctx
        token = base64.b64encode(f"{cfg.sonar_token}:".encode("utf-8")).decode("ascii")
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._component_param = "components"

    def get(self, path: str, params: dict) -> dict:
        url = f"{self.cfg.sonar_url}{path}?{urllib.parse.urlencode(params)}"
        return parse_json(interruptible(http_request, url, self.ctx, self.headers), "SonarQube")

    def version(self) -> str:
        raw = interruptible(http_request, f"{self.cfg.sonar_url}/api/server/version", self.ctx, self.headers)
        return decode_bytes(raw).strip()[:40]

    def search_issues(self, params: dict) -> dict:
        base = {"resolved": "false"}
        if self.cfg.sonar_branch:
            base["branch"] = self.cfg.sonar_branch
        base.update(params)
        try:
            return self.get("/api/issues/search", {**base, self._component_param: self.cfg.project})
        except HttpError as e:
            if e.code != 400 or self._component_param == "componentKeys":
                raise
            self._component_param = "componentKeys"  # anciennes versions de SonarQube
            return self.get("/api/issues/search", {**base, self._component_param: self.cfg.project})

    def rule_stats(self) -> list:
        data = self.search_issues({"facets": "rules", "ps": 1})
        for facet in data.get("facets", []):
            if facet.get("property") == "rules":
                return [(v["val"], v["count"]) for v in facet.get("values", [])]
        return []

    def total_open(self) -> int:
        data = self.search_issues({"ps": 1})
        return data.get("paging", {}).get("total", data.get("total", 0))

    def rule_info(self, key: str) -> tuple:
        rule = self.get("/api/rules/show", {"key": key}).get("rule", {})
        parts = []
        if rule.get("htmlDesc"):
            parts.append(rule["htmlDesc"])
        for section in rule.get("descriptionSections", []) or []:
            if section.get("key") in ("root_cause", "how_to_fix", "default", "introduction"):
                parts.append(section.get("content", ""))
        if not parts and rule.get("mdDesc"):
            parts.append(rule["mdDesc"])
        return rule.get("name", key), html_to_text(" ".join(parts))[:3000]

    def issues_for_rule(self, rule: str, on_page=None) -> list:
        issues, page, ps = [], 1, 500
        while True:
            data = self.search_issues({"rules": rule, "ps": ps, "p": page})
            batch = data.get("issues", [])
            issues.extend(batch)
            total = data.get("paging", {}).get("total", data.get("total", 0))
            if on_page:
                on_page(len(issues), total)
            if not batch or len(issues) >= total or page * ps >= 10000:
                return issues
            page += 1


def html_to_text(text: str) -> str:
    text = re.sub(r"<(br|/p|/li|/h\d|/pre)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


# ---------------------------------------------------------------------------
# LLM (API compatible OpenAI : Mistral, Devstral, vLLM...)
# ---------------------------------------------------------------------------

class LLM:
    def __init__(self, cfg: Config, ctx: ssl.SSLContext):
        self.cfg, self.ctx = cfg, ctx
        url = cfg.mistral_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions" if url.endswith("/v1") else "/v1/chat/completions"
        self.url = url
        self.headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        if cfg.mistral_key:
            self.headers["Authorization"] = f"Bearer {cfg.mistral_key}"

    def chat(self, messages: list) -> str:
        body = json.dumps({  # ensure_ascii : accents envoyés en \uXXXX, sans risque d'encodage
            "model": self.cfg.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.cfg.llm_max_tokens,
        }).encode("ascii")
        raw = interruptible(http_request, self.url, self.ctx, self.headers, body, self.cfg.llm_timeout)
        data = parse_json(raw, "LLM")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise FixerError(f"Réponse LLM inattendue : {str(data)[:300]}")
        if isinstance(content, list):  # certains serveurs renvoient des morceaux
            content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        return content or ""


SYSTEM_PROMPT = """Tu es un expert Java. Tu corriges UNE SEULE issue SonarQube dans un extrait de code.

Règles absolues :
- Ne change pas le comportement du code.
- Ne change aucune signature de méthode publique ou protégée (le code étend des API Calypso).
- Modifie le minimum de lignes possible, uniquement autour de l'issue.
- Garde exactement l'indentation, le style et les accents du code existant.
- N'ajoute pas de lignes vides, pas de commentaires d'explication.
- Si la correction est risquée, ambiguë, demande de modifier d'autres fichiers ou des imports,
  ou si le code ne correspond pas au message : réponds SKIP.

Format de réponse OBLIGATOIRE, rien d'autre :
START_LINE: <premier numéro de ligne remplacé>
END_LINE: <dernier numéro de ligne remplacé>
```java
<nouveau code qui remplace ces lignes, SANS les numéros de ligne>
```

Ou, si tu ne corriges pas, une seule ligne :
SKIP: <raison courte>"""

RETRY_PROMPT = """Ta réponse ne respecte pas le format. Réponds UNIQUEMENT avec :
START_LINE: N
END_LINE: M
```java
<code>
```
ou bien une seule ligne : SKIP: <raison>"""


def build_user_prompt(rule_key, rule_name, rule_desc, issue, rel_path, start, end, excerpt) -> str:
    return (
        f"Règle : {rule_key} - {rule_name}\n"
        f"Description de la règle :\n{rule_desc}\n\n"
        f"Message Sonar : {issue.get('message', '')}\n"
        f"Fichier : {rel_path}\n"
        f"Issue située lignes {start} à {end}.\n\n"
        f"Code (format 'numéro | code') :\n{excerpt}\n"
    )


# ---------------------------------------------------------------------------
# Analyse tolérante de la réponse du LLM
# ---------------------------------------------------------------------------

class Proposal:
    def __init__(self, start: int, end: int, code_lines: list):
        self.start, self.end, self.code_lines = start, end, code_lines


def parse_reply(text: str):
    """Retourne Proposal, ou ('SKIP', raison), ou None si illisible."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    m_start = re.search(r"START_LINE\s*[:=]\s*\**\s*(\d+)", text, re.I)
    m_end = re.search(r"END_LINE\s*[:=]\s*\**\s*(\d+)", text, re.I)
    if not (m_start and m_end):
        as_json = parse_json_reply(text)
        if as_json is not None:
            return as_json
        m_skip = re.search(r"^[\s*#>`_-]*SKIP[\s*_]*[:\-]\s*(.*)$", text, re.I | re.M)
        if m_skip:
            return ("SKIP", (m_skip.group(1).strip(" *`_") or "sans raison")[:200])
        if re.fullmatch(r"[\s*`_]*SKIP[\s*`_.]*", text, re.I):
            return ("SKIP", "sans raison")
        return None
    start, end = int(m_start.group(1)), int(m_end.group(1))
    after = text[max(m_start.end(), m_end.end()):]
    m_code = re.search(r"```[ \t]*[\w+-]*[ \t]*\n(.*?)\n?[ \t]*```", after, re.S)
    if m_code:
        code = m_code.group(1)
    else:  # pas de bloc ``` : on prend ce qui suit END_LINE
        code = after.split("\n", 1)[1] if "\n" in after else ""
        if not code.strip():
            return None
    return Proposal(start, end, clean_code(code.split("\n")))


def parse_json_reply(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("skip") is True:
        return ("SKIP", str(data.get("reason", "sans raison"))[:200])
    try:
        start, end = int(data["start_line"]), int(data["end_line"])
    except (KeyError, TypeError, ValueError):
        return None
    code = data.get("new_code", data.get("code", ""))
    return Proposal(start, end, clean_code(str(code).replace("\r\n", "\n").split("\n")))


def clean_code(lines: list) -> list:
    lines = [ln.rstrip("\r") for ln in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    non_empty = [ln for ln in lines if ln.strip()]
    # le LLM a recopié les numéros de ligne "  123 | code"
    if non_empty and all(re.match(r"^\s*\d+\s?\|", ln) for ln in non_empty):
        lines = [re.sub(r"^\s*\d+\s?\| ?", "", ln) for ln in lines]
    return lines


def fix_spacing(new_lines: list, old_lines: list) -> list:
    """Corrige deux défauts classiques : lignes vides parasites et indentation perdue."""
    if len(new_lines) >= 3:
        blank_new = sum(1 for ln in new_lines if not ln.strip()) / len(new_lines)
        blank_old = (sum(1 for ln in old_lines if not ln.strip()) / len(old_lines)) if old_lines else 0
        if blank_new >= 0.3 and blank_old < 0.15:
            new_lines = [ln for ln in new_lines if ln.strip()]
    old_ne = [ln for ln in old_lines if ln.strip()]
    new_ne = [ln for ln in new_lines if ln.strip()]
    if old_ne and new_ne:
        old_indent = re.match(r"[ \t]*", old_ne[0]).group(0)
        if old_indent and all(not re.match(r"[ \t]", ln) for ln in new_ne):
            new_lines = [(old_indent + ln) if ln.strip() else ln for ln in new_lines]
    return new_lines


# ---------------------------------------------------------------------------
# Fichiers source : encodage et fins de ligne préservés à l'octet près
# ---------------------------------------------------------------------------

class SourceFile:
    def __init__(self, path: Path, forced_encoding: str = ""):
        self.path = path
        self.raw = path.read_bytes()
        raw = self.raw
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise EncodingProblem("fichier en UTF-16, non géré")
        self.bom = raw.startswith(BOM)
        body = raw[3:] if self.bom else raw
        if b"\x00" in body:
            raise EncodingProblem("fichier binaire ou UTF-16, non géré")
        encodings = [forced_encoding] if forced_encoding else ["utf-8", "cp1252", "latin-1"]
        for enc in encodings:
            try:
                text = body.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise EncodingProblem(f"fichier illisible en {forced_encoding} (SOURCE_ENCODING)")
        if text.encode(enc) != body:
            raise EncodingProblem(f"encodage {enc} non réversible")
        self.encoding = enc
        self.ascii_only = all(b < 128 for b in body)
        self.lines = LINE_RE.findall(text)  # chaque élément garde sa propre fin de ligne
        endings = Counter(ln[len(ln.rstrip("\r\n")):] for ln in self.lines)
        endings.pop("", None)
        self.default_eol = endings.most_common(1)[0][0] if endings else "\n"

    def content(self, n: int) -> str:
        return self.lines[n - 1].rstrip("\r\n")

    def __len__(self) -> int:
        return len(self.lines)

    def replaced(self, start: int, end: int, new_lines: list) -> bytes:
        last = self.lines[end - 1]
        eol = last[len(last.rstrip("\r\n")):]
        if eol:
            new = [ln + eol for ln in new_lines]
        else:  # dernière ligne du fichier sans retour à la ligne
            new = [ln + self.default_eol for ln in new_lines[:-1]] + new_lines[-1:]
        data = "".join(self.lines[:start - 1] + new + self.lines[end:]).encode(self.encoding)
        return (BOM + data) if self.bom else data


def read_text_for_diff(data: bytes) -> list:
    body = data[3:] if data.startswith(BOM) else data
    return [ln.rstrip("\r\n") + "\n" for ln in LINE_RE.findall(decode_bytes(body))]


def sonar_line_hash(line: str) -> str:
    return hashlib.md5(re.sub(r"[\x00-\x20]", "", line).encode("utf-8")).hexdigest()


def find_module_dir(file_path: Path, repo: Path) -> Path:
    for parent in [file_path.parent, *file_path.parents]:
        if any((parent / f).exists() for f in ("pom.xml", "build.gradle", "build.gradle.kts", "build.xml")):
            return parent
        if parent == repo:
            break
    return repo


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text)


# ---------------------------------------------------------------------------
# Lancement de la compilation (arrêt propre sur Ctrl+C et délai dépassé)
# ---------------------------------------------------------------------------

def kill_tree(p: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run(cmd, cwd: Path, timeout: int = 600, shell: bool = False):
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        p = subprocess.Popen(cmd, cwd=str(cwd), shell=shell, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    except OSError as e:
        return 127, str(e)
    chunks: list = []

    def reader():
        for block in iter(lambda: p.stdout.read(65536), b""):
            chunks.append(block)
            if len(chunks) > 64:  # garde la fin (~4 Mo max)
                del chunks[:32]

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t0 = time.time()
    try:
        while p.poll() is None:
            if time.time() - t0 > timeout:
                kill_tree(p)
                t.join(5)
                return 124, decode_output(b"".join(chunks)) + f"\nDÉLAI DÉPASSÉ après {timeout} s (BUILD_TIMEOUT)"
            time.sleep(0.2)
    except KeyboardInterrupt:
        kill_tree(p)
        raise
    t.join(5)
    return p.returncode, decode_output(b"".join(chunks))


def fill_cmd(cmd: str, file_path: Path, module_dir: Path) -> str:
    return cmd.replace("{file}", str(file_path)).replace("{module_dir}", str(module_dir))


# ---------------------------------------------------------------------------
# État (reprise), sauvegardes et rapports
# ---------------------------------------------------------------------------

def state_path(rule: str) -> Path:
    return STATE_DIR / (safe_name(rule) + ".json")


def load_state(rule: str) -> dict:
    p = state_path(rule)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def save_state(rule: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = state_path(rule).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_path(rule))


class RunDir:
    """Dossier d'un run : rapport, diff, logs, sauvegardes des originaux."""

    def __init__(self, rule: str, dry_run: bool):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = REPORTS_DIR / f"{safe_name(rule)}_{stamp}{'_simulation' if dry_run else ''}"
        n = 2
        while self.path.exists():
            self.path = self.path.with_name(f"{self.path.name}-{n}")
            n += 1
        (self.path / "logs").mkdir(parents=True)
        self.manifest = {"rule": rule, "dry_run": dry_run, "files": {}}
        self.save_manifest()

    def backup(self, rel_path: str, abs_path: Path, original: bytes) -> None:
        if rel_path in self.manifest["files"]:
            return
        # nom court et plat : évite la limite Windows de 260 caractères par chemin
        name = rel_path.replace("\\", "/").split("/")[-1]
        dest = self.path / "backup" / f"{len(self.manifest['files']) + 1:04d}_{name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(original)
        self.manifest["files"][rel_path] = {"abs": str(abs_path), "backup": str(dest),
                                            "written_sha": sha256(original), "issues": []}
        self.save_manifest()

    def record_write(self, rel_path: str, data: bytes, issue_key: str) -> None:
        info = self.manifest["files"][rel_path]
        info["written_sha"] = sha256(data)
        info["issues"].append(issue_key)
        self.save_manifest()

    def save_manifest(self) -> None:
        tmp = self.path / "manifest.tmp"
        tmp.write_text(json.dumps(self.manifest, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path / "manifest.json")

    def write_issue_log(self, issue_key: str, sections: dict) -> None:
        body = "\n\n".join(f"===== {k} =====\n{v}" for k, v in sections.items())
        (self.path / "logs" / f"{safe_name(issue_key)}.txt").write_text(body, encoding="utf-8")

    def write_changes_diff(self) -> int:
        """Diff global (originaux -> fichiers actuels) pour relecture. Retourne le nb de fichiers modifiés."""
        parts, count = [], 0
        for rel, info in sorted(self.manifest["files"].items()):
            before = Path(info["backup"]).read_bytes()
            current = Path(info["abs"])
            after = current.read_bytes() if current.exists() else b""
            if before == after:
                continue
            count += 1
            parts.extend(difflib.unified_diff(read_text_for_diff(before), read_text_for_diff(after),
                                              fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
        if parts:
            (self.path / "modifications.diff").write_text("".join(parts), encoding="utf-8")
        return count


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

def cmd_check(cfg: Config, sonar: Sonar, llm: LLM) -> int:
    ok = True
    log("Vérification de la configuration (rien n'est modifié)\n")
    log("1/5 SonarQube")
    try:
        with Spinner("connexion à SonarQube"):
            version = sonar.version()
            total = sonar.total_open()
        log(f"    OK - version {version}, projet {cfg.project} : {total} issues ouvertes")
        if total == 0:
            log("    ATTENTION - 0 issue : vérifie SONAR_PROJECT_KEY et SONAR_BRANCH")
    except FixerError as e:
        ok = False
        log(f"    ÉCHEC - {e}")

    log("2/5 LLM")
    try:
        with Spinner(f"question test à {cfg.model}"):
            reply = llm.chat([{"role": "user", "content": "Réponds exactement : « Déjà vérifié, ça marche »"}])
        log(f"    OK - {llm.url} a répondu : {reply.strip()[:60]!r}")
        if "Ã" in reply:
            log("    ATTENTION - accents mal décodés dans la réponse (Ã...) : signale-le, le serveur LLM "
                "n'est pas en UTF-8.")
    except FixerError as e:
        ok = False
        log(f"    ÉCHEC - {e}")

    log("3/5 Dossier du repo")
    java_files = []
    with Spinner("recherche des fichiers .java"):
        for f in cfg.repo.rglob("*.java"):
            if not any(part.startswith(".") or part in ("target", "build", "node_modules") for part in f.parts):
                java_files.append(f)
    writable = os.access(str(cfg.repo), os.W_OK)
    if not java_files:
        ok = False
        log(f"    ÉCHEC - aucun fichier .java dans {cfg.repo}")
    elif not writable:
        ok = False
        log(f"    ÉCHEC - pas de droit d'écriture sur {cfg.repo}")
    else:
        log(f"    OK - {len(java_files)} fichiers .java, écriture autorisée")

    log("4/5 Encodage des fichiers (accents)")
    if java_files:
        counts, problems = Counter(), []
        with Spinner("lecture des fichiers") as sp:
            for i, f in enumerate(java_files, 1):
                if i % 200 == 0:
                    sp.set(f"lecture des fichiers {i}/{len(java_files)}")
                try:
                    src = SourceFile(f, cfg.encoding)
                    counts["ASCII pur (sans accent)" if src.ascii_only else src.encoding + (" + BOM" if src.bom else "")] += 1
                except (EncodingProblem, OSError) as e:
                    problems.append(f"{f.relative_to(cfg.repo)} : {e}")
        log("    " + " | ".join(f"{k} : {v}" for k, v in counts.most_common()))
        accented = [k for k in counts if not k.startswith("ASCII")]
        if len(accented) > 1:
            log("    ATTENTION - plusieurs encodages dans le repo. Le script s'adapte fichier par fichier ;")
            log("                vérifie juste que ta compilation passe (étape 5).")
        for p in problems[:5]:
            log(f"    ignoré : {p}")
        if problems:
            log(f"    ({len(problems)} fichier(s) seront ignorés)")

    log("5/5 Compilation (sans aucune modification, peut être long)")
    if not cfg.build_cmd:
        log("    IGNORÉE - BUILD_CMD vide : les corrections ne seront pas compilées par le script.")
        log("              Compile toi-même et relis le diff avant de commiter.")
        log("\nTout est prêt. Étape suivante : python sonar_fixer.py stats" if ok
            else "\nCorrige les points en ÉCHEC dans .env, puis relance : python sonar_fixer.py check")
        return 0 if ok else 1
    sample = java_files[0] if java_files else cfg.repo
    module_dir = find_module_dir(sample, cfg.repo) if java_files else cfg.repo
    cmd = fill_cmd(cfg.build_cmd, sample, module_dir)
    log(f"    commande : {cmd}")
    with Spinner("compilation en cours"):
        code, out = run(cmd, cfg.repo, cfg.build_timeout, shell=True)
    if code == 0:
        log("    OK - la compilation passe")
    else:
        ok = False
        log(f"    ÉCHEC (code {code}) - fin de la sortie :")
        for line in out.strip().splitlines()[-15:]:
            log("      " + line)

    log("\nTout est prêt. Étape suivante : python sonar_fixer.py stats" if ok
        else "\nCorrige les points en ÉCHEC dans .env, puis relance : python sonar_fixer.py check")
    return 0 if ok else 1


def cmd_stats(sonar: Sonar, top: int) -> int:
    with Spinner("récupération des statistiques Sonar") as sp:
        stats = sonar.rule_stats()
        names = {}
        for i, (key, _) in enumerate(stats[:top], 1):
            sp.set(f"lecture des règles {i}/{min(top, len(stats))}")
            try:
                names[key] = sonar.rule_info(key)[0]
            except FixerError:
                names[key] = "?"
    if not stats:
        log("Aucune issue ouverte trouvée (vérifie SONAR_PROJECT_KEY / SONAR_BRANCH).")
        return 1
    total = sum(c for _, c in stats)
    log(f"{total} issues ouvertes, {len(stats)} règles. Top {min(top, len(stats))} :\n")
    log(f"{'issues':>7}  {'cumul':>6}  {'règle':<22} nom")
    cumul = 0
    for key, count in stats[:top]:
        cumul += count
        log(f"{count:>7}  {100 * cumul / total:5.1f}%  {key:<22} {names.get(key, '?')[:70]}")
    log("\ncumul = part des issues couverte en traitant toutes les règles jusqu'à cette ligne.")
    log("Étape suivante : python sonar_fixer.py fix --rule <règle> --max 20 --dry-run")
    return 0


def issue_range(issue: dict) -> tuple:
    tr = issue.get("textRange") or {}
    start = tr.get("startLine") or issue.get("line") or 0
    end = tr.get("endLine") or start
    return int(start), int(max(end, start))


def short_path(rel: str) -> str:
    parts = rel.replace("\\", "/").split("/")
    return "/".join(parts[-2:])


def cmd_fix(cfg: Config, sonar: Sonar, llm: LLM, rule: str, max_issues: int,
            dry_run: bool, retry: bool) -> int:
    with Spinner("lecture de la règle") as sp:
        rule_name, rule_desc = sonar.rule_info(rule)
        sp.set("récupération des issues Sonar")
        all_issues = sonar.issues_for_rule(
            rule, lambda n, t: sp.set(f"récupération des issues Sonar {n}/{t}"))

    state = load_state(rule)
    done_status = {"OK"} if retry else {"OK", "SKIP", "REJECT"}
    todo = [i for i in all_issues if state.get(i["key"], {}).get("status") not in done_status]
    already = len(all_issues) - len(todo)
    # même fichier : de la DERNIÈRE ligne à la PREMIÈRE, pour garder les numéros valides
    todo.sort(key=lambda i: (i.get("component", ""), -issue_range(i)[0]))
    todo = todo[:max(0, max_issues)]

    log(f"=== Règle {rule} : {rule_name} ===")
    log(f"Issues ouvertes : {len(all_issues)} | déjà traitées avant : {already} | à traiter maintenant : {len(todo)}"
        f" (dans {len(set(i.get('component') for i in todo))} fichier(s))")
    if not todo:
        log("Rien à faire." + ("" if retry or not already else " (--retry pour retenter les SKIP/REJECT)"))
        return 0

    run_dir = RunDir(rule, dry_run)
    if dry_run:
        log("Mode SIMULATION : Mistral propose, rien n'est modifié ni compilé.")
    else:
        log("Les fichiers sont modifiés directement (aucune opération git).")
        log(f"Originaux sauvegardés dans : {run_dir.path / 'backup'}")
    if cfg.build_cmd:
        log("Pour chaque issue : Mistral propose -> contrôles -> écriture -> compilation"
            " -> gardé si OK, sinon fichier restauré.")
    else:
        log("Pour chaque issue : Mistral propose -> contrôles -> écriture.")
        log("PAS DE COMPILATION (BUILD_CMD vide) : compile toi-même et relis le diff avant de commiter.")
    log("Ctrl+C pour arrêter proprement à tout moment (le fichier en cours est restauré).\n")

    progress = RunProgress(len(todo))
    module_ok: dict = {}
    hash_stats = {"checked": 0, "matched": 0}
    proposals = []
    interrupted = False
    csv_file = open(run_dir.path / "rapport.csv", "w", newline="", encoding="utf-8-sig")  # Excel : accents OK
    writer = csv.writer(csv_file, delimiter=";")
    writer.writerow(["issue", "fichier", "ligne", "statut", "raison", "message Sonar"])
    CONSOLE.start_status(progress.status, progress.heartbeat)
    ctx = None
    try:
        for issue in todo:
            ctx = IssueContext(cfg, llm, rule, rule_name, rule_desc, issue, dry_run,
                               module_ok, hash_stats, run_dir, progress)
            progress.begin(f"{short_path(ctx.rel_path)}:{issue_range(issue)[0]}")
            try:
                status, reason, diff = ctx.process()
            except (KeyboardInterrupt, FixerError):
                raise
            except Exception as e:  # une issue qui plante ne doit jamais arrêter tout le run
                import traceback
                ctx.log_sections["Erreur inattendue"] = traceback.format_exc()
                status, reason, diff = ctx.reject(f"erreur inattendue : {type(e).__name__}: {e}"[:200])
            ctx = None
            progress.end(status)
            log(f"  {status:<6} {short_path(rel_or(issue, cfg))}:{issue_range(issue)[0]}  {reason}")
            writer.writerow([issue["key"], rel_or(issue, cfg), issue_range(issue)[0], status, reason,
                             issue.get("message", "")])
            csv_file.flush()
            if dry_run:
                if diff:
                    proposals.append(f"# {issue['key']} - {issue.get('message', '')}\n{diff}\n")
            else:
                state[issue["key"]] = {"status": status, "reason": reason}
                save_state(rule, state)
    except KeyboardInterrupt:
        interrupted = True
        if ctx is not None:
            ctx.restore()
    finally:
        CONSOLE.stop_status()
        csv_file.close()

    elapsed = fmt_dur(time.time() - progress.t0)
    c = progress.counts
    log("")
    log(f"=== {'INTERROMPU' if interrupted else 'Terminé'} en {elapsed} ===")
    log(f"OK {c['OK']} | SKIP {c['SKIP']} | REJECT {c['REJECT']}"
        + (f" | non traitées {len(todo) - progress.done}" if interrupted else ""))
    if interrupted:
        log("Le fichier en cours a été restauré. Relancer la même commande reprend là où ça s'est arrêté.")
    if dry_run:
        if proposals:
            (run_dir.path / "propositions.diff").write_text("".join(proposals), encoding="utf-8")
            log(f"Propositions à relire   : {run_dir.path / 'propositions.diff'}")
        log("Rien n'a été modifié. Pour appliquer : même commande sans --dry-run.")
    else:
        changed = run_dir.write_changes_diff()
        log(f"Fichiers modifiés       : {changed}")
        if changed:
            log(f"Modifications à relire  : {run_dir.path / 'modifications.diff'}")
            if not cfg.build_cmd:
                log("À FAIRE                 : compiler le projet avant de commiter (non compilé par le script)")
            log("Pour tout annuler       : python sonar_fixer.py undo")
    log(f"Rapport (Excel)         : {run_dir.path / 'rapport.csv'}")
    if c["SKIP"] or c["REJECT"]:
        log(f"Détail de chaque échec  : {run_dir.path / 'logs'}")
    return 130 if interrupted else 0


def rel_or(issue: dict, cfg: Config) -> str:
    comp = issue.get("component", "")
    project = issue.get("project") or cfg.project  # la clé projet peut contenir ':'
    if comp.startswith(project + ":"):
        return comp[len(project) + 1:]
    return comp.split(":", 1)[1] if ":" in comp else comp


def resolve_source(cfg: Config, rel_path: str) -> Path:
    """Chemin local du fichier. Si REPO_PATH n'est pas exactement la racine analysée par Sonar
    (sous-dossier ou dossier parent), la bonne racine est retrouvée automatiquement."""
    parts = [x for x in rel_path.replace("\\", "/").split("/") if x]
    base = cfg.repo / cfg.source_root if cfg.source_root else cfg.repo
    detected = getattr(cfg, "detected_base", None)
    if detected is not None and detected.joinpath(*parts).is_file():
        return detected.joinpath(*parts)
    if base.joinpath(*parts).is_file():
        return base.joinpath(*parts)
    candidates = list(base.parents)
    try:
        candidates += [d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except OSError:
        pass
    for root in candidates:
        if root.joinpath(*parts).is_file():
            if detected != root:
                cfg.detected_base = root
                log(f"  INFO : fichiers trouvés à partir de {root} (REPO_PATH = {cfg.repo}).")
                log(f"         Pour aller plus vite, mets REPO_PATH={root} dans .env.")
            return root.joinpath(*parts)
    return base.joinpath(*parts)


class IssueContext:
    """Traitement d'une issue : tout ce qui peut mal tourner aboutit à SKIP ou REJECT."""

    def __init__(self, cfg, llm, rule, rule_name, rule_desc, issue, dry_run, module_ok, hash_stats,
                 run_dir, progress):
        self.cfg, self.llm = cfg, llm
        self.rule, self.rule_name, self.rule_desc = rule, rule_name, rule_desc
        self.issue, self.dry_run = issue, dry_run
        self.module_ok, self.hash_stats = module_ok, hash_stats
        self.run_dir, self.progress = run_dir, progress
        self.rel_path = rel_or(issue, cfg)
        self.path = resolve_source(cfg, self.rel_path)
        self.original = None
        self.log_sections: dict = {}

    def step(self, text: str) -> None:
        self.progress.set_step(text)

    def restore(self) -> None:
        if self.original is not None and not self.dry_run:
            self.path.write_bytes(self.original)
            self.original = None

    def reject(self, reason: str, status: str = "REJECT"):
        self.restore()
        self.run_dir.write_issue_log(self.issue["key"], {
            "Issue": f"{self.rel_path}:{issue_range(self.issue)[0]}\n{self.issue.get('message', '')}",
            "Statut": f"{status} - {reason}", **self.log_sections})
        return status, reason, ""

    def process(self):
        self.step("lecture du fichier")
        start, end = issue_range(self.issue)
        if start <= 0:
            return self.reject("issue au niveau fichier (pas de ligne)", "SKIP")
        if not self.path.is_file():
            return self.reject(f"fichier introuvable localement ({self.path})", "SKIP")
        try:
            src = SourceFile(self.path, self.cfg.encoding)
        except EncodingProblem as e:
            return self.reject(str(e), "SKIP")
        if end > len(src):
            return self.reject("lignes hors du fichier (code différent de l'analyse Sonar)", "SKIP")

        shift = self.check_hash(src, start)
        if shift is None:
            return self.reject("code local différent de l'analyse Sonar (relance une analyse)", "SKIP")
        start, end = start + shift, end + shift

        ctx_from = max(1, start - self.cfg.context_lines)
        ctx_to = min(len(src), end + self.cfg.context_lines)
        excerpt = "\n".join(f"{i:>6} | {src.content(i)}" for i in range(ctx_from, ctx_to + 1))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(self.rule, self.rule_name, self.rule_desc,
                                                          self.issue, self.rel_path, start, end, excerpt)},
        ]
        try:
            self.step("Mistral propose une correction")
            reply = self.llm.chat(messages)
            self.log_sections["Réponse LLM"] = reply
            result = parse_reply(reply)
            if result is None:  # une seconde chance avec rappel du format
                self.step("Mistral, 2e essai (format non respecté)")
                messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": RETRY_PROMPT}]
                reply = self.llm.chat(messages)
                self.log_sections["Réponse LLM (2e essai)"] = reply
                result = parse_reply(reply)
        except FixerError as e:
            return self.reject(f"appel LLM en échec : {str(e).splitlines()[0][:150]}")
        if result is None:
            return self.reject("réponse LLM illisible")
        if isinstance(result, tuple):
            return self.reject(f"LLM : {result[1]}", "SKIP")

        self.step("contrôles")
        p = result
        if not (ctx_from <= p.start <= p.end <= ctx_to):
            return self.reject(f"lignes {p.start}-{p.end} hors de l'extrait fourni")
        if p.start > end or p.end < start:
            return self.reject(f"lignes {p.start}-{p.end} ne couvrent pas l'issue ({start}-{end})")
        old_lines = [src.content(i) for i in range(p.start, p.end + 1)]
        new_lines = fix_spacing(p.code_lines, old_lines)
        old_text = "\n".join(old_lines)
        if unicodedata.normalize("NFC", old_text) == old_text:  # même forme d'accents que le fichier
            new_lines = [unicodedata.normalize("NFC", ln) for ln in new_lines]
        # retire les lignes recopiées à l'identique en début/fin (diff minimal)
        while old_lines and new_lines and old_lines[0].rstrip() == new_lines[0].rstrip() and p.start < p.end:
            old_lines.pop(0), new_lines.pop(0)
            p.start += 1
        while old_lines and new_lines and old_lines[-1].rstrip() == new_lines[-1].rstrip() and p.start < p.end:
            old_lines.pop(), new_lines.pop()
            p.end -= 1
        if len(old_lines) > self.cfg.max_changed or len(new_lines) > self.cfg.max_changed:
            return self.reject(f"modification trop grosse ({len(old_lines)} -> {len(new_lines)} lignes)")
        if len(new_lines) > 2 * len(old_lines) + 5:
            return self.reject(f"trop de lignes ajoutées ({len(old_lines)} -> {len(new_lines)})")
        if [ln.rstrip() for ln in new_lines] == [ln.rstrip() for ln in old_lines]:
            return self.reject("aucun changement proposé", "SKIP")
        lost, added = non_ascii_changes(old_lines, new_lines)
        if lost:
            return self.reject(f"accents/caractères spéciaux perdus ou modifiés : {show_chars(lost)}")
        if added:
            return self.reject(f"caractères spéciaux ajoutés (accents mal encodés ?) : {show_chars(added)}")
        try:
            new_bytes = src.replaced(p.start, p.end, new_lines)
        except UnicodeEncodeError:
            return self.reject(f"caractères incompatibles avec l'encodage {src.encoding}")

        diff = (f"--- a/{self.rel_path}\n+++ b/{self.rel_path}\n"
                f"@@ -{p.start},{len(old_lines)} +{p.start},{len(new_lines)} @@\n"
                + "".join(f"-{ln}\n" for ln in old_lines)
                + "".join(f"+{ln}\n" for ln in new_lines))
        self.log_sections["Diff proposé"] = diff
        if self.dry_run:
            return "OK", f"proposé (lignes {p.start}-{p.end})", diff

        module_dir = find_module_dir(self.path, self.cfg.repo)
        reason = self.ensure_baseline(module_dir)
        if reason:
            return self.reject(reason, "SKIP")

        self.step("écriture")
        self.run_dir.backup(self.rel_path, self.path, src.raw)
        self.original = src.raw
        self.path.write_bytes(new_bytes)
        for label, cmd in (("compilation", self.cfg.build_cmd), ("tests", self.cfg.test_cmd)):
            if not cmd:
                continue
            self.step(f"{label} en cours")
            code, out = run(fill_cmd(cmd, self.path, module_dir), self.cfg.repo, self.cfg.build_timeout, shell=True)
            if code != 0:
                self.log_sections[f"Sortie {label}"] = "\n".join(out.strip().splitlines()[-80:])
                why = "délai dépassé" if code == 124 else "échec"
                return self.reject(f"{label} : {why} après modification, fichier restauré")
        self.original = None
        self.run_dir.record_write(self.rel_path, new_bytes, self.issue["key"])
        done = "compile" if self.cfg.build_cmd else "non compilé"
        return "OK", f"lignes {p.start}-{p.end} -> {len(new_lines)} ligne(s), {done}", diff

    def check_hash(self, src: SourceFile, start: int):
        """0 si la ligne correspond, décalage si retrouvée à proximité, None si introuvable."""
        expected = self.issue.get("hash")
        hs = self.hash_stats
        if not self.cfg.verify_hash or not expected or hs.get("disabled"):
            return 0
        hs["checked"] += 1
        if sonar_line_hash(src.content(start)) == expected:
            hs["matched"] += 1
            return 0
        candidates = [d for d in range(-60, 61)
                      if 1 <= start + d <= len(src) and sonar_line_hash(src.content(start + d)) == expected]
        if len(candidates) == 1:  # une seule ligne identique à proximité : on suit le décalage
            hs["matched"] += 1
            return candidates[0]
        if hs["checked"] >= 5 and hs["matched"] == 0:
            hs["disabled"] = True
            log("  ATTENTION : la vérification des lignes Sonar ne correspond jamais, elle est désactivée"
                " pour ce run (VERIFY_LINE_HASH).")
            return 0
        return None

    def ensure_baseline(self, module_dir: Path):
        if not self.cfg.build_cmd:
            return None
        key = str(module_dir)
        if key not in self.module_ok:
            if "{file}" in self.cfg.build_cmd:
                self.module_ok[key] = True  # compilation par fichier : pas de référence possible
            else:
                self.step(f"compilation de référence du module {module_dir.name} (avant modification)")
                code, out = run(fill_cmd(self.cfg.build_cmd, self.path, module_dir), self.cfg.repo,
                                self.cfg.build_timeout, shell=True)
                self.module_ok[key] = code == 0
                if code != 0:
                    log(f"  ATTENTION : le module {module_dir.name} ne compile DÉJÀ pas sans modification ;"
                        " ses issues sont ignorées.")
                    self.log_sections["Compilation de référence"] = "\n".join(out.strip().splitlines()[-80:])
        return None if self.module_ok[key] else "le module ne compile pas avant modification"


def cmd_undo(run_name: str = "") -> int:
    runs = sorted((d for d in REPORTS_DIR.glob("*") if (d / "manifest.json").exists()
                   and not d.name.endswith("_simulation")), key=lambda d: d.stat().st_mtime) \
        if REPORTS_DIR.exists() else []
    if run_name:
        runs = [d for d in runs if d.name == run_name]
    runs = [d for d in runs if not json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("undone")]
    if not runs:
        log("Aucun run à annuler." + (f" ({run_name} introuvable ou déjà annulé)" if run_name else ""))
        return 1
    run_dir = runs[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    log(f"Annulation du run {run_dir.name} ({len(manifest['files'])} fichier(s))")
    state = load_state(manifest["rule"])
    restored = kept = 0
    for rel, info in sorted(manifest["files"].items()):
        target, backup = Path(info["abs"]), Path(info["backup"])
        if not backup.exists():
            log(f"  sauvegarde absente, ignoré : {rel}")
            kept += 1
            continue
        current = target.read_bytes() if target.exists() else None
        if current is not None and sha256(current) != info["written_sha"]:
            log(f"  modifié depuis le run, NON restauré (à vérifier à la main) : {rel}")
            kept += 1
            continue
        target.write_bytes(backup.read_bytes())
        restored += 1
        for key in info.get("issues", []):
            state.pop(key, None)
    save_state(manifest["rule"], state)
    manifest["undone"] = True
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"Terminé : {restored} fichier(s) restauré(s), {kept} non touché(s).")
    return 0


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Corrige des issues SonarQube avec le LLM interne.")
    parser.add_argument("--env", default=str(TOOL_DIR / ".env"), help="fichier de configuration (défaut : .env)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="teste Sonar, le LLM, les encodages et la compilation")
    p_stats = sub.add_parser("stats", help="top des règles par nombre d'issues")
    p_stats.add_argument("--top", type=int, default=30)
    p_fix = sub.add_parser("fix", help="corrige les issues d'une règle")
    p_fix.add_argument("--rule", required=True, help="ex : java:S1155")
    p_fix.add_argument("--max", type=int, default=20, help="nombre max d'issues à traiter (défaut 20)")
    p_fix.add_argument("--dry-run", action="store_true", help="simulation : propose sans rien modifier")
    p_fix.add_argument("--retry", action="store_true", help="retente les issues SKIP/REJECT des runs précédents")
    p_undo = sub.add_parser("undo", help="annule le dernier run (restaure les fichiers d'origine)")
    p_undo.add_argument("--run", default="", help="nom du dossier du run dans reports/ (défaut : le dernier)")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "undo":
            return cmd_undo(args.run)
        load_env_file(Path(args.env))
        cfg = Config()
        ctx = make_ssl_context(cfg.ca_bundle)
        sonar, llm = Sonar(cfg, ctx), LLM(cfg, ctx)
        if args.command == "check":
            return cmd_check(cfg, sonar, llm)
        if args.command == "stats":
            return cmd_stats(sonar, args.top)
        return cmd_fix(cfg, sonar, llm, args.rule, args.max, args.dry_run, args.retry)
    except FixerError as e:
        CONSOLE.stop_status()
        log(f"\nERREUR : {e}")
        return 2
    except KeyboardInterrupt:
        CONSOLE.stop_status()
        log("\nInterrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
