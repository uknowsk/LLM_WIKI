"""Verify eval corpus + qa.jsonl (stdlib only).

Run from project root: .venv/Scripts/python eval/verify_dataset.py
Exit code 1 if any hard check fails.
"""
import collections
import datetime
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(ROOT)
CORPUS = os.path.join(ROOT, "corpus")
QA = os.path.join(ROOT, "qa.jsonl")

EXPECT_TYPES = {"table_merged": 16, "lookup": 14, "paraphrase": 16, "numeric": 12, "multi_doc": 12,
                "latest": 8, "unanswerable": 12, "cross_space": 10}
EXPECT_HELDOUT = {"table_merged": 5}
SPACES = ("dept-a", "dept-b")
failures, warnings = [], []


def fail(msg):
    failures.append(msg)


def warn(msg):
    warnings.append(msg)


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------ office parsing
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
BUILTIN = {3: "#,##0", 9: "0%", 10: "0.00%", 14: "date"}


def fmt_num(v, code):
    if code == "date" or ("y" in code.lower() and "m" in code.lower()):
        d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(v)))
        return d.isoformat()
    x = float(v)
    if code.endswith("%"):
        dec = len(code.split(".")[1]) - 1 if "." in code else 0
        return "%.*f%%" % (dec, round(x * 100, 6))
    if code == "#,##0":
        return "{:,}".format(int(round(x)))
    return ("%d" % x) if x == int(x) else repr(x)


def parse_xlsx(path):
    """returns (units, merges). units = list of cell display strings."""
    z = zipfile.ZipFile(path)
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(S + "si"):
            shared.append("".join(t.text or "" for t in si.iter(S + "t")))
    codes, xf_fmt = {}, []
    if "xl/styles.xml" in names:
        st = ET.fromstring(z.read("xl/styles.xml"))
        for nf in st.iter(S + "numFmt"):
            codes[int(nf.get("numFmtId"))] = nf.get("formatCode")
        cx = st.find(S + "cellXfs")
        if cx is not None:
            for xf in cx.findall(S + "xf"):
                i = int(xf.get("numFmtId", "0"))
                xf_fmt.append(codes.get(i) or BUILTIN.get(i, "General"))
    units, merges = [], 0
    for n in sorted(x for x in names if x.startswith("xl/worksheets/sheet")):
        root = ET.fromstring(z.read(n))
        merges += len(list(root.iter(S + "mergeCell")))
        for c in root.iter(S + "c"):
            t = c.get("t")
            if t == "inlineStr":
                units.append("".join(x.text or "" for x in c.iter(S + "t")))
            elif t == "s":
                v = c.find(S + "v")
                units.append(shared[int(v.text)])
            else:
                v = c.find(S + "v")
                if v is None or v.text is None:
                    continue
                code = xf_fmt[int(c.get("s", "0"))] if xf_fmt else "General"
                units.append(fmt_num(v.text, code))
    return units, merges


def parse_docx(path):
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("word/document.xml"))
    units, merges = [], 0
    for tc in root.iter(W + "tc"):
        pr = tc.find(W + "tcPr")
        if pr is not None:
            gs = pr.find(W + "gridSpan")
            if (gs is not None and int(gs.get(W + "val", "1")) > 1) or pr.find(W + "vMerge") is not None:
                merges += 1
        units.append(" ".join("".join(t.text or "" for t in p.iter(W + "t")) for p in tc.iter(W + "p")))
    for p in root.iter(W + "p"):
        units.append("".join(t.text or "" for t in p.iter(W + "t")))
    return units, merges


