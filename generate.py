#!/usr/bin/env python3
"""Daily vacancy digest for a UX/UI designer.

Collects fresh design vacancies from rabota.by (Belarus), hh.ru (remote, Russia), getmatch.ru,
career.habr.com and designer.ru, checks them against the criteria in the configuration, marks new and re-posted
vacancies, writes vacancies.html and digest.md, and optionally sends the digest to
Telegram.

Personal settings do not live in this file. MIN_USD, MY_TARGET and the list of
companies already contacted come from the environment, so the repository carries
the tool and not the numbers of whoever runs it.

Usage:  python3 generate.py              (last 30 days)
        python3 generate.py --days 14
        python3 generate.py --telegram   (send to Telegram)
"""
import argparse, subprocess, datetime as dt, html, json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEEN = HERE / "seen.json"
COMPANIES = HERE / "companies.txt"
HISTORY = HERE / "history.json"
DIGEST = HERE / "digest.md"
TG_CONF = Path.home() / "Library/Application Support/vacancy-digest/telegram.json"
MY_TARGET = int(os.environ.get("MY_TARGET") or 0)  # the number to name first, $ net; 0 hides the comparison
OUT = HERE / "vacancies.html"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

QUERIES = ["UX/UI", "UI/UX", "продуктовый дизайнер", "product designer", "дизайнер интерфейсов",
           "веб-дизайнер", "web designer", "visual designer", "UX designer", "UI designer"]
# (host, area, extra params, label)
CIS_AREAS = ["113", "40", "97", "48", "13", "9"]  # Russia, Kazakhstan, Uzbekistan, Kyrgyzstan, Armenia, Azerbaijan
SOURCES = [
    ("rabota.by", ["16"], {}, "Беларусь"),
    ("hh.ru", CIS_AREAS, {"work_format": "REMOTE"}, "СНГ, удалёнка"),
]
ANALYSIS = HERE / "analysis.json"
COMPANY_CACHE = HERE / "employers.json"  # public employer pages only; contact statuses never go here
FEW_RESPONSES = 50
TITLE_OK = re.compile(r"дизайн|design|\bux\b|\bui\b", re.I)
TITLE_SKIP = re.compile(r"графическ|graphic|моушн|motion|3d|интерьер|interior|ландшафт|одежд|мебел|полиграф|smm|упаков|ювелир|флорист|парикмах|иллюстрат|архитект|разработчик|developer|engineer|инженер|аналитик|analyst|администр|презентац|продав|консультант|карточ|wildberries|ozon|маркетплейс|технический дизайн|реклам|баннер|принт|оформител|организацион|editor|head of frontend|оператор|фотограф|печатн|level design|геймдизайн|game design|нарратив|редактор|копирайт|writer|менеджер|manager|художник|artist|econom|экономик", re.I)

MIN_USD = int(os.environ.get("MIN_USD") or 1000)  # salary floor, $ net
HOME_CITY = os.environ.get("HOME_CITY", "")  # home city; unset means an office anywhere is fine
ENG_BAD = os.environ.get("ENG_BAD", "1") != "0"  # mark a vacancy red when it asks for B2+ English
FALLBACK_RATES = {"USD": 1.0, "BYR": 1 / 3.0, "BYN": 1 / 3.0, "RUR": 1 / 80, "RUB": 1 / 80, "EUR": 1.08}
EXP = {"noExperience": "без опыта", "between1And3": "1–3 года", "between3And6": "3–6 лет", "moreThan6": "6+ лет"}
FMT = {"REMOTE": "удалёнка", "HYBRID": "гибрид", "ON_SITE": "офис", "FIELD_WORK": "разъезды"}
EMP = {"FULL": "полная", "PART": "частичная", "PROJECT": "проект", "PROBATION": "стажировка", "VOLUNTEER": "волонтёр"}
LANG_LVL = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6, "l1": 7}


