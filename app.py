"""
مقارنة أسعار العسل — Honey Price Tracker
تشغيل محلي:  streamlit run app.py
"""
import re
import json
import os
import datetime
import html
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urljoin, unquote

import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ إعدادات
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.9",
}
TIMEOUT = 20
URLS_FILE = "urls.txt"
HISTORY_FILE = "prices_history.csv"

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")
CURRENCY = r"(?:EGP|L\.?E|ج\.?\s?م\.?|جنيه(?:ا|ًا)?|جنية)"
NUM = r"([\d٠-٩][\d٠-٩.,٬٫]*)"
PRICE_RE = re.compile(rf"(?:{CURRENCY}\s*{NUM}|{NUM}\s*{CURRENCY})", re.I)
WEIGHT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(كيلوجرام|كيلو|كجم|كغ|kg|kilo|جرام|غرام|جم|gram|gm|g)(?![a-zA-Z])",
    re.I,
)
KG_UNITS = {"كيلوجرام", "كيلو", "كجم", "كغ", "kg", "kilo"}


# ------------------------------------------------------------------ أدوات مساعدة
def to_number(text):
    """يحوّل نصاً (بأرقام عربية أو إنجليزية) إلى رقم."""
    if text is None:
        return None
    s = str(text).translate(AR_DIGITS)
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", "") if len(parts[-1]) == 3 else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_weight_kg(text):
    """يستخرج الوزن بالكيلو من اسم المنتج (مثل: 500 جرام، 1 كيلو، نصف كيلو)."""
    if not text:
        return None
    t = str(text).translate(AR_DIGITS)
    if re.search(r"نص(?:ف)?\s*كيلو", t):
        return 0.5
    if re.search(r"ربع\s*كيلو", t):
        return 0.25
    m = WEIGHT_RE.search(t)
    if not m:
        return None
    value = to_number(m.group(1))
    if not value:
        return None
    return value if m.group(2).lower() in KG_UNITS else value / 1000


def get_title(soup):
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return soup.title.get_text(strip=True) if soup.title else ""


# ------------------------------------------------------------------ طرق الاستخراج
def try_shopify(url):
    """متاجر Shopify: نقرأ بيانات المنتج مباشرة بكل أحجامه."""
    p = urlparse(url)
    if "/products/" not in p.path:
        return None
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}.js",
                         headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    rows = []
    for v in data.get("variants", []):
        variant = v.get("title") or ""
        if variant == "Default Title":
            variant = ""
        weight = parse_weight_kg(variant) or parse_weight_kg(data.get("title"))
        if not weight and v.get("grams"):
            weight = v["grams"] / 1000
        rows.append({
            "product": data.get("title", ""),
            "variant": variant,
            "price": v.get("price", 0) / 100,
            "available": v.get("available"),
            "weight_kg": weight,
            "method": "Shopify",
        })
    return rows or None


def walk_jsonld(node):
    if isinstance(node, list):
        for item in node:
            yield from walk_jsonld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "hasVariant", "mainEntity"):
            if key in node:
                yield from walk_jsonld(node[key])


