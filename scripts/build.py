#!/usr/bin/env python3
"""Rebuild the injectable drug-device combination trial list.

Queries ClinicalTrials.gov (API v2) and the EU Clinical Trials Information
System (CTIS public API), keeps ongoing Phase 2/3 parenteral trials whose
record names a delivery device, applies the manual decisions in
data/curation.json, joins revenue figures from data/revenue.json and writes:

  data/trials.json     the full dataset
  data/build-log.txt   counts and any errors from this run
  index.html           the published page (template: site/template.html)

Run locally or from the GitHub Actions workflow:  python scripts/build.py
"""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = {"User-Agent": "injectable-combination-trials/1.0 (+https://github.com/sushilkk81/Injectable-Combination-Trials)"}
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


# --------------------------------------------------------------------------
# ClinicalTrials.gov
# --------------------------------------------------------------------------
CTG = "https://clinicaltrials.gov/api/v2/studies"
CTG_STATUS = "RECRUITING|ACTIVE_NOT_RECRUITING|NOT_YET_RECRUITING|ENROLLING_BY_INVITATION"
CTG_PHASE = "AREA[Phase](PHASE2 OR PHASE3)"
FIELDS = ("NCTId,BriefTitle,OfficialTitle,Phase,OverallStatus,LeadSponsorName,LeadSponsorClass,Condition,"
          "InterventionType,InterventionName,InterventionDescription,ArmGroupDescription,PrimaryCompletionDate,"
          "StartDate,EnrollmentCount,LocationCountry,BriefSummary,OrgStudyId,SecondaryId")
DEV = ('(autoinjector OR "auto-injector" OR "auto injector" OR "prefilled pen" OR "pre-filled pen" OR "pen injector" '
       'OR "injection pen" OR "prefilled syringe" OR "pre-filled syringe" OR "on-body injector" OR "on-body delivery system" '
       'OR "wearable injector" OR "needle-free" OR "dual chamber" OR "safety syringe" OR "injection device" OR "patch pump" '
       'OR "large volume injector" OR "infusor" OR "implantable pump")')
BROAD = ('(pen OR pens OR syringe OR syringes OR injector OR autoinjector OR "auto-injector" OR pump OR wearable OR "on-body" '
         'OR cartridge OR microneedle OR "needle-free" OR "delivery system" OR "delivery device" OR applicator OR APFS OR "dual chamber")')

CAT = [
    ("Autoinjector", r"auto[- ]?injectors?\b"),
    ("Pen injector", r"pre[- ]?filled[- ]pens?\b|pen[- ]injectors?\b|injection pens?\b|injector pens?\b|(single|multi|multiple|fixed)[- ](dose|use)[- ]pens?\b|disposable pens?\b|\bpen devices?\b|insulin pens?\b|FlexPen|FlexTouch|KwikPen|SoloStar|\bpens?\b(?=[^.]{0,40}(inject|subcutan))|(inject|subcutan)[^.]{0,40}\bpens?\b"),
    ("Prefilled syringe", r"pre[- ]?filled[- ](safety[- ]|glass[- ])?syringes?\b|syringes? pre[- ]?filled|\bAPFS\b|safety syringes?\b|needle safety device|syringe[^.]{0,30}needle guard"),
    ("On-body / wearable injector", r"on[- ]body[- ](injector|deliver|device|infus|system|pump|subcutaneous)|wearable (injector|infus|device|drug|pump)|patch pumps?\b|\bOBDS\b|large[- ]volume (injector|delivery)"),
    ("Needle-free injector", r"needle[- ]?free (inject|device|jet|system|deliver)|jet[- ]inject"),
    ("Dual-chamber system", r"dual[- ]chamber"),
    ("Microneedle", r"micro[- ]?needle (patch|array|device|injector)|microarray patch|hollow micro[- ]?needle"),
    ("Infusion pump system", r"(insulin|implantable|intrathecal|portable|subcutaneous infusion|subcutaneous)[- ]pumps?\b|pump[- ]delivered|\bCSII\b|prefilled cartridge"),
    ("Drug-device combination (device type not stated)", r"drug[- ]device|device[- ]drug|combination product consisting|delivery (device|system) version|cartridge co-packaged"),
]
CAT = [(n, re.compile(p, re.I)) for n, p in CAT]
COMBODEV = re.compile(r"pre[- ]?filled|cartridge|delivery (system|device)|injector", re.I)
PAR = re.compile(r"inject|subcutaneous|intravenous|intramuscular|infusion|syringe|intravitreal|intrathecal|intradermal|suprachoroidal|\bSC\b|\bIV\b|\bIM\b|\bs\.c\.|implant|intra-?articular|parenteral|depot|intratumoral|intracameral|epidural", re.I)
DRUG_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "GENETIC"}
PH_MAP = {"PHASE1": "1", "PHASE2": "2", "PHASE3": "3", "PHASE4": "4", "EARLY_PHASE1": "E1"}
ST_MAP = {"RECRUITING": "Recruiting", "ACTIVE_NOT_RECRUITING": "Active, not recruiting",
          "NOT_YET_RECRUITING": "Not yet recruiting", "ENROLLING_BY_INVITATION": "Enrolling by invitation"}


