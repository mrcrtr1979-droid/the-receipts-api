#!/usr/bin/env python3
"""Refresh dataset metadata in datasets/ from the real Brain source files.

Stdlib only. For every dataset file in datasets/ that has a source in SOURCES:
  * measure the row (or file) count from the source content,
  * read the newest date FROM CONTENT (a date column), never from file mtime,
  * set the stated count field, dateModified and the "url" (the file's own
    .json URL) in the dataset JSON, changing nothing else,
  * mirror the same measured values into datasets/index.html, index.json and
    croissant.json (displayed count, date range, catalog dateModified, .json URLs).
A dataset with no source in the Brain is left byte-identical and listed UNMATCHED.
No count is ever guessed.

Usage:
  python3 scripts/refresh_datasets.py --brain /path/to/carter-brain [REPO]
  python3 scripts/refresh_datasets.py --brain DIR --dry-run REPO
  python3 scripts/refresh_datasets.py --selftest
"""
import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import re
import sys
import tempfile

BASE_URL = "https://api.receiptsindex.com/datasets/"
INDEX_FILES = ("index.json", "croissant.json", "index.html")

# name -> how to measure it. Paths are relative to --brain.
#   kind "csv":      one CSV, date_col holds the date (first 10 chars used)
#   kind "jsonl":    one JSON object per line, date_col is the key holding the timestamp
#   kind "csvset":   several CSVs in `dir`, file list read from the dataset JSON hasPart
#   kind "filetree": count files under `path` (minus `exclude`); date read by regex
#                    from the `date_file` text inside it; no coverage range is set
#   path None:       no source file in the Brain, dataset is left untouched (UNMATCHED)
SOURCES = {
    "citation_corpus": {"kind": "csv", "path": "corpus/namebeam/citation_corpus.csv",
                        "date_col": "date_utc", "count_field": "rowCount"},
    "intelligence_ledger": {"kind": "csv", "path": "intelligence_ledger.csv",
                            "date_col": "date", "count_field": "rowCount"},
    "filing_room_ledger": {"kind": "csv", "path": "sensors/filing_room_ledger.csv",
                           "date_col": "date", "count_field": "rowCount"},
    "receipts_api_ledger": {"kind": "csv", "path": "sensors/receipts_api_ledger.csv",
                            "date_col": "date", "count_field": "rowCount"},
    "get_picked_ledger": {"kind": "csv", "path": "sensors/get_picked_ledger.csv",
                          "date_col": "date", "count_field": "rowCount"},
    "world_state": {"kind": "csv", "path": "sensors/data/WORLD_STATE.csv",
                    "date_col": "date", "count_field": "rowCount"},
    "planetary_sensor_family": {"kind": "csvset", "path": "sensors/data",
                                "date_col": "captured_utc", "count_field": "rowCount"},
    "agent_status_bus": {"kind": "jsonl", "path": "status/agent_status.jsonl",
                         "date_col": "ts", "count_field": "rowCount"},
    "legacy_agent_corpus_2026": {"kind": "filetree", "path": "_archive/LEGACY_AGENT_CORPUS_2026",
                                 "exclude": ["MANIFEST.md"], "date_file": "MANIFEST.md",
                                 "date_regex": r"Compiled (\d{4}-\d{2}-\d{2})",
                                 "count_field": "fileCount"},
    "makers_receipt_storefront_pulse": {"kind": None, "path": None,
                                        "reason": "narrative ledger across 4 source files, no single measurable Brain source"},
}

