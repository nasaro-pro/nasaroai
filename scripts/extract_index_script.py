"""Extract main inline script from index.html for node --check."""
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
html = (root / "index.html").read_text(encoding="utf-8")
marker = '<script src="/static/nasaro-features.js'
idx = html.find(marker)
if idx == -1:
    blocks = re.findall(r"<script(?:[^>]*)>([\s\S]*?)</script>", html)
    body = max(blocks, key=len)
else:
    start = html.find("<script>", idx)
    end = html.find("</script>", start)
    body = html[start + len("<script>") : end]

out = root / "_check_index.js"
out.write_text(body, encoding="utf-8")
print(f"extracted {len(body)} chars -> {out.name}")