def http(method, url, retries=4, **kw):
    kw.setdefault("timeout", 90)
    headers = dict(UA)
    headers.update(kw.pop("headers", {}))
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers=headers, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # network hiccup
            last = str(e)
        time.sleep(2 + attempt * 3)
    raise RuntimeError(f"{method} {url} failed: {last}")


def ctg_search(params):
    out, token = [], None
    while True:
        p = dict(params)
        p.update({"filter.overallStatus": CTG_STATUS, "pageSize": 1000, "fields": FIELDS, "format": "json"})
        p["filter.advanced"] = CTG_PHASE + (" AND " + params["filter.advanced"] if "filter.advanced" in params else "")
        if token:
            p["pageToken"] = token
        j = http("GET", CTG, params=p).json()
        out += j.get("studies", [])
        token = j.get("nextPageToken")
        if not token:
            return out


def ctg_by_ids(ids):
    out = []
    ids = sorted(ids)
    for i in range(0, len(ids), 100):
        p = {"filter.ids": ",".join(ids[i:i + 100]), "pageSize": 100, "fields": FIELDS, "format": "json"}
        out += http("GET", CTG, params=p).json().get("studies", [])
    return out


def clip(x, n):
    x = re.sub(r"\s+", " ", (x or "").replace('"', "'").replace("\\", "'")).strip()
    return x if len(x) <= n else x[: n - 1] + "…"


def snip(text, rx):
    m = rx.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", text[max(0, m.start() - 55): m.end() + 55]).strip()


def ps(s):
    return s["protocolSection"]


def ctg_fields(s):
    p = ps(s)
    im = p.get("armsInterventionsModule", {})
    ints = im.get("interventions", [])
    sp = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
    st = p.get("statusModule", {})
    drugs = []
    for i in ints:
        if i.get("type") in DRUG_TYPES and not re.search(r"placebo|saline|sham|vehicle", i.get("name", ""), re.I):
            n = i["name"].strip()
            if n not in drugs:
                drugs.append(n)
    countries = []
    for loc in p.get("contactsLocationsModule", {}).get("locations", []):
        c = loc.get("country")
        if c and c not in countries:
            countries.append(c)
    return {
        "nct": p["identificationModule"]["nctId"],
        "title": clip(p["identificationModule"].get("briefTitle"), 140),
        "phase": "/".join(PH_MAP.get(x, x) for x in p.get("designModule", {}).get("phases", [])),
        "status": ST_MAP.get(st.get("overallStatus"), (st.get("overallStatus") or "").replace("_", " ").title()),
        "sponsor": clip(sp.get("name"), 60),
        "stype": "Industry" if sp.get("class") == "INDUSTRY" else "Non-industry",
        "drugs": clip("; ".join(drugs), 110),
        "cond": clip("; ".join(p.get("conditionsModule", {}).get("conditions", [])[:2]), 80),
        "n": p.get("designModule", {}).get("enrollmentInfo", {}).get("count"),
        "start": (st.get("startDateStruct", {}).get("date") or "")[:7],
        "pcd": (st.get("primaryCompletionDateStruct", {}).get("date") or "")[:7],
        "countries": countries,
        "orgId": p["identificationModule"].get("orgStudyInfo", {}).get("id", ""),
    }