def fetch(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def usd_rates():
    """NBRB exchange rates: USD per one unit of currency."""
    try:
        data = json.loads(fetch("https://api.nbrb.by/exrates/rates?periodicity=0"))
        byn = {d["Cur_Abbreviation"]: d["Cur_OfficialRate"] / d["Cur_Scale"] for d in data}
        usd = byn["USD"]
        rates = {"USD": 1.0, "BYR": 1 / usd, "BYN": 1 / usd, "EUR": byn["EUR"] / usd, "RUR": byn["RUB"] / usd, "RUB": byn["RUB"] / usd}
        for c in ("KZT", "UZS", "KGS", "AMD", "AZN"):
            if c in byn:
                rates[c] = byn[c] / usd
        return rates, f"курс НБРБ на {dt.date.today():%d.%m}: 1 $ = {usd:.2f} BYN"
    except Exception as e:  # network or format issue: use approximate rates
        print("  NBRB rates unavailable, using approximate ones:", e, file=sys.stderr)
        return FALLBACK_RATES, "курсы приблизительные (НБРБ не ответил)"


def search(host, area, extra, query, days):
    out, page = [], 0
    while page < 3:
        params = {"text": query, "area": area, "search_field": "name", "order_by": "publication_time",
                  "search_period": str(days), "items_on_page": "50", "page": str(page), **extra}
        s = fetch(f"https://{host}/search/vacancy?" + urllib.parse.urlencode(params, doseq=True))
        m = re.search(r'id="HH-Lux-InitialState"[^>]*>(.*?)</template>', s, re.S)
        if not m:
            raise RuntimeError(f"{host}: не нашёл данные на странице (возможно, капча)")
        vsr = json.loads(html.unescape(m.group(1))).get("vacancySearchResult", {})
        items = vsr.get("vacancies", [])
        out += items
        pages = ((vsr.get("paging") or {}).get("lastPage") or {}).get("page", 1)
        page += 1
        if not items or page >= int(pages or 1):
            break
        time.sleep(0.7)
    return out


def fit_of(flags):
    lv = {f[0] for f in flags}
    return "bad" if "bad" in lv else ("warn" if "warn" in lv else "good")


# Share of a gross salary left after income tax and employee contributions, by currency (rough).
NET_SHARE = {"BYN": 0.86, "BYR": 0.86, "RUB": 0.87, "RUR": 0.87, "KZT": 0.78, "UZS": 0.88, "KGS": 0.82,
             "AMD": 0.75, "AZN": 0.83}


def pay(lo, hi, cur, rates, gross=False):
    """Salary as display text plus net USD numbers: lo, hi, top (for the floor check) and mid (for market stats)."""
    if not (lo or hi):
        return "", None
    cur = (cur or "").upper()
    cur_s = {"BYR": "BYN", "RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}.get(cur, cur)
    text = (f"{lo:,}–{hi:,}" if lo and hi else (f"от {lo:,}" if lo else f"до {hi:,}")).replace(",", " ") + f" {cur_s}"
    if gross:
        text += " gross"
    k = rates.get(cur)
    if not k:
        return text, None
    k *= NET_SHARE.get(cur, 0.87) if gross else 1
    nlo, nhi = (lo * k if lo else None), (hi * k if hi else None)
    r50 = lambda x: f"{round(x / 50) * 50:,}".replace(",", " ")
    if cur != "USD" or gross:
        span = f"{r50(nlo)}–{r50(nhi)}" if nlo and nhi else r50(nlo or nhi)
        text += f" ≈ ${span}" + (" на руки" if gross else "")
    mid = (nlo + nhi) / 2 if nlo and nhi else nlo  # "до X" has no reliable middle
    return text, {"lo": nlo, "hi": nhi, "top": nhi or nlo, "mid": mid}


def habr_all(days):
    """Habr Career: full-text search, so several queries plus a title filter later."""
    out, cutoff = {}, dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    for q in ["UX/UI", "UI/UX", "продуктовый дизайнер", "product designer", "дизайнер интерфейсов", "веб-дизайнер", "designer"]:
        for page in range(1, 6):
            d = json.loads(fetch("https://career.habr.com/api/frontend/vacancies?" + urllib.parse.urlencode(
                {"q": q, "type": "all", "sort": "date", "page": page})))
            items = d.get("list", [])
            for v in items:
                out[v["id"]] = v
            old = items and all(dt.datetime.fromisoformat(v["publishedDate"]["date"]) < cutoff for v in items if not v.get("isMarked"))
            if old or page >= d.get("meta", {}).get("totalPages", 1):
                break
            time.sleep(0.5)
    return [v for v in out.values() if dt.datetime.fromisoformat(v["publishedDate"]["date"]) >= cutoff]


def normalize_habr(v, rates):
    sal = v.get("salary") or {}
    salary, money_ = pay(sal.get("from"), sal.get("to"), sal.get("currency"), rates)
    locs = [l["title"] for l in (v.get("locations") or [])]
    name, flags = v["title"].strip(), []
    if not v.get("remoteWork") and HOME_CITY and HOME_CITY not in locs:
        flags.append(("bad", "офис: " + (", ".join(locs) or "?")))
    q = v.get("qualification") or ""
    flags += level_flags(name + " " + q)
    flags += salary_flags(money_, open_top=not sal.get("to"))
    return {
        "id": "hb" + str(v["id"]), "name": name, "company": (v.get("company") or {}).get("title", ""),
        "url": "https://career.habr.com" + v["href"], "salary": salary, "pay": money_, "pub": v["publishedDate"]["date"], "created": "",
        "reposted": False, "city": ", ".join(locs), "region": "habr", "source": "career.habr.com",
        "formats": ["удалёнка"] if v.get("remoteWork") else ["офис"], "exp": q, "emp": "", "flags": flags,
        "fit": fit_of(flags), "responses": None, "test": False, "letter": False,
    }


RU_MON = {m: i + 1 for i, m in enumerate(["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"])}


def designer_ru(days, pages=8):
    """designer.ru/vacancy: HTML cards with date, format, company and "<Company> ищет ..." title."""
    out, today = [], dt.date.today()
    cutoff = today - dt.timedelta(days=days)
    for page in range(1, pages + 1):
        s = fetch("https://designer.ru/vacancy/" + (f"?PAGEN_1={page}" if page > 1 else ""))
        cards = re.split(r'id="bx_\d+_(\d+)"', s)
        fresh_on_page = 0
        for i in range(1, len(cards), 2):
            c = cards[i + 1]
            href = re.search(r'href="(/vacancy/[^"]+)"', c)
            parts = [x.strip() for x in re.sub(r"<[^>]+>", "|", c).split("|")]
            parts = [html.unescape(re.sub(r"\s+", " ", x)) for x in parts if x.strip() and x.strip() != ">"]
            if not href or len(parts) < 4:
                continue
            m = re.match(r"(\d+) (\w{3})", parts[0])
            if not m:
                continue
            d = dt.date(today.year, RU_MON.get(m.group(2), 1), int(m.group(1)))
            if d > today:
                d = d.replace(year=today.year - 1)
            if d < cutoff:
                continue
            fresh_on_page += 1
            title = next((x for x in parts if " ищет " in x), parts[3])
            out.append({"id": cards[i], "href": href.group(1), "date": d, "where": parts[1], "company": parts[2], "title": title})
        if not fresh_on_page:
            break
        time.sleep(0.5)
    return out


def normalize_designer_ru(v):
    title = v["title"]
    role = title.split(" ищет ", 1)[1] if " ищет " in title else title
    role = re.sub(r"^(в команду |на удал[её]нку |на проект )+", "", role)
    role = role[:1].upper() + role[1:]
    where, flags = v["where"], []
    remote = "удал" in where.lower()
    if not remote and HOME_CITY and HOME_CITY not in where:
        flags.append(("bad", "офис: " + where))
    flags += level_flags(role)
    pub = dt.datetime.combine(v["date"], dt.time(12)).astimezone()
    return {
        "id": "dr" + v["id"], "name": role, "company": v["company"], "url": "https://designer.ru" + v["href"],
        "salary": "", "pub": pub.isoformat(), "created": "", "reposted": False, "city": where, "region": "designer.ru",
        "source": "designer.ru", "formats": [], "exp": "", "emp": "", "flags": flags, "fit": fit_of(flags),
        "responses": None, "test": False, "letter": False,
    }


OUTSIDE_RF = re.compile(r"весь мир|worldwide|беларус|минск|снг|казахстан|армени|грузи|узбекистан|кыргызстан|азербайджан|европ|europe|любой стран|anywhere", re.I)
SENIOR = re.compile(r"senior|сеньор|\bsr\b|lead|\bлид\b|head|director|директор|руководител|ведущ|старш|principal|staff", re.I)
JUNIOR = re.compile(r"junior|junoir|джун|младш|стаж[её]р|стажировк|intern|начинающ|ассистент", re.I)
GAMEDEV = re.compile(r"\bgame|гейм|gamedev|\bunity\b|игров", re.I)


def level_flags(title, exp_code=None):
    """Middle only: senior/lead and junior/intern are excluded."""
    middle = re.search(r"middle|мидл", title, re.I)
    if exp_code == "moreThan6" or (SENIOR.search(title) and not middle):
        return [("bad", "senior")]
    extra = [("warn", "геймдев")] if GAMEDEV.search(title) else []
    if SENIOR.search(title):
        return [("warn", "middle/senior")] + extra
    if JUNIOR.search(title):
        return [("bad", "junior")]
    if exp_code == "noExperience" and not re.search(r"middle|мидл", title, re.I):
        return [("bad", "без опыта, junior")]
    return ([("good", "middle")] if middle else []) + extra


def salary_flags(money_, open_top=False):
    """Net pay vs the floor. Excluded only when the whole range is below it; "from X" has no ceiling, so it is a warning."""
    usd = money_ and money_["top"]
    if usd is None:
        return []
    if usd >= MIN_USD:
        return [("good", f"от ${MIN_USD}")]
    return [("warn", f"вилка от ${round(usd / 50) * 50}")] if open_top else [("bad", f"ниже ${MIN_USD}")]


# --- reading the vacancy text ---
UXUI_TEXT = re.compile(r"сайт|лендинг|веб-дизайн|веб‑дизайн|web design|интерфейс|\bux\b|\bui\b|figma|прототип|user flow|юзер|пользовательск|вайрфрейм|wireframe|дизайн-систем|design system|продуктов", re.I)
RF_ONLY = re.compile(r"(только|исключительно)\s+(из|в|на территории)\s+(рф|росси)|находит\w*\s+(в|на территории)\s+(рф|росси)|"
                     r"(налогов\w+\s+)?резидент\w*\s+рф|гражданств\w*\s+рф|граждан\w*\s+рф|проживани\w*\s+(в|на территории)\s+(рф|росси)|"
                     r"(самозанят|\bип\b)[^.\n]{0,40}\bрф\b|только\s+рф|located in russia|russia only|from russia only|"
                     r"(внутри|с территории|на территории|в пределах)\s+(рф|росси)|нахождени\w*\s+(в|на территории)\s+(рф|росси)|"
                     r"удал[её]н\w*[^.\n]{0,30}\s(из|по)\s+(рф|росси)", re.I)
TK_RF = re.compile(r"(тк|трудов\w+ кодекс\w*)\s+рф", re.I)
ENG_HIGH = re.compile(r"(english|английск)[^.\n]{0,60}?(b2|c1|c2|upper|advanced|fluent|свободн|разговорн)|(b2|c1|upper[- ]?intermediate|fluent)[^.\n]{0,30}(english|английск)", re.I)
ENG_MID = re.compile(r"(english|английск)[^.\n]{0,60}?(b1|intermediate|технич|чтени)", re.I)
YEARS = re.compile(r"(?:опыт\w*|experience)[^.\n]{0,50}?(?:от|не менее|минимум|более|at least|from)?\s*(\d{1,2})\s*\+?\s*(?:-|–|до)?\s*\d*\s*(?:лет|год|years?|yrs)", re.I)
DOMAINS = {
    "финтех": r"финтех|fintech|\bбанк[аеиу]?\b(?!\s+вакансий)|банковск\w*\s+(сервис|продукт|приложени)|интернет-банк|сбербанк онлайн|криптобанк|"
              r"платёжн|платежн|payment|кредитн\w*\s+(продукт|карт|конвейер)|insurtech|страхов\w*\s+(продукт|сервис|компани)|"
              r"инвестиц\w*\s+(платформ|продукт|приложени)|брокер|финанс\w*\s+(данн|продукт|сервис|сектор|организац)|управля\w*\s+финанс|wallet|кошел[её]к",
    "крипто": r"\bкрипт|crypto|blockchain|блокчейн|web3|defi|\bnft\b|токеномик",
    "e-commerce": r"e-?commerce|интернет-магазин|маркетплейс|marketplace|ритейл|retail|корзин|checkout|каталог товар",
    "B2B/SaaS": r"\bb2b\b|saas|\bcrm\b|\berp\b|дашборд|dashboard|админк|enterprise|бэк-?офис|back-?office|корпоративн\w+ систем",
    "мобайл": r"мобильн\w*\s+(приложени|продукт|сервис|платформ|игр|онлайн|верси|устройств|интерфейс)|mobile(?!\s+devs)|\bios\b|android",
    "дизайн-системы": r"дизайн-систем|дизайн систем|design system|ui[- ]?kit|компонентн\w+ библиотек",
}


def read_text(desc):
    t = re.sub(r"<[^>]+>", " ", desc or "")
    return re.sub(r"\s+", " ", html.unescape(t)).replace("\xa0", " ")


def assess_text(r, desc):
    """Checks the full vacancy text. Returns flags, matched domains and duties."""
    t = read_text(desc)
    flags = []
    if not UXUI_TEXT.search(t + " " + r["name"]) or re.search(r"сборк\w* лендинг|конструктор\w* сайт|tilda|тильд|онлайн-конструктор", r["name"], re.I):
        flags.append(("bad", "по тексту не UX/UI"))
    if RF_ONLY.search(t):
        flags.append(("warn", "только из РФ"))  # many work for RF employers from Belarus anyway: warn, not exclude
    if TK_RF.search(t) and not any(f[1] == "только из РФ" for f in flags):
        flags.append(("warn", "оформление по ТК РФ"))
    if re.search(r"немецк|german|deutsch|французск|french|испанск|spanish", r["name"] + " " + t[:3000], re.I) and \
            re.search(r"(знани|владени|требован|обязател|required|fluent|уров)\w*[^.\n]{0,40}(немецк|german|deutsch|французск|french|испанск|spanish)|"
                      r"(немецк|german|deutsch|французск|french|испанск|spanish)\w*[^.\n]{0,40}(язык|language|b1|b2|c1)", r["name"] + " " + t, re.I):
        flags.append(("bad", "нужен другой иностранный язык"))
    if ENG_HIGH.search(t):
        flags.append(("bad" if ENG_BAD else "warn", "английский B2+ в тексте"))
    elif ENG_MID.search(t) and not any("английский" in f[1] for f in r["flags"]):
        flags.append(("warn", "английский B1 в тексте"))
    years = [int(m.group(1)) for m in YEARS.finditer(t) if 0 < int(m.group(1)) < 15]
    if years and min(years) >= 5:
        flags.append(("bad", f"опыт от {min(years)} лет"))
    if re.search(r"тестов\w*\s+задани|test (task|assignment)", t, re.I):
        flags.append(("plain", "тестовое"))
    doms = [k for k, rx in DOMAINS.items() if re.search(rx, t + " " + r["name"], re.I)]
    return flags, doms, duties(desc)


# --- company research (hh / rabota.by employer pages) ---
KIND = [
    ("кадровое агентство", r"кадров\w* агентств|рекрутинг|рекрутингов|recruit|подбор\w* персонал|staffing|аутстаф"),
    ("агентство/студия", r"(digital|креативн\w*|рекламн\w*|маркетингов\w*|брендингов\w*|интернет)[- ]агентств|"
                          r"(дизайн|digital|веб|web|креативн\w*)[- ]студи|(digital|creative|design|branding|marketing) (studio|agency)"),
    ("аутсорс", r"аутсорс|outsourc|заказн\w* разработ|разработк\w* (ПО |программного обеспечения )?(под ключ|на заказ|для клиентов)|software development company"),
    ("продукт", r"продуктов\w* (компани|IT|ИТ)|собственн\w* (продукт|платформ|сервис)|свой продукт|наш\w* продукт|product company"),
]


def company_info(eid):
    """What the employer does, taken from its public rabota.by / hh.ru page."""
    page = fetch(f"https://rabota.by/employer/{eid}")
    m = re.search(r'id="HH-Lux-InitialState"[^>]*>(.*?)</template>', page, re.S)
    d = json.loads(html.unescape(m.group(1)))
    ei = d.get("employerInfo") or {}
    rating = (d.get("employerOrganizationSchema") or {}).get("aggregateRating") or {}
    return {
        "desc": read_text(ei.get("description") or "").strip()[:1500], "site": (ei.get("site") or {}).get("href") or "",
        "country": ei.get("employerCountryCode") or "", "it": bool(ei.get("accreditedITEmployer")),
        "rating": rating.get("ratingValue"), "reviews": rating.get("ratingCount"),
    }


def co_line(c):
    """One line: kind, rating, accreditation, first meaningful sentence of the description."""
    if not c:
        return ""
    desc = c.get("desc", "")
    kind = next((k for k, rx in KIND if re.search(rx, desc, re.I)), "")
    what = next((x for x in re.split(r"(?<=[.!?])\s+", desc) if len(x.split()) >= 5), "")
    if len(what) > 80:
        what = what[:80].rsplit(" ", 1)[0] + "…"
    bits = [kind] if kind else []
    if c.get("rating") and (c.get("reviews") or 0) >= 5:
        bits.append(f"★{c['rating']} ({c['reviews']} отз.)")
    if c.get("it"):
        bits.append("аккредитованная IT")
    head = " · ".join(bits)
    return (head + (": " if head and what else "") + what).strip()


def read_companies(found):
    """Looks up employers of vacancies that passed the filters. Cached for 30 days."""
    cache = json.loads(COMPANY_CACHE.read_text()) if COMPANY_CACHE.exists() else {}
    today = dt.date.today()
    fresh = 0
    for r in found.values():
        eid = r.get("employer_id")
        if r["fit"] == "bad" or not eid:
            continue
        c = cache.get(str(eid))
        if not c or "desc" not in c or (today - dt.date.fromisoformat(c["date"])).days > 30:
            try:
                c = company_info(eid) | {"date": today.isoformat()}
                cache[str(eid)] = c
                fresh += 1
                time.sleep(0.4)
            except Exception:
                continue
        r["co"] = co_line(c)
    COMPANY_CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    return fresh


def read_all(found):
    """Reads the text of every vacancy that passed the basic checks. Results are cached by id."""
    cache = json.loads(ANALYSIS.read_text()) if ANALYSIS.exists() else {}
    today = dt.date.today().isoformat()
    fresh = 0
    for vid, r in found.items():
        if r["fit"] == "bad":
            continue
        c = cache.get(vid)
        if not c:
            desc = description(r)
            if not desc:
                r["flags"].append(("plain", "не прочитана"))
                r["unread"] = True
                continue
            fl, doms, duty = assess_text(r, desc)
            c = cache[vid] = {"flags": fl, "domains": doms, "duty": duty, "date": today}
            fresh += 1
            time.sleep(0.4)
        c["date"] = today
        r["flags"] += [("warn", f[1]) if f[1] == "только из РФ" else tuple(f) for f in c["flags"]]
        r["flags"] += [("good", "✓ " + d) for d in c["domains"]]
        r["domains"], r["duty"] = c["domains"], c["duty"]
        r["fit"] = fit_of(r["flags"])
    cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get("date", today) >= cutoff}
    ANALYSIS.write_text(json.dumps(cache, ensure_ascii=False))
    return fresh


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def flat(lst, key):
    try:
        return lst[0][key]
    except (IndexError, KeyError, TypeError):
        return []


def normalize(v, region, rates):
    c = v.get("compensation") or {}
    lo, hi, cur = c.get("from"), c.get("to"), c.get("currencyCode")
    salary, money_ = pay(lo, hi, cur, rates, gross=bool(c.get("gross")))
    langs = [l for l in flat(v.get("languages"), "language")]
    eng = max((LANG_LVL.get(l.get("degree"), 0) for l in langs if l.get("isoCode") == "eng"), default=0)
    eng_lbl = next((l["degree"].upper() for l in langs if l.get("isoCode") == "eng" and LANG_LVL.get(l.get("degree"), 0) == eng), "")
    formats = flat(v.get("workFormats"), "workFormatsElement")
    city = (v.get("area") or {}).get("name", "")
    pub, created = parse_time(v["publicationTime"]["$"]), parse_time(v.get("creationTime"))
    name = v["name"]
    company = v["company"].get("visibleName") or v["company"].get("name", "")
    if v["company"].get("@category") == "PERSON" or not v["company"].get("id"):
        company += " (частное лицо)"

    # check against criteria
    flags = []  # (level, text): bad / warn / good
    remote = "REMOTE" in formats
    if not remote and HOME_CITY and city != HOME_CITY:
        flags.append(("bad", f"офис в г. {city}"))
    if eng >= LANG_LVL["b2"]:
        flags.append(("bad", f"английский {eng_lbl}"))
    elif eng == LANG_LVL["b1"]:
        flags.append(("warn", "английский B1"))
    flags += level_flags(name, v.get("workExperience"))
    flags += salary_flags(money_, open_top=not hi)
    reposted = bool(created and pub and (pub - created).days >= 2)
    return {
        "id": str(v["vacancyId"]), "name": name, "company": company, "employer_id": v["company"].get("id"),
        "url": v.get("links", {}).get("desktop") or f"https://rabota.by/vacancy/{v['vacancyId']}",
        "salary": salary, "pay": money_, "pub": pub.isoformat(), "created": created.isoformat() if created else "",
        "reposted": reposted, "city": city, "region": region, "source": v.get("displayHost") or ("rabota.by" if region == "Беларусь" else "hh.ru"),
        "formats": [FMT.get(f, f) for f in formats], "exp": EXP.get(v.get("workExperience"), ""),
        "emp": EMP.get(v.get("employmentForm"), ""), "flags": flags,
        "fit": fit_of(flags),
        "responses": v.get("totalResponsesCount") or v.get("responsesCount"),
        "test": bool(v.get("userTestPresent")), "letter": bool(v.get("@responseLetterRequired")),
    }


def getmatch_all():
    """getmatch.ru returns all active offers in one list; there is no text filter."""
    out, offset = [], 0
    while offset < 3000:
        try:
            d = json.loads(fetch(f"https://getmatch.ru/api/offers?limit=100&offset={offset}"))
        except ValueError:  # empty response past the end of the list
            break
        out += d.get("offers", [])
        offset += 100
        if offset >= d.get("meta", {}).get("total", 0):
            break
        time.sleep(0.5)
    return out


def normalize_getmatch(v, rates):
    pub = dt.datetime.fromisoformat(v["published_at"]).replace(tzinfo=dt.timezone.utc)
    lo, hi, cur = v.get("salary_display_from"), v.get("salary_display_to"), v.get("salary_currency")
    salary, money_ = pay(lo, hi, cur, rates, gross=v.get("salary_taxes") == "gross")
    locs = [l for l in v.get("location_items") or [] if not l.get("exclude")]
    fmts = {l.get("format") for l in locs}
    labels = ", ".join(dict.fromkeys(l["label"] for l in locs))
    eng_name = ((v.get("english_level") or {}).get("name") or "")[:2]
    eng = LANG_LVL.get(eng_name.lower(), 0)
    name = v["position"]
    flags = []
    at_home = not HOME_CITY or any(HOME_CITY in l["label"] for l in locs)
    remote_labels = [l["label"] for l in locs if l.get("format") == "remote"]
    if not remote_labels and not at_home:
        flags.append(("bad", "релокация" if any("relocation" in f for f in fmts) else f"офис: {labels}"))
    elif remote_labels and not at_home and not any(OUTSIDE_RF.search(x) for x in remote_labels):
        flags.append(("warn", "удалёнка только из РФ"))
    if eng >= LANG_LVL["b2"]:
        flags.append(("bad", f"английский {eng_name}"))
    elif eng == LANG_LVL["b1"]:
        flags.append(("warn", "английский B1"))
    flags += level_flags(name)
    flags += salary_flags(money_, open_top=not hi)
    fmt_ru = {"remote": "удалёнка", "hybrid": "гибрид", "office": "офис"}
    return {
        "id": "gm" + str(v["id"]), "name": name, "company": (v.get("company") or {}).get("name", ""),
        "url": "https://getmatch.ru" + v["url"], "salary": salary, "pay": money_, "pub": pub.isoformat(), "created": "",
        "reposted": False, "city": labels, "region": "getmatch", "source": "getmatch.ru",
        "formats": [fmt_ru[f] for f in fmt_ru if f in fmts], "exp": "", "emp": "", "flags": flags,
        "fit": fit_of(flags),
        "responses": None, "test": False, "letter": bool(v.get("cover_letter_required")),
        "_desc": v.get("description_html") or (v.get("offer_description") or "").replace("\n", "<br>"),
    }


def load_companies():
    out = []
    env_companies = os.environ.get("COMPANIES")
    source = env_companies.splitlines() if env_companies else (
        COMPANIES.read_text(encoding="utf-8").splitlines() if COMPANIES.exists() else [])
    if source:
        for line in source:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            names, status = line.split("|", 1)
            out.append(([n.strip().lower() for n in names.split("/") if n.strip()], status.strip()))
    return out


def mark_companies(rows, companies):
    for r in rows.values():
        co = r["company"].lower()
        for names, status in companies:
            if any(n in co for n in names):
                r["status"] = status
                bad = status.lower().startswith("отказ")
                r["flags"].insert(0, ("bad" if bad else "plain", status))
                r["fit"] = fit_of(r["flags"])
                break


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f = int(k)
    return xs[f] + (xs[min(f + 1, len(xs) - 1)] - xs[f]) * (k - f)


def usd_of(r):
    return (r.get("pay") or {}).get("top")


def best_picks(rows, n=40):
    """Best vacancies to apply to today: no red flags, fresh, not re-posted, few responses."""
    cand = [r for r in rows if r["fit"] != "bad" and not r.get("status") and not r.get("unread")]
    now = dt.datetime.now(dt.timezone.utc)
    med_resp = pct([r["responses"] for r in rows if r["responses"]], .5) or 300
    def key(r):
        age = (now - dt.datetime.fromisoformat(r["pub"])).days
        resp = r["responses"] if r["responses"] is not None else med_resp
        warns = sum(f[0] == "warn" for f in r["flags"])
        return (warns - 0.8 * len(r.get("domains") or []) - (1.5 if r.get("new") else 0) + (1 if r["reposted"] else 0)
                + age / 7 + min(resp, 600) / 200 - (0.5 if usd_of(r) and usd_of(r) >= MIN_USD else 0))
    # rabota.by first: Minsk and Belarus are the main market
    return sorted(cand, key=lambda r: (r["source"] != "rabota.by", key(r)))[:n]


def analyze(rows, errors):
    today = dt.date.today().isoformat()
    ok = [r for r in rows if r["fit"] != "bad"]
    new = [r for r in rows if r.get("new")]
    new_ok = [r for r in new if r["fit"] != "bad"]
    paid = [r for r in rows if usd_of(r)]
    # market stats: middle of the range (or "from X"), net; "up to X" is left out
    mid = [r["pay"]["mid"] for r in paid if r["pay"].get("mid") and not any(f[1] in ("senior", "junior") for f in r["flags"])]
    by_rows = [r for r in rows if r["source"] == "rabota.by"]
    remote = [r for r in by_rows if "удалёнка" in r["formats"]]
    eng = [r for r in rows if any("английский" in f[1] for f in r["flags"])]
    resp = [r["responses"] for r in rows if r["responses"]]
    by_src, by_co = {}, {}
    for r in rows:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        by_co[r["company"]] = by_co.get(r["company"], 0) + 1
    week_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7))
    this_week = [r for r in rows if dt.datetime.fromisoformat(r["pub"]) >= week_ago]
    this_week_fresh = [r for r in this_week if not r["reposted"]]
    st = {
        "date": today, "total": len(rows), "ok": len(ok), "new": len(new), "new_ok": len(new_ok),
        "week": len(this_week), "week_fresh": len(this_week_fresh),
        "reposted_share": round(100 * sum(r["reposted"] for r in rows) / max(len(rows), 1)),
        "salary_share": round(100 * len(paid) / max(len(rows), 1)),
        "mid_median": round(pct(mid, .5) or 0), "mid_p25": round(pct(mid, .25) or 0), "mid_p75": round(pct(mid, .75) or 0), "mid_n": len(mid),
        "remote_share": round(100 * len(remote) / max(len(by_rows), 1)),
        "eng_share": round(100 * len(eng) / max(len(rows), 1)),
        "resp_median": round(pct(resp, .5) or 0),
        "sources": by_src,
    }
    hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    hist = [h for h in hist if h["date"] != today] + [{k: v for k, v in st.items() if k != "sources"}]
    hist = hist[-120:]
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=1))
    prev = [h for h in hist if h["date"] < today]
    week_hist = [h for h in hist if h["date"] >= (dt.date.today() - dt.timedelta(days=7)).isoformat() and h["date"] < today]

    ins = []  # insights
    if st["mid_n"] >= 5:
        line = (f"Мидл без senior/junior: медиана ≈ ${st['mid_median']}, половина вилок между "
                f"${st['mid_p25']} и ${st['mid_p75']} (по {st['mid_n']} вакансиям с зарплатой).")
        if MY_TARGET:
            pos = "выше" if MY_TARGET > st["mid_median"] else "ниже"
            tail = ", это нормальная цифра для старта торга" if pos == "выше" and MY_TARGET <= st["mid_p75"] else ""
            line += f" Твои ${MY_TARGET} — {pos} медианы{tail}."
        ins.append(line)
    ins.append(f"Зарплату указывают {st['salary_share']}% вакансий, поэтому медиана — ориентир, а не рынок целиком.")
    ins.append(f"{st['reposted_share']}% вакансий — поднятые старые. Настоящих новых за 7 дней: {st['week_fresh']} из {st['week']}.")
    if st["resp_median"]:
        ins.append(f"Медиана откликов на hh/rabota.by — {st['resp_median']}. Вакансии с откликами меньше сотни стоит закрывать первыми: там тебя прочитают.")
    ins.append(f"Английский B1+ просят в {st['eng_share']}% вакансий. На rabota.by удалёнку дают в {st['remote_share']}%, остальное — офис или гибрид.")
    if week_hist and sum(h["new"] for h in week_hist):
        avg_new = sum(h["new"] for h in week_hist) / len(week_hist)
        trend = "больше обычного" if st["new"] > avg_new * 1.3 else ("меньше обычного" if st["new"] < avg_new * 0.7 else "как обычно")
        ins.append(f"Сегодня новых {st['new']}, в среднем за неделю {avg_new:.1f} в день — {trend}.")
    elif not prev:
        ins.append("Это первый день истории: динамика появится со второго запуска.")
    top_co = sorted(((c, n) for c, n in by_co.items() if n >= 2), key=lambda x: -x[1])[:5]
    if top_co:
        ins.append("Нанимают активнее всех: " + ", ".join(f"{c} ({n})" for c, n in top_co) + ".")
    picks = best_picks(rows)
    new_ok.sort(key=lambda r: r["source"] != "rabota.by")  # stable sort keeps date order inside groups
    return st, ins, picks, hist, new_ok