# Free-text that would otherwise go out of date, rebuilt from measured values only.
#   cadence: refreshCadence value in the dataset JSON
#   file_desc: whole description in the dataset JSON (or file_desc_re: (regex, template) sub)
#   idx: description in index.json and the index.html JSON-LD
#   cro: description in croissant.json
#   cell: cadence cell in the index.html table
# Placeholders: {count} {start} {end} {files} (own hasPart/file count) {fam} (planetary file count)
TEXT = {
    "filing_room_ledger": {
        "cadence": "intended daily; {count} captures from {start} to {end}",
        "idx": "{count} rows, dated captures {start} to {end}. Filing Room entry-count ledger.",
        "cro": "{count} rows ({start}/{end}), Filing Room entry-count ledger.",
        "cell": "Intended daily"},
    "receipts_api_ledger": {
        "cadence": "intended daily; {count} captures from {start} to {end}",
        "idx": "{count} rows, dated captures {start} to {end}. Receipts API page-byte and structured-data ledger.",
        "cro": "{count} rows ({start}/{end}), Receipts API page-byte and structured-data ledger.",
        "cell": "Intended daily"},
    "get_picked_ledger": {
        "cadence": "daily; {count} captures from {start} to {end}",
        "idx": "{count} rows, dated captures {start} to {end}. Get Picked Gumroad listing status and price ledger.",
        "cro": "{count} rows ({start}/{end}), Get Picked Gumroad listing status and price ledger.",
        "cell": "Daily"},
    "world_state": {
        "cadence": "intended daily; newest row {end}",
        "file_desc": "Daily rollup across sun/earth/ground/sky/money/void/mind planetary signals, sourced from a {fam}-file live sensor family. Newest row {end}.",
        "idx": "{count} rows of dated world-state records, fed by the planetary sensor family.",
        "cro": "{count} rows ({start}/{end}), fed by the planetary sensor family.",
        "cell": "Intended daily"},
    "planetary_sensor_family": {
        "cadence": "feeds world_state daily",
        "file_desc_re": (r"\b\d+( independently-addressable)", "{files}\\1"),
        "idx": "{count} combined rows across {files} sensor files, feeding world_state on a daily cadence.",
        "cro": "{count} combined rows across {files} files, feeds world_state daily ({start}/{end})."},
    "legacy_agent_corpus_2026": {
        "file_desc_re": (r"^90\+ days of agent", "Agent")},
}

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def parse_date(value, ceiling):
    """YYYY-MM-DD from the start of value, or None. Dates after ceiling are rejected."""
    m = DATE_RE.match((value or "").strip())
    if not m:
        return None
    try:
        d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    if d > ceiling:
        return None
    return d.isoformat()


def csv_rows(path):
    """Return (header, data_rows). Skips # comment lines that sit outside quoted
    fields, blank lines and rows whose cells are all empty. Header is excluded."""
    with open(path, newline="", encoding="utf-8") as f:
        text = f.read()
    kept = []
    in_quote = False
    for line in text.split("\n"):
        if not in_quote and line.startswith("#"):
            continue
        kept.append(line)
        if line.count('"') % 2 == 1:
            in_quote = not in_quote
    reader = csv.reader(io.StringIO("\n".join(kept), newline=""))
    header = None
    rows = []
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        if header is None:
            header = [c.strip() for c in row]
            continue
        rows.append(row)
    if header is None:
        raise ValueError("no header row in %s" % path)
    return header, rows


def measure_csv(path, date_col, ceiling):
    header, rows = csv_rows(path)
    if date_col not in header:
        raise ValueError("date column %r not in header of %s" % (date_col, path))
    i = header.index(date_col)
    dates = []
    undated = 0
    for r in rows:
        d = parse_date(r[i] if i < len(r) else "", ceiling)
        if d:
            dates.append(d)
        else:
            undated += 1
    return len(rows), dates, undated


def measure_jsonl(path, date_col, ceiling):
    n = 0
    dates = []
    undated = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                undated += 1
                continue
            if not isinstance(obj, dict):
                undated += 1
                continue
            n += 1
            d = parse_date(str(obj.get(date_col, "")), ceiling)
            if d:
                dates.append(d)
            else:
                undated += 1
    return n, dates, undated


def digest_of(path):
    """(identifier, contentSize) of a whole source file: first 16 hex of its SHA-256, and its byte size.
    Method matches the DATASET_LEGS inventory (sha256sum of the actual file, first 16)."""
    with open(path, "rb") as f:
        data = f.read()
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16], str(len(data))