def classify_ctg(s):
    p = ps(s)
    im = p.get("armsInterventionsModule", {})
    ints = im.get("interventions", [])
    types = {i.get("type") for i in ints}
    if not types & DRUG_TYPES:
        return None
    ident = p["identificationModule"]
    int_txt = " \n ".join(i.get("name", "") + ". " + (i.get("description") or "") for i in ints)
    strong = ". \n ".join([ident.get("briefTitle", ""), ident.get("officialTitle") or "", int_txt]
                          + [a.get("description") or "" for a in im.get("armGroups", [])])
    weak = p.get("descriptionModule", {}).get("briefSummary") or ""
    cats, snips, basis = [], [], None
    for n, rx in CAT:
        a = snip(strong, rx)
        if a:
            cats.append(n); snips.append(a)
    if cats:
        basis = "A"
    else:
        for n, rx in CAT:
            a = snip(weak, rx)
            if a:
                cats.append(n); snips.append(a)
        if cats:
            basis = "B"
    combo = [i for i in ints if i.get("type") == "COMBINATION_PRODUCT"]
    combo_txt = " \n ".join(i.get("name", "") + " " + (i.get("description") or "") for i in combo)
    if not basis and combo and COMBODEV.search(combo_txt):
        basis, cats, snips = "C", ["Other delivery device"], [snip(combo_txt, COMBODEV)]
    if not basis or not PAR.search(strong + " " + weak):
        return None
    dd = "Drug-device combination (device type not stated)"
    if len(cats) > 1 and dd in cats:
        k = cats.index(dd); cats.pop(k); snips.pop(k)
    return {"cats": cats, "basis": basis, "ev": clip(snips[0], 160)}


def run_ctg():
    queries = {
        "intr": {"query.intr": DEV}, "titles": {"query.titles": DEV}, "term": {"query.term": DEV},
        "combo": {"filter.advanced": "AREA[InterventionType]COMBINATION_PRODUCT"},
        "b_intr": {"query.intr": BROAD}, "b_titles": {"query.titles": BROAD}, "b_term": {"query.term": BROAD},
    }
    allst = {}
    for k, q in queries.items():
        res = ctg_search(q)
        log(f"CT.gov query {k}: {len(res)} studies")
        for s in res:
            allst.setdefault(ps(s)["identificationModule"]["nctId"], s)
    log(f"CT.gov unique studies screened: {len(allst)}")
    rows = {}
    for nct, s in allst.items():
        c = classify_ctg(s)
        if c:
            r = ctg_fields(s); r.update(c); r["src"] = "CT"
            rows[nct] = r
    log(f"CT.gov studies naming a device for a parenteral drug: {len(rows)}")
    return rows


# --------------------------------------------------------------------------
# EU CTIS
# --------------------------------------------------------------------------
CTIS = "https://euclinicaltrials.eu/ctis-public-api"
CTIS_TERMS = ["autoinjector", "auto-injector", "pre-filled pen", "prefilled pen", "pre-filled syringe", "prefilled syringe",
              "pen injector", "on-body", "injection device", "cartridge", "needle-free", "wearable injector", "dual chamber", "implant"]
CTIS_ONGOING = {2: "Not yet recruiting", 3: "Recruiting", 4: "Recruiting", 5: "Active, not recruiting"}
CTIS_PHASE = [("Phase II and Phase III", "2/3"), ("Phase I and Phase II", "1/2"), ("(Phase III)", "3"), ("(Phase II)", "2")]
EU_PAR = re.compile(r"SUBCUTANEOUS|INTRAVENOUS|INTRAMUSCULAR|INTRAVITREAL|INTRADERMAL|INTRATHECAL|INTRA-?ARTICULAR|INTRALESIONAL|INTRACAMERAL|SUPRACHOROIDAL|INTRACEREBRO|INTRATUMORAL|EPIDURAL|INTRACOCHLEAR|SUBRETINAL|INTRACAVERNOUS|INTRAOCULAR|INTRAPERITONEAL", re.I)
EU_CAT = [
    ("Autoinjector", r"auto-?injector|autoinjector|ypsomate|sureclick|clickject|molly|\bAI\b|sensoready|smartject"),
    ("Pen injector", r"pre-?filled pen|\bpen\b|flextouch|kwikpen|solostar|flexpen"),
    ("On-body / wearable injector", r"on-?body|wearable|OBDS|enfuse|patch pump|large.volume"),
    ("Needle-free injector", r"needle-?free|jet inject"),
    ("Microneedle", r"micro-?needle"),
    ("Infusion pump system", r"\bpump\b"),
    ("Prefilled syringe", r"pre-?filled syringe|\bPFS\b|safety syringe|syringe"),
    ("Cartridge system", r"cartridge"),
    ("Implant with applicator", r"implant"),
]
EU_CAT = [(n, re.compile(p, re.I)) for n, p in EU_CAT]
JSON_H = {"Content-Type": "application/json", "Accept": "application/json"}