def write_digest(st, ins, picks, new_ok, errors):
    L = [f"# Вакансии — сводка на {dt.date.today():%d.%m.%Y}", "",
         f"Всего {st['total']}, без красных {st['ok']}, новых с прошлого запуска {st['new']} (подходят {st['new_ok']}).", "",
         "## Выводы", *[f"- {x}" for x in ins], "", "## Кому откликнуться в первую очередь"]
    for r in picks:
        L.append(f"- [{r['name']}]({r['url']}) — {r['company']}; {r['salary'] or 'без зарплаты'}; "
                 + "; ".join(x for x in [", ".join(r["formats"]), f"{r['responses']} откликов" if r["responses"] else "", r["source"]] if x))
    if new_ok:
        L += ["", "## Новые подходящие"]
        L += [f"- [{r['name']}]({r['url']}) — {r['company']}" + (f"; {r['salary']}" if r["salary"] else "")
              + ("; " + ", ".join(f[1] for f in r["flags"]) if r["flags"] else "") for r in new_ok[:20]]
    if errors:
        L += ["", "## Ошибки источников", *[f"- {e}" for e in errors]]
    DIGEST.write_text("\n".join(L) + "\n", encoding="utf-8")


# --- marks from Telegram (buttons and text replies), stored encrypted in the public repo ---
STATE_ENC = HERE / "state.enc"
STATE_KEY_FILE = Path.home() / "Library/Application Support/vacancy-digest/state_key"
ACTIONS = {"a": "откликнулся", "s": "мимо", "r": "отказ"}
REPLY = re.compile(r"^\s*(\d{1,2})\s*[.:)-]?\s*(отклик\w*|откликнулся|мимо|не\s*интерес\w*|скип|отказ\w*|сброс|отмен\w*)", re.I | re.M)