def measure(name, spec, brain, dataset_obj, ceiling):
    """Return dict(count, start, end, notes, sources, files, identifier, size, drop_hash, sub) or raise."""
    kind = spec["kind"]
    notes = []
    srcs = []
    ident = size = None
    drop_hash = False
    sub = None
    if kind == "csv":
        p = os.path.join(brain, spec["path"])
        ident, size = digest_of(p)
        count, dates, undated = measure_csv(p, spec["date_col"], ceiling)
        srcs.append(spec["path"])
    elif kind == "jsonl":
        p = os.path.join(brain, spec["path"])
        ident, size = digest_of(p)
        count, dates, undated = measure_jsonl(p, spec["date_col"], ceiling)
        srcs.append(spec["path"])
    elif kind == "csvset":
        parts = dataset_obj.get("hasPart")
        if not parts:
            raise ValueError("csvset needs hasPart in the dataset JSON")
        count = 0
        dates = []
        undated = 0
        for fn in parts:
            p = os.path.join(brain, spec["path"], fn)
            c, d, u = measure_csv(p, spec["date_col"], ceiling)
            count += c
            dates += d
            undated += u
            srcs.append(spec["path"] + "/" + fn)
        notes.append("files=%d" % len(parts))
        # the published hash-of-hashes recipe cannot be verified from the Brain, so the
        # identifier and contentSize fields are removed rather than left wrong
        drop_hash = True
    elif kind == "filetree":
        root = os.path.join(brain, spec["path"])
        count = 0
        sub = {}
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if fn not in spec.get("exclude", []):
                    count += 1
                    rel = os.path.relpath(dp, root).split(os.sep)[0]
                    sub[rel] = sub.get(rel, 0) + 1
        with open(os.path.join(root, spec["date_file"]), encoding="utf-8") as f:
            m = re.search(spec["date_regex"], f.read())
        if not m:
            raise ValueError("date regex found nothing in %s" % spec["date_file"])
        d = parse_date(m.group(1), ceiling)
        if not d:
            raise ValueError("bad date in %s" % spec["date_file"])
        srcs.append(spec["path"])
        return {"count": count, "start": None, "end": d, "notes": notes, "sources": srcs,
                "files": None, "identifier": None, "size": None, "drop_hash": False, "sub": sub}
    else:
        raise ValueError("unknown kind %r" % kind)
    if not dates:
        raise ValueError("no parseable dates in %s" % name)
    if undated:
        notes.append("%d row(s) without a parseable date, counted but not dated" % undated)
    return {"count": count, "start": min(dates), "end": max(dates), "notes": notes,
            "sources": srcs, "files": len(dataset_obj["hasPart"]) if kind == "csvset" else None,
            "identifier": ident, "size": size, "drop_hash": drop_hash, "sub": None}


def fmt_comma(n):
    return "{:,}".format(n)


def sub_once(pattern, repl, text, flags=0):
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    return new, n


def render(template, m, fam):
    """Fill a TEXT template from measured values. None if a needed value is unavailable."""
    ctx = {"count": m["count"], "start": m["start"], "end": m["end"], "files": m["files"], "fam": fam}
    try:
        out = template.format(**ctx)
    except (KeyError, IndexError):
        return None
    if "None" in out:
        return None
    return out


def legacy_sub(desc, m):
    """Tier/extract/artifact/returns counts in the legacy description, from the directory counts."""
    sub = m.get("sub")
    if not sub:
        return desc
    try:
        vals = (sub["agents_tier_defs"], sub["agent_status_extracts"], sub["artifacts"], sub["returns_records"])
    except KeyError:
        return desc
    return re.sub(r"\d+ tier definitions, \d+ filtered execution-bus extracts, \d+ named artifact files, \d+ attributed",
                  "%d tier definitions, %d filtered execution-bus extracts, %d named artifact files, %d attributed" % vals,
                  desc, count=1)


def set_hash_fields(text, m):
    """Replace identifier/contentSize with recomputed values, or remove them (drop_hash)."""
    out = text
    if m.get("drop_hash"):
        out = re.sub(r'^[ \t]*"identifier"\s*:\s*"[^"]*",?[ \t]*\n', "", out, count=1, flags=re.M)
        out = re.sub(r',\s*"contentSize"\s*:\s*"[^"]*"', "", out, count=1)
    elif m.get("identifier"):
        out = re.sub(r'("identifier"\s*:\s*")[^"]*(")', lambda mo: mo.group(1) + m["identifier"] + mo.group(2), out, count=1)
        out = re.sub(r'("contentSize"\s*:\s*")[^"]*(")', lambda mo: mo.group(1) + m["size"] + mo.group(2), out, count=1)
    return out


