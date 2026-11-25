"""Countries for Meta ad set location targeting: the one list (English name + ISO 3166-1 alpha-2 code) that the
engine checks against and the UI picker shows (served by GET /api/meta/countries). No network.

Also the location rules Studio applies before anything reaches Meta (meta_ads.check and the live plan both call
geo_problems), so the MCP and the UI get the same answers:
- only real country codes;
- no country Meta does not deliver ads to (RESTRICTED);
- no country that overlaps a region / city / postcode the ad set already targets in that same country, or that the ad
  set excludes (Meta refuses overlapping locations);
- at least one location left after the change.
"""
import unicodedata

# ISO 3166-1 alpha-2, English short names. XK (Kosovo) is not an official ISO code but Meta targets it under XK.
COUNTRIES = (
    ("AF", "Afghanistan"), ("AX", "Åland Islands"), ("AL", "Albania"), ("DZ", "Algeria"), ("AS", "American Samoa"),
    ("AD", "Andorra"), ("AO", "Angola"), ("AI", "Anguilla"), ("AQ", "Antarctica"), ("AG", "Antigua and Barbuda"),
    ("AR", "Argentina"), ("AM", "Armenia"), ("AW", "Aruba"), ("AU", "Australia"), ("AT", "Austria"),
    ("AZ", "Azerbaijan"), ("BS", "Bahamas"), ("BH", "Bahrain"), ("BD", "Bangladesh"), ("BB", "Barbados"),
    ("BY", "Belarus"), ("BE", "Belgium"), ("BZ", "Belize"), ("BJ", "Benin"), ("BM", "Bermuda"),
    ("BT", "Bhutan"), ("BO", "Bolivia"), ("BQ", "Bonaire, Sint Eustatius and Saba"), ("BA", "Bosnia and Herzegovina"),
    ("BW", "Botswana"), ("BV", "Bouvet Island"), ("BR", "Brazil"), ("IO", "British Indian Ocean Territory"),
    ("BN", "Brunei"), ("BG", "Bulgaria"), ("BF", "Burkina Faso"), ("BI", "Burundi"), ("CV", "Cabo Verde"),
    ("KH", "Cambodia"), ("CM", "Cameroon"), ("CA", "Canada"), ("KY", "Cayman Islands"), ("CF", "Central African Republic"),
    ("TD", "Chad"), ("CL", "Chile"), ("CN", "China"), ("CX", "Christmas Island"), ("CC", "Cocos (Keeling) Islands"),
    ("CO", "Colombia"), ("KM", "Comoros"), ("CG", "Congo"), ("CD", "Congo (Democratic Republic)"), ("CK", "Cook Islands"),
    ("CR", "Costa Rica"), ("CI", "Côte d'Ivoire"), ("HR", "Croatia"), ("CU", "Cuba"), ("CW", "Curaçao"),
    ("CY", "Cyprus"), ("CZ", "Czechia"), ("DK", "Denmark"), ("DJ", "Djibouti"), ("DM", "Dominica"),
    ("DO", "Dominican Republic"), ("EC", "Ecuador"), ("EG", "Egypt"), ("SV", "El Salvador"), ("GQ", "Equatorial Guinea"),
    ("ER", "Eritrea"), ("EE", "Estonia"), ("SZ", "Eswatini"), ("ET", "Ethiopia"), ("FK", "Falkland Islands"),
    ("FO", "Faroe Islands"), ("FJ", "Fiji"), ("FI", "Finland"), ("FR", "France"), ("GF", "French Guiana"),
    ("PF", "French Polynesia"), ("TF", "French Southern Territories"), ("GA", "Gabon"), ("GM", "Gambia"),
    ("GE", "Georgia"), ("DE", "Germany"), ("GH", "Ghana"), ("GI", "Gibraltar"), ("GR", "Greece"), ("GL", "Greenland"),
    ("GD", "Grenada"), ("GP", "Guadeloupe"), ("GU", "Guam"), ("GT", "Guatemala"), ("GG", "Guernsey"), ("GN", "Guinea"),
    ("GW", "Guinea-Bissau"), ("GY", "Guyana"), ("HT", "Haiti"), ("HM", "Heard Island and McDonald Islands"),
    ("VA", "Holy See (Vatican City)"), ("HN", "Honduras"), ("HK", "Hong Kong"), ("HU", "Hungary"), ("IS", "Iceland"),
    ("IN", "India"), ("ID", "Indonesia"), ("IR", "Iran"), ("IQ", "Iraq"), ("IE", "Ireland"), ("IM", "Isle of Man"),
    ("IL", "Israel"), ("IT", "Italy"), ("JM", "Jamaica"), ("JP", "Japan"), ("JE", "Jersey"), ("JO", "Jordan"),
    ("KZ", "Kazakhstan"), ("KE", "Kenya"), ("KI", "Kiribati"), ("KP", "North Korea"), ("KR", "South Korea"),
    ("XK", "Kosovo"), ("KW", "Kuwait"), ("KG", "Kyrgyzstan"), ("LA", "Laos"), ("LV", "Latvia"), ("LB", "Lebanon"),
    ("LS", "Lesotho"), ("LR", "Liberia"), ("LY", "Libya"), ("LI", "Liechtenstein"), ("LT", "Lithuania"),
    ("LU", "Luxembourg"), ("MO", "Macao"), ("MG", "Madagascar"), ("MW", "Malawi"), ("MY", "Malaysia"),
    ("MV", "Maldives"), ("ML", "Mali"), ("MT", "Malta"), ("MH", "Marshall Islands"), ("MQ", "Martinique"),
    ("MR", "Mauritania"), ("MU", "Mauritius"), ("YT", "Mayotte"), ("MX", "Mexico"), ("FM", "Micronesia"),
    ("MD", "Moldova"), ("MC", "Monaco"), ("MN", "Mongolia"), ("ME", "Montenegro"), ("MS", "Montserrat"),
    ("MA", "Morocco"), ("MZ", "Mozambique"), ("MM", "Myanmar"), ("NA", "Namibia"), ("NR", "Nauru"), ("NP", "Nepal"),
    ("NL", "Netherlands"), ("NC", "New Caledonia"), ("NZ", "New Zealand"), ("NI", "Nicaragua"), ("NE", "Niger"),
    ("NG", "Nigeria"), ("NU", "Niue"), ("NF", "Norfolk Island"), ("MK", "North Macedonia"),
    ("MP", "Northern Mariana Islands"), ("NO", "Norway"), ("OM", "Oman"), ("PK", "Pakistan"), ("PW", "Palau"),
    ("PS", "Palestine"), ("PA", "Panama"), ("PG", "Papua New Guinea"), ("PY", "Paraguay"), ("PE", "Peru"),
    ("PH", "Philippines"), ("PN", "Pitcairn"), ("PL", "Poland"), ("PT", "Portugal"), ("PR", "Puerto Rico"),
    ("QA", "Qatar"), ("RE", "Réunion"), ("RO", "Romania"), ("RU", "Russia"), ("RW", "Rwanda"),
    ("BL", "Saint Barthélemy"), ("SH", "Saint Helena, Ascension and Tristan da Cunha"), ("KN", "Saint Kitts and Nevis"),
    ("LC", "Saint Lucia"), ("MF", "Saint Martin (French part)"), ("PM", "Saint Pierre and Miquelon"),
    ("VC", "Saint Vincent and the Grenadines"), ("WS", "Samoa"), ("SM", "San Marino"), ("ST", "Sao Tome and Principe"),
    ("SA", "Saudi Arabia"), ("SN", "Senegal"), ("RS", "Serbia"), ("SC", "Seychelles"), ("SL", "Sierra Leone"),
    ("SG", "Singapore"), ("SX", "Sint Maarten (Dutch part)"), ("SK", "Slovakia"), ("SI", "Slovenia"),
    ("SB", "Solomon Islands"), ("SO", "Somalia"), ("ZA", "South Africa"),
    ("GS", "South Georgia and the South Sandwich Islands"), ("SS", "South Sudan"), ("ES", "Spain"), ("LK", "Sri Lanka"),
    ("SD", "Sudan"), ("SR", "Suriname"), ("SJ", "Svalbard and Jan Mayen"), ("SE", "Sweden"), ("CH", "Switzerland"),
    ("SY", "Syria"), ("TW", "Taiwan"), ("TJ", "Tajikistan"), ("TZ", "Tanzania"), ("TH", "Thailand"),
    ("TL", "Timor-Leste"), ("TG", "Togo"), ("TK", "Tokelau"), ("TO", "Tonga"), ("TT", "Trinidad and Tobago"),
    ("TN", "Tunisia"), ("TR", "Türkiye"), ("TM", "Turkmenistan"), ("TC", "Turks and Caicos Islands"), ("TV", "Tuvalu"),
    ("UG", "Uganda"), ("UA", "Ukraine"), ("AE", "United Arab Emirates"), ("GB", "United Kingdom"),
    ("US", "United States"), ("UM", "United States Minor Outlying Islands"), ("UY", "Uruguay"), ("UZ", "Uzbekistan"),
    ("VU", "Vanuatu"), ("VE", "Venezuela"), ("VN", "Vietnam"), ("VG", "Virgin Islands (British)"),
    ("VI", "Virgin Islands (US)"), ("WF", "Wallis and Futuna"), ("EH", "Western Sahara"), ("YE", "Yemen"),
    ("ZM", "Zambia"), ("ZW", "Zimbabwe"),
)
NAMES = dict(COUNTRIES)