def state_key():
    import os
    if os.environ.get("STATE_KEY"):
        return os.environ["STATE_KEY"]
    return STATE_KEY_FILE.read_text().strip() if STATE_KEY_FILE.exists() else None


def _openssl(args, data, key):
    import os
    env = dict(os.environ, STATE_KEY=key)
    return subprocess.run(["openssl", "enc", *args, "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-md", "sha256",
                           "-pass", "env:STATE_KEY"], input=data, capture_output=True, env=env, check=True).stdout


def load_state():
    key = state_key()
    empty = {"offset": 0, "last": [], "shown": {}, "marks": {}}
    if not key or not STATE_ENC.exists():
        return empty
    return empty | json.loads(_openssl(["-d"], STATE_ENC.read_bytes(), key))


def save_state(state):
    key = state_key()
    if key:
        STATE_ENC.write_bytes(_openssl(["-salt"], json.dumps(state, ensure_ascii=False).encode(), key))


def tg_api(conf, method, **params):
    req = urllib.request.Request(f"https://api.telegram.org/bot{conf['token']}/{method}",
                                 data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())["result"]


def poll_marks(conf, state):
    """Reads button presses and replies since the last run. Telegram keeps them for 24 hours."""
    ups = tg_api(conf, "getUpdates", offset=state["offset"], timeout=0, allowed_updates=["message", "callback_query"])
    done = []
    today = dt.date.today().strftime("%d.%m")
    for u in ups:
        state["offset"] = u["update_id"] + 1
        cq, msg = u.get("callback_query"), u.get("message")
        chat = ((cq or {}).get("message") or msg or {}).get("chat", {}).get("id")
        if str(chat) != str(conf["chat_id"]):
            continue  # only the owner's chat counts
        pairs = []
        if cq and (cq.get("data") or "").startswith("m|"):
            _, act, vid = cq["data"].split("|", 2)
            pairs.append((act, vid))
            try:
                tg_api(conf, "answerCallbackQuery", callback_query_id=cq["id"], text="Принято, запишу при проверке")
            except Exception:
                pass  # too late to answer; the mark still counts
        elif msg and msg.get("text"):
            for m in REPLY.finditer(msg["text"]):
                n, word = int(m.group(1)), m.group(2).lower()
                act = "a" if word.startswith("отклик") else "r" if word.startswith("отказ") else \
                      "x" if word.startswith(("сброс", "отмен")) else "s"
                if 0 < n <= len(state["last"]):
                    pairs.append((act, state["last"][n - 1]))
        for act, vid in pairs:
            info = state["shown"].get(vid)
            if not info:
                continue
            if act == "x":
                state["marks"].pop(vid, None)
                done.append(f"{info['company']} — {info['name']}: отметка снята")
            else:
                state["marks"][vid] = {"act": act, "company": info["company"], "name": info["name"], "date": today}
                done.append(f"{info['company']} — {info['name']}: {ACTIONS[act]}")
    if done:
        tg_send("Записал:\n" + "\n".join("• " + html.escape(x) for x in done) +
                "\n\nУчту в утренней сводке. Ошибся — ответь «номер сброс».", conf)
    return len(done)