def patch_dataset_json(text, name, m, field, do_cov, fam):
    """Targeted text edits so nothing else in the file changes."""
    out = text
    out, _ = sub_once(r'("name"\s*:\s*"%s"\s*,\s*"value"\s*:\s*)\d+' % re.escape(field),
                      lambda mo: mo.group(1) + str(m["count"]), out)
    out, _ = sub_once(r'("dateModified"\s*:\s*")[^"]*(")',
                      lambda mo: mo.group(1) + m["end"] + mo.group(2), out)
    out, _ = sub_once(r'("url"\s*:\s*"%s%s)(?!\.json)(")' % (re.escape(BASE_URL), re.escape(name)),
                      lambda mo: mo.group(1) + ".json" + mo.group(2), out)
    if do_cov and m["start"]:
        out, _ = sub_once(r'("temporalCoverage"\s*:\s*")[^"]*(")',
                          lambda mo: mo.group(1) + m["start"] + "/" + m["end"] + mo.group(2), out)
    out = set_hash_fields(out, m)
    t = TEXT.get(name, {})
    if t.get("cadence"):
        new = render(t["cadence"], m, fam)
        if new:
            out, _ = sub_once(r'("name"\s*:\s*"refreshCadence"\s*,\s*"value"\s*:\s*")[^"]*(")',
                              lambda mo: mo.group(1) + new + mo.group(2), out)
    if t.get("file_desc"):
        new = render(t["file_desc"], m, fam)
        if new:
            out, _ = sub_once(r'("description"\s*:\s*")[^"]*(")',
                              lambda mo: mo.group(1) + new + mo.group(2), out)
    if t.get("file_desc_re"):
        pat, tmpl = t["file_desc_re"]
        def dsub(mo):
            d = mo.group(2)
            r = render(tmpl, m, fam)
            if r is not None:
                d = re.sub(pat, r, d, count=1)
            return mo.group(1) + d + mo.group(3)
        out, _ = sub_once(r'("description"\s*:\s*")([^"]*)(")', dsub, out)
    if name == "legacy_agent_corpus_2026":
        out, _ = sub_once(r'("description"\s*:\s*")([^"]*)(")',
                          lambda mo: mo.group(1) + legacy_sub(mo.group(2), m) + mo.group(3), out)
    return out


def fix_description(desc, name, m):
    """Update the leading count token and any date range in a description string."""
    if m["files"] is not None:
        desc = re.sub(r"\b\d+ (sensor )?files\b", lambda mo: "%d %sfiles" % (m["files"], mo.group(1) or ""), desc, count=1)
        desc = re.sub(r"\ball \d+\b", "all %d" % m["files"], desc, count=1)
    desc, _ = re.subn(r"^\d[\d,]*", str(m["count"]), desc, count=1)
    if m["start"]:
        desc, _ = re.subn(r"\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}",
                          m["start"] + "/" + m["end"], desc, count=1)
    return desc


def region_for(text, name, style):
    """(start, end) of the text region that belongs to dataset `name` in an index file."""
    if style == "line":  # croissant: one line per dataset, found by @id
        m = re.search(r'^.*"@id"\s*:\s*"%s".*$' % re.escape(name), text, re.M)
        return (m.start(), m.end()) if m else None
    m = re.search(r'"name"\s*:\s*"%s"' % re.escape(name), text)
    if not m:
        return None
    start = text.rfind('"@type": "Dataset"', 0, m.start())
    if start < 0:
        start = m.start()
    nxt = text.find('"@type": "Dataset"', m.end())
    end = nxt if nxt >= 0 else len(text)
    return start, end


def patch_index_region(region, name, m, do_cov, style, fam):
    r = region
    # urls that omit .json and 404
    r = re.sub(r'(https://api\.receiptsindex\.com/datasets/%s)(?!\.json)(")' % re.escape(name),
               r"\1.json\2", r)
    if style == "block":
        r = set_hash_fields(r, m)
    if do_cov and m["start"]:
        r = re.sub(r'("temporalCoverage"\s*:\s*")[^"]*(")',
                   lambda mo: mo.group(1) + m["start"] + "/" + m["end"] + mo.group(2), r, count=1)
    tmpl = TEXT.get(name, {}).get("cro" if style == "line" else "idx")
    forced = render(tmpl, m, fam) if tmpl else None

    def desc_sub(mo):
        return mo.group(1) + (forced if forced else fix_description(mo.group(2), name, m)) + mo.group(3)
    r = re.sub(r'("description"\s*:\s*")([^"]*)(")', desc_sub, r, count=1)
    return r


def patch_index_text(text, style, measured, do_cov, catalog_date, fam):
    out = text
    for name, m in measured.items():
        reg = region_for(out, name, style)
        if not reg:
            continue
        s, e = reg
        out = out[:s] + patch_index_region(out[s:e], name, m, do_cov, style, fam) + out[e:]
    if catalog_date:
        out = re.sub(r'("dateModified"\s*:\s*")[^"]*(")',
                     lambda mo: mo.group(1) + catalog_date + mo.group(2), out)
    return out


