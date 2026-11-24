"""Rebuild docs/index.html from template.html + data.json (inlines the data so the page also works from file://)."""
import json
from pathlib import Path

here = Path(__file__).parent
data = json.loads((here / "data.json").read_text(encoding="utf-8"))
html = (here / "template.html").read_text(encoding="utf-8")
assert html.count("/*__DATA__*/null") == 1
(here / "index.html").write_text(html.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"))), encoding="utf-8")
print("wrote docs/index.html")
