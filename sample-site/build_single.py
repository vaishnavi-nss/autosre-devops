"""Writes AutoSRE_Incident_Console.html: index.html with data.js inlined, so the whole site is one file that opens anywhere (double-click, email, preview)."""
from pathlib import Path

here = Path(__file__).parent
html = (here / "index.html").read_text(encoding="utf-8")
data = (here / "data.js").read_text(encoding="utf-8")
tag = '<script src="data.js"></script>'
assert tag in html
safe = data.replace("</script", "<" + chr(92) + "/script")        # a literal closing tag inside the data would end the script early
out = html.replace(tag, "<script>" + chr(10) + safe + "</script>")
(here / "AutoSRE_Incident_Console.html").write_text(out, encoding="utf-8")
print("wrote", here / "AutoSRE_Incident_Console.html", len(out) // 1024, "KB")