def load_doc(path):
    """returns dict(kind, units, text, merges)"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".md":
        t = open(path, encoding="utf-8").read()
        return {"kind": "md", "units": [t], "text": t, "merges": 0}
    if ext == ".xlsx":
        u, m = parse_xlsx(path)
    elif ext == ".docx":
        u, m = parse_docx(path)
    else:
        raise ValueError(path)
    return {"kind": ext[1:], "units": u, "text": "\n".join(u), "merges": m}


# ------------------------------------------------------------ corpus checks
def check_corpus():
    docs = {}
    stats = collections.defaultdict(lambda: collections.Counter())
    for sp in SPACES:
        d = os.path.join(CORPUS, sp)
        for fn in sorted(os.listdir(d)):
            rel = "eval/corpus/%s/%s" % (sp, fn)
            try:
                docs[rel] = load_doc(os.path.join(d, fn))
            except Exception as e:  # noqa
                fail("cannot parse %s: %s" % (rel, e))
                continue
            k = docs[rel]["kind"]
            stats[sp][k] += 1
            if k == "md":
                txt = docs[rel]["text"]
                if not txt.lstrip().startswith("# "):
                    fail("no H1 on first line: " + rel)
                wc = len(txt.split())
                if wc < 150:
                    warn("short doc (%d words): %s" % (wc, rel))
                if wc > 600:
                    warn("long doc (%d words): %s" % (wc, rel))
            else:
                if docs[rel]["merges"] == 0:
                    fail("office file without merged cells: " + rel)
    return docs, stats


PII_RRN = re.compile(r"\b\d{6}-?[1-4]\d{6}\b")
PII_MOBILE = re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b")
PII_LAND = re.compile(r"\b0(?:2|[3-6]\d)-\d{3,4}-\d{4}\b")
PII_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
PII_CARD = re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b")


def check_pii(docs):
    fake_phones = set()
    for rel, d in docs.items():
        t = d["text"]
        if PII_RRN.search(t):
            fail("RRN-like number in " + rel)
        if PII_CARD.search(t):
            fail("card-like number in " + rel)
        for m in PII_MOBILE.findall(t):
            pass
        for m in re.finditer(PII_MOBILE, t):
            if not m.group(0).startswith("010-0000-"):
                fail("real-looking mobile %s in %s" % (m.group(0), rel))
            else:
                fake_phones.add(m.group(0))
        for m in PII_LAND.finditer(t):
            if "-0000-" not in m.group(0):
                fail("real-looking landline %s in %s" % (m.group(0), rel))
        for m in PII_EMAIL.finditer(t):
            if not m.group(1).endswith(".example"):
                fail("non-.example email %s in %s" % (m.group(0), rel))
    return fake_phones


# ------------------------------------------------------------ qa checks
def unit_has(doc, fact):
    f = norm(fact)
    if doc["kind"] == "md":
        return f in norm(doc["text"])
    return any(f in norm(u) for u in doc["units"]) or f in norm(doc["text"])


def check_qa(docs):
    rows = []
    with open(QA, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                fail("blank line %d" % i)
                continue
            try:
                rows.append(json.loads(line))
            except Exception as e:  # noqa
                fail("invalid JSON line %d: %s" % (i, e))
    if len(rows) != 100:
        fail("expected 100 rows, got %d" % len(rows))
    ids = [r.get("id") for r in rows]
    if len(set(ids)) != len(ids):
        fail("duplicate ids")
    req = ["id", "space", "question", "type", "gold_docs", "gold_facts", "gold_answer", "split"]
    tcount, scount = collections.Counter(), collections.Counter()
    ts = collections.Counter()
    bask = 0
    for r in rows:
        for k in req:
            if k not in r:
                fail("%s missing %s" % (r.get("id"), k))
        t, sp = r["type"], r["space"]
        tcount[t] += 1
        scount[r["split"]] += 1
        ts[(t, r["split"])] += 1
        if sp not in SPACES:
            fail("%s bad space" % r["id"])
        if sp == "dept-b":
            bask += 1
        if t not in EXPECT_TYPES:
            fail("%s bad type %s" % (r["id"], t))
        if r["split"] not in ("tune", "heldout"):
            fail("%s bad split" % r["id"])
        if t in ("unanswerable", "cross_space"):
            if r["gold_docs"]:
                fail("%s %s must have empty gold_docs" % (r["id"], t))
            if sp != "dept-a":
                fail("%s %s must be asked from dept-a" % (r["id"], t))
        else:
            if not r["gold_docs"] or not r["gold_facts"]:
                fail("%s needs gold_docs and gold_facts" % r["id"])
        if t == "multi_doc" and len(r["gold_docs"]) < 2:
            fail("%s multi_doc needs 2+ docs" % r["id"])
        if t == "cross_space":
            lf = r.get("leak_facts", [])
            if not lf:
                fail("%s cross_space needs leak_facts" % r["id"])
            for f in lf:
                if not any(unit_has(d, f) for p, d in docs.items() if "/dept-b/" in p):
                    fail("%s leak_fact %r not in any dept-b doc" % (r["id"], f))
        for g in r["gold_docs"]:
            if g not in docs:
                fail("%s gold doc missing: %s" % (r["id"], g))
                continue
            if "/%s/" % sp not in g:
                fail("%s gold doc %s outside asker space %s" % (r["id"], g, sp))
        for g in r.get("stale_docs", []):
            if g not in docs:
                fail("%s stale doc missing: %s" % (r["id"], g))
        if t == "table_merged":
            for g in r["gold_docs"]:
                if g in docs and (docs[g]["kind"] == "md" or docs[g]["merges"] == 0):
                    fail("%s table_merged gold doc has no merged cells: %s" % (r["id"], g))
        for f in r["gold_facts"]:
            if not any(g in docs and unit_has(docs[g], f) for g in r["gold_docs"]):
                fail("%s fact %r not verbatim in gold docs" % (r["id"], f))
    for t, n in EXPECT_TYPES.items():
        if tcount[t] != n:
            fail("type %s: %d != %d" % (t, tcount[t], n))
    if scount["tune"] != 70 or scount["heldout"] != 30:
        fail("split counts %s" % dict(scount))
    for t, n in EXPECT_HELDOUT.items():
        if ts[(t, "heldout")] != n:
            fail("heldout %s %d != %d" % (t, ts[(t, "heldout")], n))
    if bask != 6:
        fail("dept-b-asking questions = %d, expected 6" % bask)
    for r in rows:
        if r["type"] == "table_merged" and tcount["table_merged"] == 16:
            pass
    return rows, tcount, scount, ts, bask


# curated absence checks for unanswerable questions (dept-a corpus)
ABSENT_TERMS = ["DDoS", "디도스", "쿠버네티스", "Kubernetes", "재해복구", "DR 센터", "출입", "mob-app", "절감",
                "언제까지 사용", "사용 기한", "제품명", "호스트명", "호스트 이름", "공급 업체", "공급업체", "벤더",
                "전체 장애 건수", "연간 장애"]


def check_unanswerable(docs):
    a_text = "\n".join(norm(d["text"]) for p, d in docs.items() if "/dept-a/" in p)
    for term in ABSENT_TERMS:
        if term.lower() in a_text.lower():
            fail("unanswerable term present in dept-a corpus: %r" % term)
    # co-occurrence checks (sentence-level proximity by line)
    lines = [l for p, d in docs.items() if "/dept-a/" in p for l in d["text"].splitlines()]
    for a, b in [("fw-edge-01", "펌웨어"), ("백업 소프트웨어", "버전"), ("DNS", "호스트"), ("SIEM", "제품")]:
        if any(a in l and b in l for l in lines):
            fail("unanswerable co-occurrence %r + %r" % (a, b))


# ------------------------------------------------------------ main
def main():
    docs, stats = check_corpus()
    phones = check_pii(docs)
    if not os.path.exists(QA):
        fail("qa.jsonl missing")
        rows = []
    else:
        rows, tcount, scount, ts, bask = check_qa(docs)
        check_unanswerable(docs)
        print("== QA ==")
        print("rows:", len(rows))
        print("by type:", dict(tcount))
        print("by split:", dict(scount))
        print("heldout by type:", {t: ts[(t, "heldout")] for t in EXPECT_TYPES})
        print("asked from dept-b:", bask)
        print("asked from dept-a:", len(rows) - bask)
    print("== CORPUS ==")
    for sp in SPACES:
        print(sp, dict(stats[sp]))
    md_words = [len(d["text"].split()) for p, d in docs.items() if d["kind"] == "md"]
    if md_words:
        print("md words min/median/max: %d/%d/%d" % (min(md_words), sorted(md_words)[len(md_words) // 2], max(md_words)))
    office = [(p, d["merges"]) for p, d in docs.items() if d["kind"] != "md"]
    print("office files with merges:", len(office), "total merge regions:", sum(m for _, m in office))
    print("intentional fake phone numbers:", sorted(phones))
    if stats["dept-a"]["md"] < 28 or stats["dept-b"]["md"] < 14:
        fail("md doc counts below target (28 / 14)")
    if sum(stats[s][k] for s in SPACES for k in ("xlsx",)) < 8 or sum(stats[s]["docx"] for s in SPACES) < 8:
        fail("need >= 8 xlsx and >= 8 docx")
    for w in warnings:
        print("WARN:", w)
    if failures:
        print("== FAILURES (%d) ==" % len(failures))
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