def ctis_search(term):
    out, page = [], 1
    while True:
        body = {"pagination": {"page": page, "size": 100}, "sort": {"property": "decisionDate", "direction": "DESC"},
                "searchCriteria": {"containAll": f'"{term}"'}}
        j = http("POST", CTIS + "/search", json=body, headers=JSON_H).json()
        out += j.get("data", [])
        if not j.get("pagination", {}).get("nextPage") or page > 40:
            return out
        page += 1
        time.sleep(0.3)


def classify_ctis(det):
    p1 = det["authorizedApplication"]["authorizedPartI"]
    prods = []
    for g in p1.get("productRoleGroupInfos", []) or []:
        for p in g.get("products", []) or []:
            di = p.get("productDictionaryInfo", {}) or {}
            prods.append({
                "name": p.get("productName") or di.get("prodName") or "",
                "subst": p.get("jsonActiveSubstanceNames") or di.get("activeSubstanceName") or "",
                "form": p.get("pharmaceuticalFormDisplay") or di.get("pharmForm") or "",
                "routes": ",".join(p.get("routes") or []),
                "dev": " | ".join(((d.get("tradeName") or "") + " — " + (d.get("description") or "")) for d in (p.get("devices") or [])),
            })
    par = [p for p in prods if EU_PAR.search(p["routes"]) or re.search(r"inject|infusion", p["form"], re.I)]
    real = [p for p in par if not re.search("placebo", p["name"] + " " + p["subst"], re.I)]
    cats, devp = [], None
    for p in par:
        if re.search("placebo", p["name"] + p["subst"], re.I) and real:
            continue
        t = p["form"] + " " + p["dev"]
        for n, rx in EU_CAT:
            if rx.search(t) and n not in cats:
                if n == "Prefilled syringe" and not re.search(r"pre-?filled|PFS|safety", t, re.I):
                    continue
                if n == "Pen injector" and re.search("pre-?filled syringe", p["form"], re.I) and not re.search("pen", p["dev"], re.I):
                    continue
                cats.append(n)
                devp = devp or p
    if not cats:
        return None
    if cats == ["Implant with applicator"] and not devp["dev"]:
        return None
    drugs = []
    for p in real:
        d = (p["subst"] or p["name"]).lower()
        if d not in drugs:
            drugs.append(d)
    ev = devp["name"] + ": " + devp["form"].capitalize() + (" · Device: " + devp["dev"] if devp["dev"] else "")
    return {"cats": cats, "ev": clip(ev, 160), "drugs": clip("; ".join(drugs), 110)}


def run_ctis():
    hits = {}
    for t in CTIS_TERMS:
        res = ctis_search(t)
        log(f"EU CTIS search '{t}': {len(res)} trials")
        for d in res:
            hits.setdefault(d["ctNumber"], {"d": d, "terms": []})["terms"].append(t)
    cand = []
    for h in hits.values():
        d = h["d"]
        ph = d.get("trialPhase", "")
        if "phase IV" in ph or "Phase IV" in ph:
            continue
        if not any(k in ph for k, _ in CTIS_PHASE):
            continue
        if d.get("ctStatus") not in CTIS_ONGOING:
            continue
        if h["terms"] == ["on-body"]:
            continue
        cand.append(d)
    log(f"EU CTIS ongoing Phase 2/3 candidates: {len(cand)}")
    out = {}
    for d in cand:
        try:
            det = http("GET", f"{CTIS}/retrieve/{d['ctNumber']}", headers={"Accept": "application/json"}).json()
        except RuntimeError as e:
            log(f"EU CTIS retrieve failed {d['ctNumber']}: {e}")
            continue
        time.sleep(0.4)
        c = classify_ctis(det)
        if not c:
            continue
        p1 = det["authorizedApplication"]["authorizedPartI"]
        ids = p1["trialDetails"]["clinicalTrialIdentifiers"]
        sec = json.dumps(ids.get("secondaryIdentifyingNumbers") or {})
        m = re.search(r"NCT\d{8}", sec)
        spon = next((s for s in p1.get("sponsors", []) if s.get("primary")), (p1.get("sponsors") or [{}])[0])
        org = ((spon.get("publicContacts") or [{}])[0].get("organisation") or {}) if spon else {}
        dur = p1["trialDetails"]["trialInformation"].get("trialDuration", {}) or {}
        phase = next(v for k, v in CTIS_PHASE if k in d["trialPhase"])
        out[d["ctNumber"]] = {
            "eu": d["ctNumber"], "nct": m.group(0) if m else "", "code": ids.get("shortTitle") or d.get("shortTitle") or "",
            "title": clip(ids.get("publicTitle") or d.get("ctTitle"), 140), "phase": phase,
            "status": CTIS_ONGOING[d["ctStatus"]], "sponsor": clip(org.get("name") or d.get("sponsor"), 60),
            "stype": "Industry" if "Pharmaceutical company" in (d.get("sponsorType") or "") else "Non-industry",
            "cond": clip(d.get("conditions"), 80), "n": int(d["totalNumberEnrolled"]) if str(d.get("totalNumberEnrolled", "")).isdigit() else None,
            "start": dmy(d.get("startDateEU")) or (dur.get("estimatedRecruitmentStartDate") or "")[:7],
            "pcd": (dur.get("estimatedGlobalEndDate") or dur.get("estimatedEndDate") or "")[:7],
            "countries": [c.split(":")[0] for c in d.get("trialCountries") or []],
            **c,
        }
    log(f"EU CTIS trials listing a device for a parenteral product: {len(out)}")
    return out


