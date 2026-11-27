"""Create and verify Smart Invoice System backups without writing to the live database.

Usage (from the project root, with the project virtual environment):
    env/bin/python scripts/backup_verify.py create <label>
    env/bin/python scripts/backup_verify.py verify <backup-dir>

`create` uses SQLite's online backup API (consistent copy, read-only on the
source), copies stored documents and a source snapshot, and writes manifest.json.
`verify` restores the backup into a temporary directory, checks SQLite integrity,
foreign keys, table row counts, stored-document SHA-256 hashes, source hashes,
and recomputes the SmartKart June-November 2026 financial baseline from the
restored copy in a separate process (locmem email, no live writes).
Secrets (.env), the virtual environment and earlier backups are never copied.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORED = ("media", "private_expense_files")
SKIP_DIRS = {"env", ".backups", ".git", "media", "private_expense_files", "__pycache__", ".demo", "node_modules", "Final_Report"}
SKIP_SUFFIX = {".sqlite3", ".pyc", ".zip"}
BASELINE = {
    "sales": "87663.50", "purchases": "68861.50", "expenses": "32069.73",
    "receivable": "17292.00", "purchase_payables": "36831.50",
    "expense_payables": "27176.03", "payable": "64007.53",
}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def table_counts(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
        return {n: con.execute(f'select count(*) from "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


def db_checks(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            "integrity": con.execute("pragma integrity_check").fetchone()[0],
            "foreign_key_violations": len(con.execute("pragma foreign_key_check").fetchall()),
        }
    finally:
        con.close()


def stored_files(base):
    out = {}
    for top in STORED:
        for p in sorted((base / top).rglob("*")):
            if p.is_file() and p.name != ".DS_Store":
                out[str(p.relative_to(base))] = sha(p)
    return out


def source_files(base):
    out = {}
    for dirpath, dirnames, filenames in os.walk(base):
        rel = Path(dirpath).relative_to(base)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (rel == Path(".") and d.startswith("."))]
        for name in filenames:
            p = Path(dirpath) / name
            if name in {".env", ".DS_Store"} or p.suffix in SKIP_SUFFIX:
                continue
            out[str(p.relative_to(base))] = sha(p)
    return out


def create(label):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-IST"
    dest = ROOT / ".backups" / f"{label}-{stamp}"
    dest.mkdir(parents=True, mode=0o700)
    src = sqlite3.connect(f"file:{ROOT / 'db.sqlite3'}?mode=ro", uri=True)
    dst = sqlite3.connect(dest / "db.sqlite3")
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    for top in STORED:
        shutil.copytree(ROOT / top, dest / top, ignore=shutil.ignore_patterns(".DS_Store"))
    sources = source_files(ROOT)
    for rel in sources:
        target = dest / "source" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    manifest = {
        "backup": str(dest), "created": stamp,
        "database_sha256": sha(dest / "db.sqlite3"),
        "tables": table_counts(dest / "db.sqlite3"),
        "files": stored_files(dest), "sources": sources,
        "excluded": ["env/", ".env (secrets)", ".git/", ".backups/ (earlier backups)", "__pycache__"],
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps({"backup": str(dest), "tables": len(manifest["tables"]), "stored_files": len(manifest["files"]), "source_files": len(sources)}))
    return dest


FINANCE = r"""
import json, os, sys, django
sys.path.insert(0, os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from datetime import date
from decimal import Decimal
from django.contrib.auth import get_user_model
from analytics.services import build_analytics_context, CONFIRMED_STATUSES
from invoices.ledger import invoice_outstanding
from invoices.models import Invoice
user = get_user_model().objects.get(business_profile__business_name='SmartKart Traders')
start, end = date(2026,6,1), date(2026,11,30)
ctx = build_analytics_context(user, start, end, 'custom')
t = ctx['totals']
exp = sum((invoice_outstanding(i, as_of=end) for i in Invoice.objects.filter(user=user, invoice_type='expense', status__in=CONFIRMED_STATUSES, invoice_date__range=(start,end))), Decimal('0'))
out = {k: str(Decimal(t[k]).quantize(Decimal('0.01'))) for k in ('sales','purchases','expenses','receivable','payable')}
out['expense_payables'] = str(Decimal(exp).quantize(Decimal('0.01')))
out['purchase_payables'] = str((Decimal(t['payable']) - Decimal(exp)).quantize(Decimal('0.01')))
print(json.dumps(out))
"""


def finance(db_path):
    env = dict(os.environ, SQLITE_PATH=str(db_path), EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DEBUG="true")
    r = subprocess.run([sys.executable, "-c", FINANCE], cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode:
        return {"error": r.stderr.strip().splitlines()[-1:]}
    return json.loads(r.stdout.strip().splitlines()[-1])


def verify(backup):
    backup = Path(backup).resolve()
    m = json.loads((backup / "manifest.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shutil.copy2(backup / "db.sqlite3", tmp / "db.sqlite3")
        for top in STORED:
            shutil.copytree(backup / top, tmp / top)
        restored_db = tmp / "db.sqlite3"
        counts = table_counts(restored_db)
        files = stored_files(tmp)
        fin = finance(restored_db)
        result = {
            "backup": str(backup),
            "scope": "isolated temporary restoration; live database not opened for writing",
            "database_sha256_matches": sha(restored_db) == m["database_sha256"],
            **db_checks(restored_db),
            "tables": len(counts),
            "table_counts_match": counts == m["tables"],
            "stored_files": len(files),
            "stored_files_sha256_match": files == m["files"],
            "source_files": len(m.get("sources", {})),
            "source_sha256_match": all(sha(backup / "source" / k) == v for k, v in m.get("sources", {}).items()),
            "financial_baseline": fin,
            "financial_baseline_matches": all(fin.get(k) == v for k, v in BASELINE.items()),
        }
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    if sys.argv[1] == "create":
        create(sys.argv[2])
    elif sys.argv[1] == "verify":
        ok = verify(sys.argv[2])
        sys.exit(0 if ok["integrity"] == "ok" and ok["table_counts_match"] and ok["stored_files_sha256_match"] and ok["financial_baseline_matches"] else 1)