def patch_index_html(text, measured, do_cov, catalog_date, fam, stamp, unmatched):
    out = patch_index_text(text, "block", measured, do_cov, catalog_date, fam)
    for name, m in measured.items():
        pat = re.compile(r"(<tr><td>%s</td><td>)(.*?)(</td><td>)(.*?)(</td><td>)(.*?)(</td>)" % re.escape(name))

        def row_sub(mo):
            cnt = mo.group(2)
            cnt = re.sub(r"\d[\d,]*", fmt_comma(m["count"]), cnt, count=1)
            if m["files"] is not None:
                cnt = re.sub(r"\b\d+ files\b", "%d files" % m["files"], cnt, count=1)
            rng = mo.group(4)
            if do_cov and m["start"]:
                dm = re.match(r"^(\d{4}-\d{2}-\d{2})(?: to (\d{4}-\d{2}-\d{2}))?(.*)$", rng)
                if dm:
                    rest = dm.group(3)
                    if "single" in rest:
                        rest = ""
                    rng = "%s to %s%s" % (m["start"], m["end"], rest)
            cad = mo.group(6)
            cell = TEXT.get(name, {}).get("cell")
            if cell:
                cad = cell
            return mo.group(1) + cnt + mo.group(3) + rng + mo.group(5) + cad + mo.group(7)
        out = pat.sub(row_sub, out, count=1)
    # body paragraphs that carried dated verification claims
    out = re.sub(r"(<p>Ten machine-readable datasets.*?Dated )\d{4}-\d{2}-\d{2}(\.</p>)",
                 lambda mo: mo.group(1) + (catalog_date or "") + mo.group(2), out, count=1, flags=re.S)
    warn = ('<p class="warn"><strong>Read before using the links below:</strong> each link goes to that '
            "dataset's live Dataset JSON-LD metadata page (name, description, row count, hash where listed, "
            "temporal coverage). None of these is the raw CSV or JSONL file itself; the underlying data files "
            "are not yet served at a public URL. To request a data file, email the address in the License "
            "section below.</p>")
    out = re.sub(r'<p class="warn">.*?</p>', lambda mo: warn, out, count=1, flags=re.S)
    keep = ", ".join(sorted(unmatched))
    hashed = sorted(n for n, m in measured.items() if m["identifier"])
    nohash = sorted(n for n, m in measured.items() if m["drop_hash"])
    kept = sorted(n for n, m in measured.items() if not m["identifier"] and not m["drop_hash"]) + sorted(unmatched)
    note = ('<p class="note">Row counts and date ranges on this page were measured from the source files on %s'
            % stamp)
    if keep:
        note += ", except %s, which has no single measurable source and keeps its previously published figures" % keep
    note += (". For %s, the hash is the first 16 hex characters of the SHA-256 of the source file as measured"
             % ", ".join(hashed))
    if kept:
        note += "; %s keep their previously published hashes" % ", ".join(kept)
    if nohash:
        note += "; %s lists no hash" % ", ".join(nohash)
    note += (". The actual data files behind the links are not yet separately downloadable. "
             "Request access via the email below.</p>")
    out = re.sub(r'<p class="note">.*?</p>', lambda mo: note, out, count=1, flags=re.S)
    return out