def dmy(s):
    m = re.match(r"(\d\d)/(\d\d)/(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}" if m else ""


def find_nct(code, eu):
    q = f'AREA[OrgStudyId]"{code}" OR AREA[SecondaryId]"{code}" OR AREA[SecondaryId]"{eu}"' if code else f'AREA[SecondaryId]"{eu}"'
    j = http("GET", CTG, params={"query.term": q, "fields": "NCTId,OverallStatus", "pageSize": 5, "format": "json"}).json()
    st = j.get("studies", [])
    return (ps(st[0])["identificationModule"]["nctId"], ps(st[0])["statusModule"]["overallStatus"]) if st else ("", "")


# --------------------------------------------------------------------------
# Merge, revenue, output
# --------------------------------------------------------------------------
EU27 = {"Austria": "AT", "Belgium": "BE", "Bulgaria": "BG", "Croatia": "HR", "Cyprus": "CY", "Czechia": "CZ", "Czech Republic": "CZ",
        "Denmark": "DK", "Estonia": "EE", "Finland": "FI", "France": "FR", "Germany": "DE", "Greece": "GR", "Hungary": "HU",
        "Ireland": "IE", "Italy": "IT", "Latvia": "LV", "Lithuania": "LT", "Luxembourg": "LU", "Malta": "MT", "Netherlands": "NL",
        "Poland": "PL", "Portugal": "PT", "Romania": "RO", "Slovakia": "SK", "Slovenia": "SI", "Spain": "ES", "Sweden": "SE",
        "Norway": "NO", "Iceland": "IS", "Liechtenstein": "LI"}
UKCH = {"United Kingdom": "UK", "Switzerland": "CH"}


def regions(countries):
    eu = sorted({EU27[c] for c in countries if c in EU27})
    ukch = sorted({UKCH[c] for c in countries if c in UKCH})
    return {"us": "United States" in countries, "eea": eu, "ukch": ukch, "india": "India" in countries}


def attach_revenue(rows, rev, cfg):
    us_s, eu_s = cfg["us_share"], cfg["europe_share"]
    for r in rows:
        r["rev"] = None
        if r["stype"] != "Industry" or r["phase"] not in ("3", "2/3"):
            continue
        hay = (r.get("drugs", "") + " " + r.get("title", "")).lower()
        for a in rev:
            if re.search(a["match"], hay, re.I):
                e = {k: a.get(k) for k in ("asset", "kind", "ww", "ww_low", "ww_high", "period", "source", "url", "date", "note", "us", "europe")}
                mid = a.get("ww") if a.get("ww") is not None else (
                    (a["ww_low"] + a["ww_high"]) / 2 if a.get("ww_low") is not None and a.get("ww_high") is not None else None)
                if mid is not None:
                    if e["us"] is None:
                        e["us"], e["us_modelled"] = round(mid * us_s, 2), True
                    if e["europe"] is None:
                        e["europe"], e["eu_modelled"] = round(mid * eu_s, 2), True
                r["rev"] = e
                break


def main():
    cur = json.loads((DATA / "curation.json").read_text())
    cfg = json.loads((DATA / "config.json").read_text())
    revp = DATA / "revenue.json"
    rev = json.loads(revp.read_text())["assets"] if revp.exists() else []

    ctg = run_ctg()
    try:
        eu = run_ctis()
    except RuntimeError as e:
        log(f"EU CTIS unavailable this run, keeping previous EU rows: {e}")
        prev = json.loads((DATA / "trials.json").read_text()) if (DATA / "trials.json").exists() else {"rows": []}
        eu = {r["eu"]: r for r in prev["rows"] if r.get("eu") and r["src"] == "EU"}

    # map EU trials to NCT numbers
    for e in eu.values():
        if not e["nct"]:
            try:
                e["nct"], e["ctg_status"] = find_nct(e["code"], e["eu"])
            except RuntimeError as err:
                log(f"NCT lookup failed {e['eu']}: {err}")
            time.sleep(0.2)

    rows = dict(ctg)
    extra = [e for e in eu.values() if e["nct"] and e["nct"] not in rows]
    ctg_extra = {ps(s)["identificationModule"]["nctId"]: s for s in ctg_by_ids({e["nct"] for e in extra})} if extra else {}
    added_eu = 0
    for e in eu.values():
        n = e["nct"]
        if n and n in rows:
            r = rows[n]
            r["eu"] = e["eu"]
            for c in e["cats"]:
                if c not in r["cats"] and c not in ("Cartridge system", "Implant with applicator"):
                    r["cats"].append(c)
            r["ev_eu"] = e["ev"]
            continue
        s = ctg_extra.get(n) if n else None
        if s:
            f = ctg_fields(s)
            raw = ps(s)["statusModule"]["overallStatus"]
            if raw in ("WITHDRAWN", "TERMINATED", "SUSPENDED"):
                continue
            if raw not in ST_MAP:
                f["status"] = e["status"]
            f.update({"cats": e["cats"], "basis": "E", "ev": e["ev"], "eu": e["eu"], "src": "EU"})
            if not f["phase"]:
                f["phase"] = e["phase"]
            if not f["drugs"]:
                f["drugs"] = e["drugs"]
            rows[n] = f
        else:
            if e.get("ctg_status") == "WITHDRAWN":
                continue
            f = {k: e[k] for k in ("title", "phase", "status", "sponsor", "stype", "drugs", "cond", "n", "start", "pcd", "countries", "cats", "ev", "eu")}
            f.update({"nct": "", "basis": "E", "src": "EU"})
            rows["EU:" + e["eu"]] = f
        added_eu += 1
    log(f"Trials added from the EU register: {added_eu}")

    out = []
    for key, r in rows.items():
        if r.get("nct") in cur["exclude"]:
            continue
        if r.get("nct") in cur["category"]:
            r["cats"] = [cur["category"][r["nct"]]]
        r.setdefault("eu", "")
        r.update(regions(r.get("countries", [])))
        r["ncountries"] = len(r.get("countries", []))
        r.pop("orgId", None)
        out.append(r)
    attach_revenue(out, rev, cfg)

    order = ["Autoinjector", "Pen injector", "Prefilled syringe", "On-body / wearable injector", "Needle-free injector", "Microneedle",
             "Infusion pump system", "Electroporation injection device", "Cartridge system", "Implant with applicator",
             "Drug-device combination (device type not stated)", "Other delivery device", "Dual-chamber system"]
    pv = {"3": 0, "2/3": 1, "2": 2, "1/2": 3}
    out.sort(key=lambda r: (min(order.index(c) if c in order else 99 for c in r["cats"]), pv.get(r["phase"], 9), r.get("nct") or r["eu"]))

    now = dt.datetime.now(dt.timezone.utc)
    meta = {"updated": now.strftime("%Y-%m-%d"), "updated_label": now.strftime("%-d %b %Y"), "count": len(out),
            "us_share": cfg["us_share"], "europe_share": cfg["europe_share"], "share_basis": cfg["share_basis"],
            "share_source_title": cfg.get("share_source_title", ""), "share_source_url": cfg.get("share_source_url", "")}
    log(f"Rows published: {len(out)}")

    (DATA / "trials.json").write_text(json.dumps({"meta": meta, "rows": out}, ensure_ascii=False, indent=1))
    tpl = (ROOT / "site" / "template.html").read_text()
    payload = json.dumps({"meta": meta, "rows": out}, ensure_ascii=False).replace("</", "<\\/")
    (ROOT / "index.html").write_text(tpl.replace("/*DATA*/null", payload))
    (DATA / "build-log.txt").write_text(now.isoformat() + "\n" + "\n".join(LOG) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # keep the last good page, record why
        LOG.append(f"BUILD FAILED: {e!r}")
        (DATA / "build-log.txt").write_text(dt.datetime.now(dt.timezone.utc).isoformat() + "\n" + "\n".join(LOG) + "\n")
        print("\n".join(LOG), file=sys.stderr)
        sys.exit(1)