def from_jsonld(soup):
    rows = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        for node in walk_jsonld(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            offers = node.get("offers") or []
            offers = offers if isinstance(offers, list) else [offers]
            for off in offers:
                if not isinstance(off, dict):
                    continue
                price = to_number(off.get("price") or off.get("lowPrice"))
                if not price:
                    continue
                avail = str(off.get("availability", ""))
                name = node.get("name", "")
                rows.append({
                    "product": name,
                    "variant": off.get("name", "") if off.get("name") != name else "",
                    "price": price,
                    "available": ("InStock" in avail) if avail else None,
                    "weight_kg": parse_weight_kg(off.get("name")) or parse_weight_kg(name),
                    "method": "JSON-LD",
                })
    return rows or None


def from_meta(soup):
    for attrs in ({"property": "product:price:amount"}, {"property": "og:price:amount"},
                  {"itemprop": "price"}):
        tag = soup.find(attrs=attrs)
        if tag:
            price = to_number(tag.get("content") or tag.get_text())
            if price:
                return [{"price": price, "method": "Meta"}]
    return None


def from_text(soup):
    """الحل الأخير: نبحث عن السعر في عناصر الصفحة أو نصها."""
    for sel in (".price ins .amount", ".price .amount", "[class*=price]", "[id*=price]"):
        for el in soup.select(sel)[:5]:
            price = to_number(el.get_text(" ", strip=True))
            if price and price > 1:
                return [{"price": price, "method": "HTML"}]
    m = PRICE_RE.search(soup.get_text(" ", strip=True))
    if m:
        price = to_number(m.group(1) or m.group(2))
        if price:
            return [{"price": price, "method": "Text"}]
    return None


def base_row(url):
    return {"site": urlparse(url).netloc.replace("www.", ""), "url": url, "product": "",
            "variant": "", "price": None, "available": None, "weight_kg": None,
            "method": "", "error": ""}


def scrape_product(url):
    """يستخرج السعر من صفحة منتج واحدة."""
    base = base_row(url)
    rows = try_shopify(url)
    if not rows:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            return [{**base, "error": f"تعذّر فتح الصفحة: {e}"}]
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
            r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        title = get_title(soup)
        rows = from_jsonld(soup) or from_meta(soup) or from_text(soup)
        if not rows:
            return [{**base, "product": title,
                     "error": "لم يُعثر على سعر (قد تُحمّل الصفحة السعر بجافاسكربت)"}]
        for row in rows:
            row["product"] = row.get("product") or title
            row["weight_kg"] = row.get("weight_kg") or parse_weight_kg(row["product"])
    return [{**base, **row} for row in rows]


# ------------------------------------------------------------------ البحث في الموقع كله
def site_root(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


EXCLUDE = []  # تُملأ من الواجهة


def normalize(text):
    """يوحّد الكتابة العربية: ى/ي، ة/ه، أ/إ/آ/ا، ويحذف التشكيل."""
    t = unquote(str(text)).lower()
    t = re.sub(r"[\u064B-\u0652\u0640]", "", t)
    return (t.replace("ى", "ي").replace("ة", "ه")
            .replace("أ", "ا").replace("إ", "ا").replace("آ", "ا"))


def matches(text, keywords):
    t = normalize(text)
    if any(x in t for x in EXCLUDE):
        return False
    return not keywords or any(k in t for k in keywords)


def get_json(url, params=None):
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def scan_shopify(root, keywords):
    """متاجر Shopify: كل المنتجات متاحة في /products.json"""
    rows, is_shopify = [], False
    for page in range(1, 6):
        data = get_json(f"{root}/products.json", {"limit": 250, "page": page})
        if not isinstance(data, dict) or "products" not in data:
            break
        is_shopify = True
        for p in data["products"]:
            tags = p.get("tags", [])
            tags = " ".join(tags) if isinstance(tags, list) else str(tags)
            if not matches(f"{p.get('title', '')} {p.get('handle', '')} "
                           f"{p.get('product_type', '')} {tags}", keywords):
                continue
            for v in p.get("variants", []):
                variant = "" if v.get("title") == "Default Title" else (v.get("title") or "")
                weight = parse_weight_kg(variant) or parse_weight_kg(p.get("title"))
                if not weight and v.get("grams"):
                    weight = v["grams"] / 1000
                rows.append({"product": p.get("title", ""), "variant": variant,
                             "price": to_number(v.get("price")), "available": v.get("available"),
                             "weight_kg": weight, "url": f"{root}/products/{p.get('handle')}",
                             "method": "Shopify"})
        if len(data["products"]) < 250:
            break
    return rows if is_shopify else None


def scan_woocommerce(root, keywords):
    """متاجر WooCommerce (ووردبريس): واجهة Store API العامة"""
    endpoint = None
    for ep in ("/wp-json/wc/store/v1/products", "/wp-json/wc/store/products"):
        if isinstance(get_json(root + ep, {"per_page": 1}), list):
            endpoint = root + ep
            break
    if not endpoint:
        return None
    rows = []
    for page in range(1, 6):
        data = get_json(endpoint, {"per_page": 100, "page": page})
        if not isinstance(data, list) or not data:
            break
        for p in data:
            name = html.unescape(p.get("name", ""))
            cats = " ".join(c.get("name", "") for c in p.get("categories", []))
            if not matches(f"{name} {p.get('slug', '')} {cats}", keywords):
                continue
            prices = p.get("prices", {})
            price = to_number(prices.get("price"))
            if price is not None:
                price = price / 10 ** int(prices.get("currency_minor_unit", 0) or 0)
            rows.append({"product": name, "variant": "", "price": price,
                         "available": p.get("is_in_stock"), "weight_kg": parse_weight_kg(name),
                         "url": p.get("permalink", root), "method": "WooCommerce"})
        if len(data) < 100:
            break
    return rows


LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)


def sitemap_urls(root):
    """يقرأ خريطة الموقع (sitemap) ويجمع كل روابط الصفحات."""
    queue = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]
    try:
        robots = requests.get(f"{root}/robots.txt", headers=HEADERS, timeout=TIMEOUT).text
        queue = re.findall(r"(?im)^sitemap:\s*(\S+)", robots) + queue
    except Exception:
        pass
    seen, pages, fetched = set(), [], 0
    while queue and fetched < 15:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        fetched += 1
        try:
            r = requests.get(sm, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        for loc in LOC_RE.findall(r.text):
            loc = html.unescape(loc)
            if re.search(r"\.xml(\.gz)?(\?|$)", loc):
                (queue.insert(0, loc) if "product" in loc.lower() else queue.append(loc))
            else:
                pages.append(loc)
    return list(dict.fromkeys(pages))


def search_page_links(root, keywords):
    """الحل الأخير: نستخدم صفحة البحث في الموقع نفسه."""
    links = []
    for kw in keywords or ["عسل"]:
        for path in (f"/search?q={kw}", f"/?s={kw}&post_type=product", f"/search?type=product&q={kw}"):
            try:
                r = requests.get(root + path, headers=HEADERS, timeout=TIMEOUT)
                if r.status_code != 200:
                    continue
                r.encoding = r.apparent_encoding or "utf-8"
            except Exception:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(root, a["href"]).split("#")[0]
                if (urlparse(href).netloc == urlparse(root).netloc
                        and "search" not in href and "?s=" not in href
                        and re.search(r"/products?/", href)
                        and matches(href + " " + a.get_text(" "), keywords)):
                    links.append(href)
            if links:
                return list(dict.fromkeys(links))
    return []


@st.cache_data(ttl=3600, show_spinner=False)
def scan_site(url, keywords, max_pages, exclude=()):
    """يبحث عن منتجات العسل في الموقع كله ويعيد أسعارها."""
    if not url.startswith("http"):
        url = "https://" + url
    root = site_root(url)
    keywords = [normalize(k) for k in keywords]
    EXCLUDE[:] = [normalize(x) for x in exclude]

    # رابط منتج مباشر ما زال مقبولاً
    if re.search(r"/products?/[^/]+", urlparse(url).path):
        return scrape_product(url)

    rows = scan_shopify(root, keywords)
    if rows is None:
        rows = scan_woocommerce(root, keywords)
    if rows is None:
        pages = [u for u in sitemap_urls(root) if matches(u, keywords)]
        if not pages:
            pages = search_page_links(root, keywords)
        pages = pages[:max_pages]
        rows = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            for res in ex.map(scrape_product, pages):
                rows.extend(r for r in res if r.get("price"))

    if not rows:
        return [{**base_row(root), "error": "لم أجد منتجات مطابقة في هذا الموقع"}]
    return [{**base_row(r.get("url", root)), **r} for r in rows]


# ------------------------------------------------------------------ حفظ البيانات
def load_urls():
    if os.path.exists(URLS_FILE):
        with open(URLS_FILE, encoding="utf-8") as f:
            return f.read()
    return ""


def save_urls(text):
    with open(URLS_FILE, "w", encoding="utf-8") as f:
        f.write(text)


# الترتيب مهم: الأصناف الأدق أولاً
HONEY_TYPES = [
    ("كشميري", ["كشميري", "kashmir"]),
    ("دوعني", ["دوعني", "دوعن", "dawani", "doani", "dawan"]),
    ("حضرمي", ["حضرمي", "hadrami"]),
    ("سدر سعودي / يمني", ["سعودي", "يمني", "saudi", "yemen"]),
    ("سدر", ["سدر", "sidr", "sidir"]),
    ("مانوكا", ["مانوكا", "manuka"]),
    ("سمر", ["سمر ", "سمره", "samar"]),
    ("طلح", ["طلح", "talh"]),
    ("حبة البركة", ["حبه البركه", "black seed", "nigella"]),
    ("بردقوش", ["بردقوش", "مردقوش", "marjoram"]),
    ("موالح", ["موالح", "برتقال", "حمضيات", "citrus", "orange"]),
    ("برسيم", ["برسيم", "clover"]),
    ("قطن", ["قطن", "cotton"]),
    ("كينا", ["كينا", "كافور", "eucalyptus"]),
    ("زعتر", ["زعتر", "thyme"]),
    ("زهور برية", ["زهور", "wildflower", "wild flower", "multiflora"]),
    ("عسل شمع", ["شمع", "شهد", "قرص", "comb"]),
    ("جبلي", ["جبلي", "mountain"]),
]
# منتجات مخلوطة (عسل + إضافات) تظهر باسمها حتى لا تُحسب ضمن صنف آخر
MIX_WORDS = ["غذاء الملكات", "حبوب اللقاح", "طلع النخيل", "مكسرات", "خلطه",
             "royal jelly", "pollen", "ginseng", "جنسنج"]


def clean_name(name):
    """اسم المنتج بدون الوزن والكلمات العامة، ليُستخدم كصنف إذا لم يكن معروفاً."""
    t = str(name).translate(AR_DIGITS)
    t = WEIGHT_RE.sub("", t)
    t = re.sub(r"[()\[\]\-–|/]+", " ", t)
    t = re.sub(r"\b(honey|bee|natural|pure)\b", "", t, flags=re.I)
    t = re.sub(r"(^|\s)(عسل|نحل|طبيعي|اصلي|أصلي)(?=\s|$)", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return f"عسل {t}" if t else "عسل نحل (غير محدد الصنف)"


def honey_type(product, variant=""):
    """يحدد صنف العسل من اسم المنتج."""
    t = f" {normalize(product)} {normalize(variant)} "
    if any(normalize(w) in t for w in MIX_WORDS):
        return clean_name(product)
    for name, words in HONEY_TYPES:
        if any(normalize(w) in t for w in words):
            return name
    return clean_name(product)


def summarize(df):
    """يحوّل المنتجات إلى جدول: صنف العسل + المتجر + أقل سعر للكيلو."""
    d = df.copy()
    d["price"] = pd.to_numeric(d["price"], errors="coerce")
    d["weight_kg"] = pd.to_numeric(d["weight_kg"], errors="coerce")
    d = d[d["price"].notna() & d["weight_kg"].notna() & (d["weight_kg"] > 0)
          & (d["available"] != False)]  # noqa: E712
    if d.empty:
        return pd.DataFrame(columns=["type", "site", "product", "price_per_kg"])
    d["price_per_kg"] = (d["price"] / d["weight_kg"]).round(0)
    d["type"] = [honey_type(p, v) for p, v in zip(d["product"].fillna(""), d["variant"].fillna(""))]
    d = d.sort_values("price_per_kg").drop_duplicates(["type", "site"])
    return d[["type", "site", "product", "price_per_kg"]]


def append_history(table):
    snap = table.copy()
    snap.insert(0, "date", datetime.date.today().isoformat())
    if os.path.exists(HISTORY_FILE):
        old = pd.read_csv(HISTORY_FILE)
        if "type" in old.columns:
            snap = pd.concat([old, snap]).drop_duplicates(
                subset=["date", "type", "site"], keep="last")
    snap.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")
    return snap


# ------------------------------------------------------------------ الواجهة
st.set_page_config(page_title="مقارنة أسعار العسل", page_icon="🍯", layout="wide")
st.markdown(
    """<style>
    .stApp, .stMarkdown, .stTextArea, .stButton, h1, h2, h3, p, label {direction: rtl; text-align: right;}
    textarea, [data-testid="stDataFrame"] {direction: ltr; text-align: left;}
    </style>""",
    unsafe_allow_html=True,
)

st.title("🍯 مقارنة أسعار العسل")
st.caption("ضع رابط كل متجر في سطر، وسيبحث التطبيق عن أصناف العسل ويحسب سعر الكيلو لكل صنف، حتى لو كانت العبوة أصغر من كيلو.")

urls_text = st.text_area("روابط المتاجر", value=load_urls(), height=150,
                         placeholder="https://honey-store.com")
with st.expander("⚙️ إعدادات البحث"):
    kw_text = st.text_input("كلمات البحث (افصل بينها بفاصلة)", value="عسل, honey")
    ex_text = st.text_input("استبعد المنتجات التي تحتوي على",
                            value="صابون, كريم, شامبو, ماسك, حلاوة, بسكويت, للشعر, للبشرة")
    max_pages = st.slider("أقصى عدد صفحات يُفحص في المواقع غير المعروفة", 10, 150, 50)
def split_words(text):
    return tuple(w.strip() for w in text.replace("،", ",").split(",") if w.strip())


keywords, exclude = split_words(kw_text), split_words(ex_text)

c1, c2 = st.columns([1, 1])
run = c1.button("🔍 استخراج الأسعار", type="primary", use_container_width=True)
if c2.button("🔄 تحديث (تجاهل الذاكرة المؤقتة)", use_container_width=True):
    scan_site.clear()
    run = True

if run:
    urls = [u.strip() for u in urls_text.splitlines() if u.strip().startswith("http")]
    if not urls:
        st.warning("أضف رابط متجر واحد على الأقل يبدأ بـ http")
        st.stop()
    save_urls("\n".join(urls))

    results, bar = [], st.progress(0.0, text="جاري البحث في المتاجر...")
    for i, url in enumerate(urls, 1):
        bar.progress((i - 1) / len(urls), text=f"جاري البحث في {urlparse(url).netloc} ({i} من {len(urls)})")
        results.extend(scan_site(url, keywords, max_pages, exclude))
    bar.empty()

    df = pd.DataFrame(results)
    st.session_state["errors"] = df[df["error"].astype(bool)]["site"].unique().tolist()
    st.session_state["no_weight"] = 0
    table = summarize(df)
    sold = df[df["available"] == False]  # noqa: E712
    sold_types = {honey_type(p, v) for p, v in zip(sold["product"].fillna(""), sold["variant"].fillna(""))}
    st.session_state["sold_out"] = sorted(sold_types - set(table["type"]))
    st.session_state["table"] = table
    st.session_state["no_weight"] = int(
        (pd.to_numeric(df["price"], errors="coerce").notna()
         & pd.to_numeric(df["weight_kg"], errors="coerce").isna()).sum())
    st.session_state["history"] = append_history(table)

table = st.session_state.get("table")
if table is not None:
    if table.empty:
        st.warning("لم أستطع حساب سعر الكيلو لأي صنف في هذه المتاجر.")
    else:
        best = (table.sort_values("price_per_kg")
                .groupby("type", as_index=False).first()
                .sort_values("price_per_kg"))
        st.subheader("🍯 أفضل سعر للكيلو لكل صنف")
        st.dataframe(
            best[["type", "price_per_kg", "site", "product"]],
            use_container_width=True, hide_index=True,
            column_config={
                "type": "صنف العسل",
                "price_per_kg": st.column_config.NumberColumn("سعر الكيلو (ج.م)", format="%.0f"),
                "site": "المتجر",
                "product": "اسم المنتج في المتجر",
            },
        )

        if table["site"].nunique() > 1:
            st.subheader("المقارنة بين المتاجر (سعر الكيلو بالجنيه)")
            pivot = table.pivot_table(index="type", columns="site",
                                      values="price_per_kg", aggfunc="min")
            pivot = pivot.loc[pivot.min(axis=1).sort_values().index]
            st.dataframe(pivot.style.format("{:,.0f}", na_rep="—")
                         .highlight_min(axis=1, color="#c8e6c9"),
                         use_container_width=True)

    if st.session_state.get("sold_out"):
        st.caption("أصناف وجدتها لكنها غير متوفرة حالياً: " + "، ".join(st.session_state["sold_out"]))
    if st.session_state.get("errors"):
        st.caption("لم أجد أسعاراً في: " + "، ".join(st.session_state["errors"]))
    if st.session_state.get("no_weight"):
        st.caption(f"تم تجاهل {st.session_state['no_weight']} منتج لأن وزنه غير مكتوب، "
                   "فلا يمكن حساب سعر الكيلو له.")

hist = st.session_state.get("history")
if hist is None and os.path.exists(HISTORY_FILE):
    hist = pd.read_csv(HISTORY_FILE)
if hist is not None and not hist.empty and "type" in hist.columns:
    st.subheader("📈 تغيّر أفضل سعر للكيلو مع الوقت")
    if hist["date"].nunique() > 1:
        st.line_chart(hist.pivot_table(index="date", columns="type",
                                       values="price_per_kg", aggfunc="min"))
    else:
        st.caption("سيظهر الرسم البياني بعد تشغيل التطبيق في أكثر من يوم.")
    st.download_button("⬇️ تحميل سجل الأسعار (CSV)",
                       hist.to_csv(index=False).encode("utf-8-sig"),
                       "honey_prices_history.csv", "text/csv")
