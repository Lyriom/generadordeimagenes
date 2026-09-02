"""Instala una recuperación acotada para chunks JS durante un despliegue.

El navegador puede intentar cargar un componente perezoso en los pocos segundos
en que Docker reemplaza el frontend. Streamlit muestra ese rechazo dentro de la
UI y no vuelve a intentarlo. Este parche recarga una sola vez por ventana de 30
segundos; el límite impide bucles si existe un fallo real y persistente.
"""
from __future__ import annotations

from pathlib import Path
import re

import streamlit

MARKER = "cvmvp-dynamic-import-recovery"
RECOVERY_SCRIPT = f"""    <script id="{MARKER}">
      (() => {{
        const pattern = /failed to fetch dynamically imported module|importing a module script failed|error loading dynamically imported module/i;
        const storageKey = "{MARKER}-last-reload";

        const messageOf = (value) => {{
          if (!value) return "";
          return [value.message, value.reason?.message, String(value)]
            .filter(Boolean)
            .join(" ");
        }};

        const recover = (value) => {{
          const message = messageOf(value);
          const pageText = document.body?.innerText || "";
          if (!pattern.test(message) && !pattern.test(pageText)) return;

          const now = Date.now();
          const lastReload = Number(sessionStorage.getItem(storageKey) || 0);
          if (now - lastReload < 30000) return;
          sessionStorage.setItem(storageKey, String(now));
          window.location.reload();
        }};

        window.addEventListener("unhandledrejection", (event) => recover(event.reason));
        window.addEventListener("error", (event) => recover(event.error || event.message), true);
        window.addEventListener("DOMContentLoaded", () => {{
          const observer = new MutationObserver(() => recover());
          observer.observe(document.body, {{ childList: true, subtree: true }});
        }});
      }})();
    </script>
"""


def patch() -> Path:
    index = Path(streamlit.__file__).resolve().parent / "static" / "index.html"
    html = index.read_text(encoding="utf-8")
    if MARKER in html:
        return index
    if "</head>" not in html:
        raise RuntimeError(f"No se encontró </head> en {index}")
    index.write_text(
        html.replace("</head>", f"{RECOVERY_SCRIPT}  </head>", 1),
        encoding="utf-8",
    )
    if MARKER not in index.read_text(encoding="utf-8"):
        raise RuntimeError("No se pudo instalar la recuperación de chunks")
    return index


def validate_javascript_assets(index: Path) -> int:
    """Comprueba los imports iniciales y perezosos del bundle de Streamlit."""
    root = index.parent
    html = index.read_text(encoding="utf-8")
    references = set(
        re.findall(r'(?:src|href)="\./(static/js/[^"]+\.js)"', html)
    )
    entry_candidates = [
        relative
        for relative in references
        if "/index." in relative and "index.esm" not in relative
    ]
    if len(entry_candidates) != 1:
        raise RuntimeError(
            f"No se pudo identificar un único bundle de entrada: {entry_candidates}"
        )
    entry = (root / entry_candidates[0]).read_text(encoding="utf-8")
    references.update(
        f"static/js/{name}"
        for name in re.findall(r"""["']\./([A-Za-z0-9_.-]+\.js)["']""", entry)
    )
    missing = sorted(relative for relative in references if not (root / relative).is_file())
    if missing:
        raise RuntimeError(f"Chunks JavaScript ausentes: {', '.join(missing)}")
    return len(references)


if __name__ == "__main__":
    patched = patch()
    chunks = validate_javascript_assets(patched)
    print(f"Recuperación instalada; {chunks} módulos JavaScript verificados en {patched}")