def apply_marks(found, state):
    """Company-level: applied and rejected. Vacancy-level: skip."""
    by_company = {}
    for vid, m in state["marks"].items():
        if m["act"] in ("a", "r"):
            by_company[m["company"].lower()] = m
    for vid, r in found.items():
        m = state["marks"].get(vid)
        if m and m["act"] == "s":
            r["flags"].insert(0, ("bad", "мимо"))
            r["fit"] = "bad"
            continue
        m = by_company.get(r["company"].lower())
        if m and not r.get("status"):
            label = ("откликнулся " if m["act"] == "a" else "отказ ") + m["date"]
            r["status"] = label
            r["flags"].insert(0, ("bad" if m["act"] == "r" else "plain", label))
            r["fit"] = fit_of(r["flags"])


def remember_shown(state, shown):
    today = dt.date.today().isoformat()
    state["last"] = [r["id"] for r in shown]
    for r in shown:
        state["shown"][r["id"]] = {"company": r["company"], "name": r["name"], "date": today}
    cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    state["shown"] = {k: v for k, v in state["shown"].items() if v["date"] >= cutoff or k in state["marks"]}


def keyboard(shown):
    rows = [[{"text": f"{n} ✓ откликнулся", "callback_data": f"m|a|{r['id']}"},
             {"text": "✕ мимо", "callback_data": f"m|s|{r['id']}"},
             {"text": "отказ", "callback_data": f"m|r|{r['id']}"}] for n, r in enumerate(shown, 1)]
    return {"inline_keyboard": rows}