# Countries Meta does not deliver ads to. Sources:
# - Meta Commercial Terms: no commercial activity (advertising) for or in countries embargoed under US law
#   https://www.facebook.com/legal/commercial_terms (the US-embargoed countries: Cuba, Iran, North Korea, Syria;
#   Crimea, Donetsk and Luhansk are regions of Ukraine and can't be picked here)
# - Meta paused all ads targeting people in Russia on 4 March 2022 (still paused)
#   https://techcrunch.com/2022/03/04/google-microsoft-sales-pause-russia
RESTRICTED = {
    "CU": "Meta does not run ads in Cuba (US embargo)",
    "IR": "Meta does not run ads in Iran (US embargo)",
    "KP": "Meta does not run ads in North Korea (US embargo)",
    "SY": "Meta does not run ads in Syria (US embargo)",
    "RU": "Meta has paused all ads targeting people in Russia since March 2022",
}

EU = ("AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
      "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE")
GROUPS = (
    ("European Union", EU),
    ("Europe", EU + ("GB", "NO", "IS", "CH", "LI", "AD", "MC", "SM", "VA", "AL", "BA", "ME", "MK", "RS", "XK", "MD", "UA",
                     "BY", "GI", "FO", "IM", "JE", "GG", "AX")),
    ("Nordics", ("DK", "FI", "IS", "NO", "SE")),
    ("DACH", ("DE", "AT", "CH")),
    ("English-speaking", ("GB", "IE", "US", "CA", "AU", "NZ")),
    ("North America", ("US", "CA")),
)