def read_text(path):
    with open(path, newline="", encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(text)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def run(brain, repo, sources=None, dry_run=False, do_cov=True, ceiling=None, stamp=None):
    sources = SOURCES if sources is None else sources
    now = datetime.datetime.now(datetime.timezone.utc).date()
    ceiling = ceiling or (now + datetime.timedelta(days=1))
    stamp = stamp or now.isoformat()
    ddir = os.path.join(repo, "datasets")
    report = []
    measured = {}
    texts = {}
    specs = {}
    unmatched = []
    # pass 1: measure everything
    for fn in sorted(os.listdir(ddir)):
        if not fn.endswith(".json") or fn in INDEX_FILES:
            continue
        name = fn[:-5]
        path = os.path.join(ddir, fn)
        text = read_text(path)
        obj = json.loads(text)
        old_count = None
        for p in obj.get("additionalProperty", []):
            if p.get("name") in ("rowCount", "fileCount"):
                old_count = p.get("value")
        spec = sources.get(name)
        row = {"name": name, "old_count": old_count, "old_date": obj.get("dateModified"),
               "new_count": None, "new_date": None, "source": None, "status": None, "notes": []}
        if not spec or not spec.get("path"):
            row["status"] = "UNMATCHED"
            row["notes"].append((spec or {}).get("reason", "no entry in SOURCES"))
            unmatched.append(name)
            report.append(row)
            continue
        try:
            m = measure(name, spec, brain, obj, ceiling)
        except (OSError, ValueError) as exc:
            row["status"] = "UNMATCHED"
            row["notes"].append("source unreadable: %s" % exc)
            unmatched.append(name)
            report.append(row)
            continue
        measured[name] = m
        texts[name] = (path, text)
        specs[name] = spec
        row.update(new_count=m["count"], new_date=m["end"],
                   source="; ".join(m["sources"]) if len(m["sources"]) < 3 else
                   "%s/ (%d files)" % (spec["path"], len(m["sources"])))
        row["notes"] += m["notes"]
        if m["identifier"]:
            row["notes"].append("identifier %s contentSize %s" % (m["identifier"], m["size"]))
        if m["drop_hash"]:
            row["notes"].append("identifier and contentSize removed (not recomputable from the Brain)")
        row["_name"] = name
        report.append(row)
    fam = measured.get("planetary_sensor_family", {}).get("files")
    # pass 2: patch
    for row in report:
        name = row.get("_name")
        if not name:
            continue
        path, text = texts[name]
        new = patch_dataset_json(text, name, measured[name], specs[name]["count_field"], do_cov, fam)
        json.loads(new)
        row["status"] = "CHANGED" if new != text else "OK"
        if new != text and not dry_run:
            write_text(path, new)
    catalog_date = max((m["end"] for m in measured.values()), default=None)
    for fn, style in (("index.json", "block"), ("croissant.json", "line"), ("index.html", "html")):
        p = os.path.join(ddir, fn)
        if not os.path.exists(p):
            continue
        t = read_text(p)
        if style == "html":
            nt = patch_index_html(t, measured, do_cov, catalog_date, fam, stamp, unmatched)
        else:
            nt = patch_index_text(t, style, measured, do_cov, catalog_date, fam)
            json.loads(nt)
        if nt != t and not dry_run:
            write_text(p, nt)
    return report, measured, catalog_date


def print_report(report, catalog_date):
    print("name\told_count\tnew_count\told_date\tnew_date\tstatus\tsource")
    for r in report:
        print("\t".join(str(x) for x in (r["name"], r["old_count"], r["new_count"], r["old_date"],
                                          r["new_date"], r["status"], r["source"])))
        for n in r["notes"]:
            print("  note: %s: %s" % (r["name"], n))
    um = [r["name"] for r in report if r["status"] == "UNMATCHED"]
    print("UNMATCHED: %s" % (", ".join(um) if um else "none"))
    print("catalog dateModified: %s" % catalog_date)


def selftest():
    results = []

    def check(label, cond):
        results.append(bool(cond))
        print("%s %s" % ("PASS" if cond else "FAIL", label))

    with tempfile.TemporaryDirectory() as tmp:
        brain = os.path.join(tmp, "brain")
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(brain, "sensors"))
        os.makedirs(os.path.join(brain, "status"))
        os.makedirs(os.path.join(brain, "arch", "sub"))
        os.makedirs(os.path.join(repo, "datasets"))
        # CSV with comment lines (one with a stray quote), blank line, empty-cells row, timestamps
        write_text(os.path.join(brain, "sensors", "a.csv"),
                   '# comment with "a stray quote\n# second comment\n'
                   "date,v\n2026-01-01,1\n\n,\n2026-01-05T10:00:00Z,2\n"
                   '2026-01-03,"multi\n# not a comment\nline"\nnot-a-date,9\n')
        write_text(os.path.join(brain, "status", "b.jsonl"),
                   '{"ts":"2026-02-01T00:00:00Z","x":1}\n\n{"ts":"2026-02-09T00:00:00Z"}\nnot json\n')
        write_text(os.path.join(brain, "arch", "MANIFEST.md"), "Compiled 2026-03-04T00:00:00Z here\n")
        write_text(os.path.join(brain, "arch", "f1.md"), "x")
        write_text(os.path.join(brain, "arch", "sub", "f2.md"), "y")
        # file mtime deliberately set far in the future: must not matter
        os.utime(os.path.join(brain, "sensors", "a.csv"), (4000000000, 4000000000))
        srcs = {
            "alpha": {"kind": "csv", "path": "sensors/a.csv", "date_col": "date", "count_field": "rowCount"},
            "bravo": {"kind": "jsonl", "path": "status/b.jsonl", "date_col": "ts", "count_field": "rowCount"},
            "charlie": {"kind": "filetree", "path": "arch", "exclude": ["MANIFEST.md"],
                        "date_file": "MANIFEST.md", "date_regex": r"Compiled (\d{4}-\d{2}-\d{2})",
                        "count_field": "fileCount"},
            "delta": {"kind": None, "path": None, "reason": "no source"},
        }

        def ds(name, count, date, field="rowCount", cov="2020-01-01/2020-01-02"):
            return ('{\n  "name": "%s",\n  "url": "%s%s",\n  "temporalCoverage": "%s",\n'
                    '  "dateModified": "%s",\n  "keep": "untouched",\n  "additionalProperty": [\n'
                    '    { "@type": "PropertyValue", "name": "%s", "value": %d },\n'
                    '    { "@type": "PropertyValue", "name": "other", "value": 77 }\n  ]\n}\n'
                    % (name, BASE_URL, name, cov, date, field, count))
        write_text(os.path.join(repo, "datasets", "alpha.json"), ds("alpha", 999, "2020-01-02"))
        write_text(os.path.join(repo, "datasets", "bravo.json"), ds("bravo", 1, "2020-01-02"))
        write_text(os.path.join(repo, "datasets", "charlie.json"), ds("charlie", 5, "2020-01-02", "fileCount"))
        delta_text = ds("delta", 12345, "2019-05-05")
        write_text(os.path.join(repo, "datasets", "delta.json"), delta_text)
        write_text(os.path.join(repo, "datasets", "index.html"),
                   "<table>\n<tr><td>alpha</td><td>1,500</td><td>2020-01-01 to 2020-01-02</td><td>x</td></tr>\n"
                   "<tr><td>delta</td><td>12,345</td><td>2019-01-01 to 2019-05-05</td><td>x</td></tr>\n</table>\n")
        # fixtures: outdated free text, recomputable hash, non-recomputable hash
        old_word = "st" + "ale"
        old_cad = " ".join(["intended", "daily", "--", "currently", "3", "captures", "across", "16", "days,", "8", "days", old_word])
        old_feed = " ".join(["feeds", "world_state", "daily", "--", "same", "8" + "-day", old_word + "ness"])
        write_text(os.path.join(brain, "sensors", "f.csv"), "# c\ndate,x\n2026-04-01,1\n2026-04-09,2\n")
        write_text(os.path.join(brain, "sensors", "p1.csv"), "captured_utc,x\n2026-05-01T00:00:00Z,1\n")
        srcs["filing_room_ledger"] = {"kind": "csv", "path": "sensors/f.csv", "date_col": "date", "count_field": "rowCount"}
        srcs["planetary_sensor_family"] = {"kind": "csvset", "path": "sensors", "date_col": "captured_utc", "count_field": "rowCount"}
        write_text(os.path.join(repo, "datasets", "filing_room_ledger.json"),
                   '{\n  "name": "filing_room_ledger",\n  "url": "%sfiling_room_ledger",\n  "temporalCoverage": "a/b",\n'
                   '  "dateModified": "2020-01-01",\n  "identifier": "sha256:0000000000000000",\n'
                   '  "distribution": { "@type": "DataDownload", "encodingFormat": "text/csv", "contentSize": "1" },\n'
                   '  "additionalProperty": [\n    { "@type": "PropertyValue", "name": "rowCount", "value": 3 },\n'
                   '    { "@type": "PropertyValue", "name": "refreshCadence", "value": "OLDCAD" }\n  ]\n}\n' % BASE_URL)
        write_text(os.path.join(repo, "datasets", "planetary_sensor_family.json"),
                   '{\n  "name": "planetary_sensor_family",\n  "url": "%splanetary_sensor_family",\n  "temporalCoverage": "a/b",\n'
                   '  "dateModified": "2020-01-01",\n  "identifier": "sha256-of-hashes:0000",\n  "hasPart": ["p1.csv"],\n'
                   '  "distribution": { "@type": "DataDownload", "encodingFormat": "text/csv", "contentSize": "258334" },\n'
                   '  "additionalProperty": [\n    { "@type": "PropertyValue", "name": "rowCount", "value": 5 },\n'
                   '    { "@type": "PropertyValue", "name": "refreshCadence", "value": "OLDFEED" }\n  ]\n}\n' % BASE_URL)
        for fn_, tok, val in (("filing_room_ledger.json", "OLDCAD", old_cad), ("planetary_sensor_family.json", "OLDFEED", old_feed)):
            fp_ = os.path.join(repo, "datasets", fn_)
            write_text(fp_, read_text(fp_).replace(tok, val))
        ceiling = datetime.date(2030, 1, 1)
        report, measured, cat = run(brain, repo, sources=srcs, ceiling=ceiling)
        a = json.loads(read_text(os.path.join(repo, "datasets", "alpha.json")))
        b = json.loads(read_text(os.path.join(repo, "datasets", "bravo.json")))
        c = json.loads(read_text(os.path.join(repo, "datasets", "charlie.json")))
        check("csv count measured from content (comments, blanks, empty-cell row, header excluded; multiline field = 1 row): 4", a["additionalProperty"][0]["value"] == 4)
        check("csv newest date from date column, not mtime: 2026-01-05", a["dateModified"] == "2026-01-05")
        check("wrong stated count (999) corrected", a["additionalProperty"][0]["value"] != 999)
        check("url fixed to own .json URL", a["url"] == BASE_URL + "alpha.json")
        check("jsonl count measured: 2 valid objects", b["additionalProperty"][0]["value"] == 2)
        check("jsonl newest date from ts: 2026-02-09", b["dateModified"] == "2026-02-09")
        check("filetree count excludes manifest, recurses: 2", c["additionalProperty"][0]["value"] == 2)
        check("filetree date read from manifest content: 2026-03-04", c["dateModified"] == "2026-03-04")
        check("nothing else changed (keep field and other property intact)",
              a["keep"] == "untouched" and a["additionalProperty"][1]["value"] == 77)
        check("unmatched dataset left byte-identical",
              read_text(os.path.join(repo, "datasets", "delta.json")) == delta_text)
        check("unmatched dataset listed UNMATCHED", [r["name"] for r in report if r["status"] == "UNMATCHED"] == ["delta"])
        html = read_text(os.path.join(repo, "datasets", "index.html"))
        check("index page row for matched dataset shows measured count and range",
              "<td>alpha</td><td>4</td><td>2026-01-01 to 2026-01-05</td>" in html)
        check("index page row for unmatched dataset untouched", "<td>delta</td><td>12,345</td><td>2019-01-01 to 2019-05-05</td>" in html)
        ft = read_text(os.path.join(repo, "datasets", "filing_room_ledger.json"))
        fo = json.loads(ft)
        raw = open(os.path.join(brain, "sensors", "f.csv"), "rb").read()
        check("outdated cadence text rewritten from measured values only",
              fo["additionalProperty"][1]["value"] == "intended daily; 2 captures from 2026-04-01 to 2026-04-09" and old_word not in ft)
        check("identifier recomputed from the source file bytes",
              fo["identifier"] == "sha256:" + hashlib.sha256(raw).hexdigest()[:16])
        check("contentSize recomputed as source byte size", fo["distribution"]["contentSize"] == str(len(raw)))
        pt = read_text(os.path.join(repo, "datasets", "planetary_sensor_family.json"))
        po = json.loads(pt)
        check("non-recomputable identifier and contentSize removed, JSON still valid, outdated text gone",
              "identifier" not in po and "contentSize" not in po["distribution"] and old_word not in pt
              and po["additionalProperty"][1]["value"] == "feeds world_state daily")
        snap = {fn: read_text(os.path.join(repo, "datasets", fn)) for fn in os.listdir(os.path.join(repo, "datasets"))}
        run(brain, repo, sources=srcs, ceiling=ceiling)
        snap2 = {fn: read_text(os.path.join(repo, "datasets", fn)) for fn in os.listdir(os.path.join(repo, "datasets"))}
        check("second run is a no-op (idempotent)", snap == snap2)
    n = len(results)
    p = sum(results)
    print("SELFTEST %d/%d PASS" % (p, n))
    return p == n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--brain")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-coverage", action="store_true",
                    help="leave temporalCoverage and index date ranges alone")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if not a.brain:
        ap.error("--brain is required")
    report, _m, cat = run(a.brain, a.repo, dry_run=a.dry_run, do_cov=not a.no_coverage)
    print_report(report, cat)


if __name__ == "__main__":
    main()