def tg_conf():
    """In the cloud: from env vars (GitHub secrets); on the Mac: from a local file."""
    import os
    if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        return {"token": os.environ["TELEGRAM_TOKEN"], "chat_id": os.environ["TELEGRAM_CHAT_ID"]}
    if TG_CONF.exists():
        return json.loads(TG_CONF.read_text())
    return None


def tg_send_file(path, caption, conf):
    """Sends the page as a file so it opens right inside Telegram on the phone."""
    boundary = "----vacancydigest"
    data = path.read_bytes()
    parts = []
    for k, v in (("chat_id", str(conf["chat_id"])), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    fname = f"vacancies-{dt.date.today():%Y-%m-%d}.html"
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{fname}"\r\nContent-Type: text/html\r\n\r\n'.encode() + data + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{conf['token']}/sendDocument", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def tg_send(text, conf=None, markup=None):
    conf = conf or tg_conf()
    fields = {"chat_id": conf["chat_id"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if markup:
        fields["reply_markup"] = json.dumps(markup)
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{conf['token']}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


DUTY_HEAD = re.compile(r"обязанност|задач|что (?:надо|нужно|предстоит|будет|будешь)|чем (?:предстоит|будешь|заниматься)|что делать|responsibilit|what you.{0,4}ll do|your role", re.I)
STOP_HEAD = re.compile(r"требован|ожида|нам важно|мы ждём|мы ждем|ищем тебя|кого мы|о тебе|условия|предлагаем|мы даём|мы даем|requirements|what we offer|бонус|плюсом", re.I)


def description(r):
    """Full vacancy text (HTML), fetched only for vacancies shown in the message."""
    if r.get("_desc"):
        return r["_desc"]
    try:
        page = fetch(r["url"])
        if r["source"] in ("rabota.by", "hh.ru"):
            m = re.search(r'id="HH-Lux-InitialState"[^>]*>(.*?)</template>', page, re.S)
            return json.loads(html.unescape(m.group(1)))["vacancyView"]["vacancyFull"]["vacancy"].get("description") or ""
        if r["source"] == "career.habr.com":
            m = re.search(r'<script type="application/json" data-ssr-state="true">(.*?)</script>', page, re.S)
            return (json.loads(m.group(1)).get("vacancy") or {}).get("description") or ""
        # designer.ru: the JobPosting JSON-LD block holds only this vacancy's text
        for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
            try:
                j = json.loads(m.group(1), strict=False)
            except ValueError:
                continue
            for x in (j if isinstance(j, list) else [j]):
                if isinstance(x, dict) and x.get("@type") == "JobPosting" and x.get("description"):
                    return x["description"]
        return ""
    except Exception:
        return ""


def duties(desc, limit=120):
    """First items of the duties section as one line."""
    if not desc:
        return ""
    t = re.sub(r"(?i)<li[^>]*>", "\n• ", desc)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</div>|<strong>|<b>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", x).strip(" ;.—-") for x in t.split("\n")]
    lines = [x for x in lines if x and x != "•"]
    for i, x in enumerate(lines):
        if DUTY_HEAD.search(x) and not STOP_HEAD.search(x) and (x.endswith((":", "?")) or len(x) <= 40):
            out = []
            for y in lines[i + 1:]:
                if STOP_HEAD.search(y) and len(y) < 80:
                    break
                y = y.lstrip("•·-–— ").strip()
                if len(y) > 15 and not y.endswith(":"):
                    out.append(y[0].lower() + y[1:] if y[:2].isalpha() and not y[:2].isupper() else y)
                if len(out) == 3:
                    break
            if out:
                s = "; ".join(out)
                return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"
    return ""


def telegram_digest(st, ins, picks, new_ok, errors):
    e = html.escape
    def card(n, r):
        where = " · ".join(x for x in [r["city"], " или ".join(r["formats"])] if x)
        pub = dt.datetime.fromisoformat(r["pub"]).astimezone()
        meta = [x for x in [f"📅 {pub:%d.%m}", f"💰 {r['salary']}" if r["salary"] else "💰 не указана",
                            f"опыт {r['exp']}" if r["exp"] and r["exp"] not in ("Senior", "Lead", "Middle", "Junior", "Intern") else "",
                            f"{r['responses']} откл." if r["responses"] else ""] if x]
        tags = [f[1] for f in r["flags"] if f[0] in ("warn", "plain")]
        doms = r.get("domains") or []
        if r["reposted"]:
            tags.append("поднята, создана " + dt.date.fromisoformat(r["created"][:10]).strftime("%d.%m"))
        out = [f'{n}. {"🆕 " if r.get("new") else ""}<b><a href="{e(r["url"])}">{e(r["name"])}</a></b> — {e(r["company"])}',
               "📍 " + e(where) if where else "", e(" · ".join(meta))]
        if r.get("co"):
            out.insert(1, "🏢 " + e(r["co"]))
        if doms:
            out.append("🎯 " + e(", ".join(doms)))
        plus = [f[1] for f in r["flags"] if f[1] in ("мало откликов", "есть вилка")]
        if plus:
            out.append("✅ " + e(", ".join(plus)))
        d = (r.get("duty") or "").rstrip("…")
        if len(d) > 100:
            d = d[:100].rsplit(" ", 1)[0] + "…"
        if d:
            out.append("🛠 " + e(d))
        if tags:
            out.append("⚠️ " + e(", ".join(tags)))
        return "\n".join(x for x in out if x)

    by = [r for r in picks if r["source"] == "rabota.by"][:5]
    other = [r for r in picks if r["source"] != "rabota.by"][:5]
    shown_new = sum(1 for r in by + other if r.get("new"))

    head = [f"<b>Вакансии на {dt.date.today():%d.%m}</b>",
            f"Новых {st['new']}, из них подходят {st['new_ok']}. За месяц {st['total']}, прочитано и подходит {st['ok']}."]
    blocks, n = [], 0
    for title, group in ((None, by), ("🌐 Удалёнка, другие площадки", other)):
        if group:
            blocks.append(["", f"<b>{title}</b>\n" if title else ""])
            for r in group:
                n += 1
                blocks[-1].append(card(n, r))
    ordered = by + other
    if st["new_ok"] > shown_new:
        blocks.append([f"…ещё {st['new_ok'] - shown_new} новых подходящих — в файле", None])
    tail = []
    keep = [x for x in ins if x.startswith(("Мидл", "Сегодня новых", "Это первый"))]
    if keep:
        tail += ["", "<b>Рынок</b>"] + [f"• {e(x)}" for x in keep]
    if errors:
        tail += ["", "Не ответили: " + e(", ".join(sorted({x.split(' ')[0].rstrip(':') for x in errors})))]
    tail += ["", "Полный список с фильтрами — в файле ниже."]

    def build():
        parts = ["\n".join(head)]
        for b in blocks:
            parts.append(b[1] + "\n\n".join(b[2:]) if b[0] == "" else b[0])
        parts.append("\n".join(tail).strip("\n"))
        return "\n\n".join(parts)
    text = build()
    while len(html.unescape(re.sub(r"<[^>]+>", "", text))) > 4000:  # Telegram counts visible text, not markup  # Telegram limit is 4096: drop the last entries
        for b in reversed(blocks):
            if len(b) > 2:
                b.pop()
                if len(b) == 2:
                    blocks.remove(b)
                break
        else:
            break
        text = build()
    kept = sum(len(b) - 2 for b in blocks if b[0] == "")
    return text, ordered[:kept]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--telegram", action="store_true", help="отправить сводку в Telegram")
    ap.add_argument("--poll", action="store_true", help="only read Telegram marks (buttons, replies) and save them")
    args = ap.parse_args()
    state = load_state()
    if args.poll or args.telegram:
        conf = tg_conf()
        if conf and state_key():
            try:
                print(f"  marks from Telegram: {poll_marks(conf, state)}", file=sys.stderr)
                save_state(state)
            except Exception as ex:
                print("  reading Telegram marks failed:", ex, file=sys.stderr)
        elif not state_key():
            print("  no STATE_KEY: buttons and marks are off", file=sys.stderr)
    if args.poll:
        return
    rates, rate_note = usd_rates()
    found, errors = {}, []
    for host, area, extra, region in SOURCES:
        for q in QUERIES:
            try:
                items = search(host, area, extra, q, args.days)
            except Exception as e:
                errors.append(f"{host} «{q}»: {e}")
                continue
            for v in items:
                if not TITLE_OK.search(v["name"]) or TITLE_SKIP.search(v["name"]):
                    continue
                if str(v["vacancyId"]) not in found:
                    found[str(v["vacancyId"])] = normalize(v, region, rates)
            print(f"  {host:10} {q:22} {len(items):3}", file=sys.stderr)
            time.sleep(0.5)

    try:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
        gm = getmatch_all()
        n = 0
        for v in gm:
            if not v.get("is_active", True) or not TITLE_OK.search(v["position"]) or TITLE_SKIP.search(v["position"]):
                continue
            r = normalize_getmatch(v, rates)
            if dt.datetime.fromisoformat(r["pub"]) >= cutoff:
                found[r["id"]] = r
                n += 1
        print(f"  getmatch.ru  total {len(gm)}, design {n}", file=sys.stderr)
    except Exception as e:
        errors.append(f"getmatch.ru: {e}")

    try:
        hb, n = habr_all(args.days), 0
        for v in hb:
            if TITLE_OK.search(v["title"]) and not TITLE_SKIP.search(v["title"]):
                r = normalize_habr(v, rates)
                found[r["id"]] = r
                n += 1
        print(f"  career.habr  total {len(hb)}, design {n}", file=sys.stderr)
    except Exception as e:
        errors.append(f"career.habr.com: {e}")

    try:
        dr, n = designer_ru(args.days), 0
        for v in dr:
            r = normalize_designer_ru(v)
            if TITLE_OK.search(r["name"]) and not TITLE_SKIP.search(r["name"]):
                found[r["id"]] = r
                n += 1
        print(f"  designer.ru  total {len(dr)}, matching titles {n}", file=sys.stderr)
    except Exception as e:
        errors.append(f"designer.ru: {e}")

    uniq = {}
    for k, r in sorted(found.items(), key=lambda kv: kv[1]["pub"], reverse=True):
        uniq.setdefault((r["company"].lower(), r["name"].lower().strip()), (k, r))
    found = dict(uniq.values())
    mark_companies(found, load_companies())
    apply_marks(found, state)
    n = read_all(found)
    print(f"  looked up {read_companies(found)} new companies", file=sys.stderr)
    for r in found.values():
        if r["responses"] is not None and r["responses"] < FEW_RESPONSES:
            r["flags"].append(("good", "мало откликов"))
        if r["salary"]:
            r["flags"].append(("good", "есть вилка"))
    print(f"  read {n} new vacancy texts", file=sys.stderr)

    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    now = dt.datetime.now().astimezone().isoformat(timespec="minutes")
    first_run = not seen
    for vid, v in found.items():
        v["new"] = (not first_run) and vid not in seen
        seen.setdefault(vid, now)
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=0))

    rows = sorted(found.values(), key=lambda v: v["pub"], reverse=True)
    st, ins, picks, hist, new_ok = analyze(rows, errors)
    write_digest(st, ins, picks, new_ok, errors)
    # the page must exist before it is sent as a file
    OUT.write_text(render(rows, rate_note, errors, args.days, ins, picks, hist), encoding="utf-8")
    print(f"Done: {len(rows)} vacancies, {sum(v['new'] for v in rows)} new -> {OUT}", file=sys.stderr)
    for e in errors:
        print("  error:", e, file=sys.stderr)
    if args.telegram:
        conf = tg_conf()
        if not conf:
            print("  Telegram send failed: not configured (no TELEGRAM_TOKEN/TELEGRAM_CHAT_ID)", file=sys.stderr)
            sys.exit(2)
        try:
            text, shown = telegram_digest(st, ins, picks, new_ok, errors)
            tg_send(text, conf, keyboard(shown) if state_key() else None)
            if state_key():
                remember_shown(state, shown)
                save_state(state)
            tg_send_file(OUT, f"Все вакансии на {dt.date.today():%d.%m}", conf)
            print("  digest sent to Telegram", file=sys.stderr)
        except Exception as ex:
            # non-zero exit: the workflow does not mark the day as sent, so a fallback run retries
            print("  Telegram send failed:", ex, file=sys.stderr)
            sys.exit(2)


def render(rows, rate_note, errors, days, ins, picks, hist):
    data = json.dumps([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
                      ensure_ascii=False).replace("</", "<\\/")
    pick_html = "".join(f'<li><a href="{html.escape(r["url"])}" target="_blank" rel="noopener">{html.escape(r["name"])}</a>'
                        f' <span class="muted">— {html.escape(r["company"])}{" · " + html.escape(r["salary"]) if r["salary"] else ""}'
                        f'{" · " + str(r["responses"]) + " откл." if r["responses"] else ""}</span></li>' for r in picks[:10])
    spark = ""
    if len(hist) >= 2:
        h = hist[-30:]; mx = max(x["new"] for x in h) or 1
        bars = "".join(f'<span title="{x["date"]}: {x["new"]} новых" style="height:{max(2, round(32 * x["new"] / mx))}px"></span>' for x in h)
        spark = f'<div class="spark-wrap"><div class="spark">{bars}</div><span class="muted">новые по дням, {len(h)} дн.</span></div>'
    summary = (f'<section class="summary"><div><h2>Выводы</h2><ul>{"".join("<li>" + html.escape(x) + "</li>" for x in ins)}</ul>{spark}</div>'
               f'<div><h2>Откликнуться в первую очередь</h2><ol>{pick_html}</ol></div></section>')
    stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    err = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    return TEMPLATE.replace("{{DATA}}", data).replace("{{STAMP}}", stamp).replace("{{RATE}}", html.escape(rate_note)) \
        .replace("{{DAYS}}", str(days)).replace("{{SUMMARY}}", summary).replace("{{ERRORS}}", f'<ul class="errors">{err}</ul>' if err else "")


TEMPLATE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вакансии дизайнера</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1b1a;--muted:#6f6b66;--line:#e7e4e0;--link:#2556c4;--card:#fff;
--good:#1f7a3f;--good-bg:#e6f4ea;--warn:#8a5a00;--warn-bg:#fdf1d8;--bad:#a8261b;--bad-bg:#fbe5e2;--new:#2556c4;--new-bg:#e5ecfb}
@media (prefers-color-scheme:dark){:root{--bg:#161514;--fg:#ecebe8;--muted:#9a958f;--line:#2c2a28;--link:#8fb0ff;--card:#1e1d1b;
--good:#7fd69b;--good-bg:#1d3324;--warn:#f0c26b;--warn-bg:#3a2e16;--bad:#ff9a8f;--bad-bg:#3d1f1c;--new:#8fb0ff;--new-bg:#1e2a45}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter",system-ui,sans-serif}
main{max-width:1040px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:24px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin:0 0 24px}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:24px;align-items:center}
.bar input{flex:1 1 200px;min-width:0;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit}
.chip{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}
.chip[aria-pressed=true]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.stats{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:24px}
.stat b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.stat span{color:var(--muted);font-size:13px}
h2{font-size:17px;margin:32px 0 8px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:13px;color:var(--muted);font-weight:600;padding:8px 8px 8px 0;border-bottom:1px solid var(--line)}
td{padding:12px 8px 12px 0;border-bottom:1px solid var(--line);vertical-align:top}
td.date{white-space:nowrap;font-variant-numeric:tabular-nums;width:72px;color:var(--muted)}
td.date b{color:var(--fg);font-weight:500}
a{color:var(--link);text-decoration-thickness:1px;text-underline-offset:3px}
.role a{font-weight:500}
.meta{color:var(--muted);font-size:13px;margin-top:4px}
.co{width:32%}.co .sal{font-variant-numeric:tabular-nums;margin-top:4px;font-size:13px}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.tag{font-size:12px;padding:2px 8px;border-radius:999px;white-space:nowrap}
.tag.good{color:var(--good);background:var(--good-bg)}.tag.warn{color:var(--warn);background:var(--warn-bg)}
.tag.bad{color:var(--bad);background:var(--bad-bg)}.tag.new{color:var(--new);background:var(--new-bg);font-weight:600}
.tag.plain{color:var(--muted);background:transparent;border:1px solid var(--line)}
tr.bad td{opacity:.55}
.note{color:var(--muted);font-size:13px;margin-top:32px;max-width:720px}
.errors{color:var(--bad);font-size:13px}
.empty{color:var(--muted);padding:24px 0}
.muted{color:var(--muted)}
.summary{display:grid;grid-template-columns:1fr 1fr;gap:32px;padding:20px 24px;background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:32px}
.summary h2{margin:0 0 8px;font-size:15px}.summary ul,.summary ol{margin:0;padding-left:20px}.summary li{margin-bottom:6px;font-size:14px}
.spark-wrap{display:flex;align-items:end;gap:12px;margin-top:16px;font-size:12px}
.spark{display:flex;align-items:end;gap:2px;height:32px}.spark span{width:6px;background:var(--link);border-radius:2px;opacity:.7}
@media (max-width:800px){.summary{grid-template-columns:1fr;padding:16px}}
@media (max-width:640px){thead{display:none}tr{display:grid;grid-template-columns:64px 1fr;border-bottom:1px solid var(--line);padding:12px 0}
td{border:0;padding:0 8px 0 0}td.co{grid-column:2;width:auto;margin-top:6px}}
</style></head><body><main>
<h1>Вакансии дизайнера</h1>
<p class="sub">Собрано {{STAMP}} · rabota.by, удалёнка с hh.ru, getmatch.ru, Хабр Карьера и designer.ru за {{DAYS}} дней · {{RATE}}</p>
{{ERRORS}}
<div class="stats" id="stats"></div>
{{SUMMARY}}
<div class="bar">
  <input id="q" type="search" placeholder="Поиск по роли или компании">
  <button class="chip" data-f="fit" aria-pressed="true">Без красных</button>
  <button class="chip" data-f="new" aria-pressed="false">Только новые</button>
  <button class="chip" data-f="fresh" aria-pressed="false">Без поднятых</button>
  <button class="chip" data-f="known" aria-pressed="false">Мои компании</button>
  <select id="src" class="chip"><option value="">Все источники</option></select>
</div>
<div id="out"></div>
<p class="note"><b>Поднятая</b> — вакансию создали раньше, чем показывает дата: работодатель перепубликовал её, чтобы поднять в выдаче. Дата создания берётся с сайта, а не угадывается по номеру.
<b>Без красных</b> скрывает то, что не подходит по заданным критериям — красная метка на карточке всегда говорит, по какому именно. Текст каждой вакансии прочитан, зелёные «✓» — совпадения с доменами из настроек. Жёлтые метки — повод прочитать внимательнее, а не отказ.
<b>Мои компании</b> — те, что заданы в настройках: отказ, отклик, собеседование.</p>
<script>
const rows = {{DATA}};
const st = {fit:true,new:false,fresh:false,known:false,src:"",q:""};
const M = ["янв","фев","мар","апр","мая","июн","июл","авг","сен","окт","ноя","дек"];
const d = s => new Date(s);
const fmt = s => { const x=d(s); return x.getDate()+" "+M[x.getMonth()]; };
const esc = s => String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function weekStart(x){const t=new Date(x);t.setHours(0,0,0,0);t.setDate(t.getDate()-((t.getDay()+6)%7));return t;}
function group(x){
  const now=weekStart(new Date()), w=weekStart(x), diff=Math.round((now-w)/864e5/7);
  const end=new Date(w);end.setDate(w.getDate()+6);
  const range=w.getDate()+(w.getMonth()===end.getMonth()?"":" "+M[w.getMonth()])+"–"+end.getDate()+" "+M[end.getMonth()];
  return [w.getTime(), diff===0?"Эта неделя ("+range+")":diff===1?"Прошлая неделя ("+range+")":range];
}
function visible(){
  const q=st.q.toLowerCase();
  return rows.filter(r=>(!st.fit||r.fit!=="bad")&&(!st.new||r.new)&&(!st.fresh||!r.reposted)
    &&(!st.src||r.source===st.src)&&(!st.known||r.status)&&(!q||(r.name+" "+r.company).toLowerCase().includes(q)));
}
function plural(n,a,b,c){n=Math.abs(n)%100;const m=n%10;return n>10&&n<20?c:m===1?a:m>=2&&m<=4?b:c;}
function render(){
  const v=visible();
  const s=[[rows.length,"всего"],[rows.filter(r=>r.fit!=="bad").length,"подходят"],[rows.filter(r=>r.new).length,"новых с прошлого раза"],[rows.filter(r=>r.reposted).length,"поднятых"]];
  document.getElementById("stats").innerHTML=s.map(([n,l])=>`<div class="stat"><b>${n}</b><span>${l}</span></div>`).join("");
  if(!v.length){document.getElementById("out").innerHTML='<p class="empty">Ничего не нашлось с такими фильтрами.</p>';return;}
  const g=new Map();
  v.forEach(r=>{const [k,l]=group(d(r.pub));if(!g.has(k))g.set(k,{l,items:[]});g.get(k).items.push(r);});
  let h="";
  [...g.entries()].sort((a,b)=>b[0]-a[0]).forEach(([,{l,items}])=>{
    h+=`<h2>${l}, ${items.length} ${plural(items.length,"вакансия","вакансии","вакансий")}</h2><table><thead><tr><th>Дата</th><th>Роль</th><th>Компания</th></tr></thead><tbody>`;
    items.forEach(r=>{
      const tags=[];
      if(r.new)tags.push(`<span class="tag new">новая</span>`);
      if(r.reposted)tags.push(`<span class="tag plain">поднята · создана ${fmt(r.created)}</span>`);
      r.flags.forEach(([lvl,t])=>tags.push(`<span class="tag ${lvl}">${esc(t)}</span>`));
      if(r.test)tags.push(`<span class="tag plain">тест при отклике</span>`);
      if(r.letter)tags.push(`<span class="tag plain">нужно письмо</span>`);
      const meta=[r.formats.join(", "),r.city,r.exp&&"опыт "+r.exp,r.emp&&r.emp!=="полная"?r.emp:"",r.responses?r.responses+" откликов":"",r.source].filter(Boolean).join(" · ");
      h+=`<tr class="${r.fit}"><td class="date"><b>${fmt(r.pub)}</b></td>
        <td class="role"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a><div class="meta">${esc(meta)}</div><div class="tags">${tags.join("")}</div></td>
        <td class="co">${esc(r.company)}${r.co?`<div class="meta">${esc(r.co)}</div>`:""}${r.salary?`<div class="sal">${esc(r.salary)}</div>`:""}</td></tr>`;
    });
    h+="</tbody></table>";
  });
  document.getElementById("out").innerHTML=h;
}
document.querySelectorAll(".chip").forEach(b=>b.onclick=()=>{const f=b.dataset.f;st[f]=!st[f];b.setAttribute("aria-pressed",st[f]);render();});
const sel=document.getElementById("src");[...new Set(rows.map(r=>r.source))].sort().forEach(x=>{const o=document.createElement("option");o.value=x;o.textContent=x+" · "+rows.filter(r=>r.source===x).length;sel.append(o);});
sel.onchange=e=>{st.src=e.target.value;render();};
document.getElementById("q").oninput=e=>{st.q=e.target.value;render();};
render();
</script></main></body></html>"""

if __name__ == "__main__":
    main()