# geo_locations keys that hold places inside a country (each entry carries country_code, or country for postcodes)
SUB_KINDS = (("regions", "region"), ("cities", "city"), ("zips", "postcode"), ("places", "place"),
             ("geo_markets", "market"), ("electoral_districts", "electoral district"), ("neighborhoods", "neighbourhood"),
             ("subcities", "area"), ("medium_geo_areas", "area"), ("large_geo_areas", "area"), ("small_geo_areas", "area"),
             ("metro_areas", "metro area"), ("subneighborhoods", "area"), ("custom_locations", "pin"))
LOCATION_KEYS = ("countries", "country_groups") + tuple(k for k, _ in SUB_KINDS)

# special ad categories with Meta's audience restrictions (housing, employment, credit / financial products)
# https://developers.facebook.com/docs/marketing-api/audiences/special-ad-category
RESTRICTED_CATS = ("HOUSING", "EMPLOYMENT", "FINANCIAL_PRODUCTS_SERVICES", "CREDIT")
POLITICS = "ISSUES_ELECTIONS_POLITICS"


def name(code):
    return NAMES.get(str(code).upper(), str(code))


def label(code):
    return f"{name(code)} ({str(code).upper()})"


def clean(codes):
    """-> codes upper-cased, trimmed, duplicates dropped (order kept)."""
    out = []
    for x in codes or []:
        x = str(x).strip().upper()
        if x and x not in out:
            out.append(x)
    return out


def _cc(entry):
    return str(entry.get("country_code") or entry.get("country") or "").upper() if isinstance(entry, dict) else ""


def sub_places(geo):
    """-> [(kind word, name, country code)] for every region / city / postcode / ... in a geo_locations dict."""
    out = []
    for key, word in SUB_KINDS:
        for e in (geo or {}).get(key) or []:
            if isinstance(e, dict):
                out.append((word, str(e.get("name") or e.get("key") or e.get("address_string") or "?"), _cc(e)))
    return out


def code_problems(codes):
    """Bad or restricted country codes -> problems."""
    out = []
    bad = [x for x in clean(codes) if x not in NAMES]
    if bad:
        out.append(f"not a country code: {', '.join(bad)} (use 2-letter ISO codes, e.g. GB, DE)")
    for x in clean(codes):
        if x in RESTRICTED:
            out.append(f"{label(x)} can't be targeted: {RESTRICTED[x]}")
    return out


def geo_problems(codes, targeting=None):
    """codes: the new country list for an ad set whose targeting is `targeting` (None for a new ad set).
    -> problems that block (Meta would refuse it, or it can't run)."""
    codes = clean(codes)
    out = code_problems(codes)
    t = targeting or {}
    geo = t.get("geo_locations") or {}
    pick = set(codes)
    for word, nm, cc in sub_places(geo):
        if cc in pick:
            out.append(f"{label(cc)} overlaps the {word} {nm} ({name(cc)}) the ad set already targets: "
                       f"Meta refuses overlapping locations, so leave {name(cc)} out or remove the {word} in Meta")
    excl = set(clean((t.get("excluded_geo_locations") or {}).get("countries")))
    both = [x for x in codes if x in excl]
    if both:
        out.append(f"{', '.join(label(x) for x in both)} {'is' if len(both) == 1 else 'are'} also excluded in this ad set's locations: "
                   "a country can't be both targeted and excluded")
    others = [k for k in LOCATION_KEYS[1:] if geo.get(k)]
    if not codes and not others:
        out.append("the ad set needs at least one location: pick a country (or keep a region or city)")
    return out


def special_warnings(cats, targeting=None, added=()):
    """Warnings for ad sets in campaigns with special ad categories (Meta's rules for those audiences)."""
    cats = [str(c).upper() for c in cats or []]
    out = []
    t = targeting or {}
    if any(c in RESTRICTED_CATS for c in cats):
        geo, excl = t.get("geo_locations") or {}, t.get("excluded_geo_locations") or {}
        if any(excl.get(k) for k in LOCATION_KEYS):
            out.append("special ad category (housing, employment or credit): Meta does not allow excluded locations in "
                       "these ad sets; remove the exclusions in Meta or the change may be refused")
        if geo.get("zips"):
            out.append("special ad category (housing, employment or credit): Meta does not allow postcode (ZIP) "
                       "targeting in these ad sets")
    if POLITICS in cats and added:
        out.append(f"social issues, elections or politics: the ads can only run in countries the account and Page are "
                   f"authorised for; check {', '.join(clean(added))} before pushing")
    return out


def summary(geo):
    """geo_locations -> short text: countries (codes), then '+ 2 regions, 1 city'."""
    geo = geo or {}
    bits = list(clean(geo.get("countries")))
    bits += [str(g) for g in geo.get("country_groups") or []]
    counts = {}
    for word, _nm, _cc in sub_places(geo):
        counts[word] = counts.get(word, 0) + 1
    extra = ", ".join(f"{n} {w if n == 1 else _plural(w)}" for w, n in counts.items())
    return (", ".join(bits) + (f" + {extra}" if bits and extra else extra)) if (bits or extra) else ""


def _plural(w):
    return {"city": "cities", "neighbourhood": "neighbourhoods"}.get(w, w + "s")


def for_ui():
    """What the picker needs: every country (code, name, restricted reason) and the quick groups."""
    return {"countries": [{"code": c, "name": n, **({"restricted": RESTRICTED[c]} if c in RESTRICTED else {})}
                          for c, n in sorted(COUNTRIES, key=lambda x: unicodedata.normalize("NFKD", x[1]).encode("ascii", "ignore"))],
            "groups": [{"name": g, "codes": list(codes)} for g, codes in GROUPS]}
