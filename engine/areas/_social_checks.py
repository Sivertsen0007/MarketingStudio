"""SoMe checks: copy rules (team_settings, key copy_rules, area "social"), caption limits, links, media rules per channel. Pure functions (link and media
lookups are passed in), so they are unit tested without the network.

Limits (checked 10 November 2026 in Meta's and LinkedIn's docs):
- Instagram (IG User Media reference, Graph v27): caption max 2200 characters, 30 hashtags, 20 @ tags; images JPEG only,
  8 MB max, aspect ratio 4:5 to 1.91:1, width 320-1440 (scaled by Instagram); Reels MOV/MP4, 3 s to 15 min, 300 MB max,
  9:16 recommended; carousel up to 20 items (Studio keeps to 2-10); alt text up to 1000 characters, images only;
  location not on carousel items; Instagram needs an image or video (no text-only posts); links in captions don't click.
- Facebook Page posts: scheduling 10 minutes to 30 days ahead (Pages API "Posts" guide). Multi-photo posts are images only.
  Studio's house limit for captions is 3000 characters.
- LinkedIn Posts API (li-lms-2026-10): organic multi-image posts (no organic carousel); house limit 3000 characters.
"""
import re
import struct

LIMITS = {"facebook": 3000, "instagram": 2200, "linkedin": 3000}
IG_HASHTAGS, IG_MENTIONS, ALT_MAX = 30, 20, 1000
IG_RATIO = (0.8, 1.91)
IG_IMAGE_MAX = 8 * 1024 * 1024
FB_IMAGE_MAX = 10 * 1024 * 1024
LI_IMAGE_MAX = 10 * 1024 * 1024
REEL_MAX, REEL_SECONDS = 300 * 1024 * 1024, (3, 15 * 60)
CAROUSEL = (2, 10)
FB_SCHEDULE_MIN, FB_SCHEDULE_MAX = 10 * 60, 30 * 24 * 3600

URL = re.compile(r"https?://[^\s<>\"')]+")
HASHTAG = re.compile(r"(?<!\w)#\w+")
MENTION = re.compile(r"(?<!\w)@[\w.]+")


def _rules():
    """The SoMe copy rules [(level, compiled, message)]; none where team_settings is not there (the VPS job)."""
    try:
        import team_settings  # noqa: PLC0415
        return team_settings.copy_rules("social")
    except Exception:  # noqa: BLE001
        return []


def copy_problems(label, text):
    """-> (problems, warnings) for one piece of copy: "block" rules are problems, "warn" rules warnings."""
    problems, warnings = [], []
    for level, pat, msg in _rules():
        if pat.search(text or ""):
            (problems if level == "block" else warnings).append(f"{label}: {msg}")
    return problems, warnings


# ---------- media facts (no PIL needed) ----------

def image_size(data):
    """(width, height) of a JPEG or PNG, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg
    return None


def video_seconds(data):
    """Duration of an MP4/MOV from its mvhd box, or None."""
    i = data.find(b"mvhd")
    if i < 0 or i + 24 > len(data):
        return None
    ver = data[i + 4]
    if ver == 1:
        scale, dur = struct.unpack(">IQ", data[i + 24:i + 36])
    else:
        scale, dur = struct.unpack(">II", data[i + 16:i + 24])
    return round(dur / scale, 1) if scale else None


def media_facts(path, kind, ext):
    """-> {kind, ext, size, width, height, seconds} read from the file head (and the whole file for MP4 boxes)."""
    data = path.read_bytes()
    f = {"kind": kind, "ext": ext, "size": len(data)}
    if kind == "image":
        wh = image_size(data[:256 * 1024]) or image_size(data)
        if wh:
            f["width"], f["height"] = wh
    else:
        f["seconds"] = video_seconds(data)
    return f


def _ratio(f):
    return f["width"] / f["height"] if f.get("width") and f.get("height") else None


def media_problems(channel, fmt, facts):
    """facts: list of media_facts dicts in post order. -> (problems, warnings) for one channel."""
    p, w = [], []
    lab = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn"}[channel]
    n = len(facts)
    kinds = {f["kind"] for f in facts}
    if channel == "instagram" and not n:
        p.append("Instagram: add an image or a video (Instagram has no text-only posts)")
    if n > 1:
        lo, hi = CAROUSEL
        if not lo <= n <= hi:
            p.append(f"{lab}: a carousel has {lo} to {hi} items (now {n})")
        if channel in ("facebook", "linkedin") and "video" in kinds:
            p.append(f"{lab}: several items must all be images (one video goes in a post on its own)")
    for i, f in enumerate(facts, 1):
        tag = f"{lab} item {i}" if n > 1 else lab
        if f["kind"] == "image":
            r = _ratio(f)
            if channel == "instagram":
                if f["ext"] != "jpg":
                    p.append(f"{tag}: Instagram takes JPEG images only: save it as .jpg")
                if f["size"] > IG_IMAGE_MAX:
                    p.append(f"{tag}: image is {f['size'] / 1048576:.1f} MB, Instagram allows 8 MB")
                if r and not IG_RATIO[0] - 0.005 <= r <= IG_RATIO[1] + 0.005:
                    p.append(f"{tag}: ratio {f['width']}x{f['height']} is outside 4:5 to 1.91:1 (Instagram)")
                if f.get("width") and not 320 <= f["width"] <= 1440:
                    w.append(f"{tag}: width {f['width']} px, Instagram scales it to 320-1440 px")
            elif channel == "facebook" and f["size"] > FB_IMAGE_MAX:
                p.append(f"{tag}: image is {f['size'] / 1048576:.1f} MB, keep Facebook images under 10 MB")
            elif channel == "linkedin" and f["size"] > LI_IMAGE_MAX:
                p.append(f"{tag}: image is {f['size'] / 1048576:.1f} MB, keep LinkedIn images under 10 MB")
            if r is None:
                w.append(f"{tag}: could not read the image size")
            elif channel in ("facebook", "linkedin") and not 0.5 <= r <= 2.0:
                w.append(f"{tag}: unusual ratio {f['width']}x{f['height']}, it will be cropped in the feed")
        else:
            if channel == "instagram":
                if f["size"] > REEL_MAX:
                    p.append(f"{tag}: video is {f['size'] / 1048576:.0f} MB, Instagram allows 300 MB")
                s = f.get("seconds")
                if s is not None and not REEL_SECONDS[0] <= s <= REEL_SECONDS[1]:
                    p.append(f"{tag}: video is {s:.0f} s, Instagram Reels are 3 s to 15 min")
            if f.get("seconds") is None:
                w.append(f"{tag}: could not read the video length")
    return p, w


# ---------- the whole post ----------

def check_post(post, caption_for, facts=None, link_check=None, when_check=True, now=None):
    """-> {"ok", "problems", "warnings", "channels": {channel: {"problems", "warnings", "length", "limit"}}}.
    caption_for(post, channel) -> text. facts: media facts list (None = media not looked at). link_check(url) -> error or ""."""
    problems, warnings, per = [], [], {}
    chans = post.get("channels") or []
    if not chans:
        problems.append("tick at least one channel")
    links = set()
    if post.get("link"):
        links.add(post["link"].strip())
    for ch in chans:
        cp, cw = [], []
        text = caption_for(post, ch)
        lab = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn"}[ch]
        if not text.strip() and not post.get("media"):
            cp.append(f"{lab}: write a caption")
        if len(text) > LIMITS[ch]:
            cp.append(f"{lab}: caption is {len(text)} characters, the limit is {LIMITS[ch]}")
        a, b = copy_problems(f"{lab} caption", text)
        cp += a
        cw += b
        links.update(u.rstrip(".,;:!") for u in URL.findall(text))
        if ch == "instagram":
            tags, ats = HASHTAG.findall(text), MENTION.findall(text)
            if len(tags) > IG_HASHTAGS:
                cp.append(f"Instagram: {len(tags)} hashtags, the limit is {IG_HASHTAGS}")
            if len(ats) > IG_MENTIONS:
                cp.append(f"Instagram: {len(ats)} @ tags, the limit is {IG_MENTIONS}")
            if URL.search(text):
                cw.append("Instagram: links in captions can't be clicked: say 'link in bio' instead")
            if post.get("link"):
                cw.append("Instagram: the link field is used on Facebook and LinkedIn only")
            if post.get("first_comment"):
                a, b = copy_problems("Instagram first comment", post["first_comment"])
                cp += a
                cw += b
            if post.get("location") and len(post.get("media") or []) > 1:
                cw.append("Instagram: location can't be set on a carousel, it will be left out")
            if post.get("format") == "reel" or any(m.get("kind") == "video" for m in post.get("media") or []):
                if post.get("alt_text"):
                    cw.append("Instagram: alt text is for images only (not Reels)")
        if facts is not None:
            a, b = media_problems(ch, post.get("format"), facts)
            cp += a
            cw += b
        per[ch] = {"problems": cp, "warnings": cw, "length": len(text), "limit": LIMITS[ch]}
        problems += cp
        warnings += cw
    if post.get("alt_text") and len(post["alt_text"]) > ALT_MAX:
        problems.append(f"alt text is {len(post['alt_text'])} characters, the limit is {ALT_MAX}")
    if post.get("alt_text"):
        a, b = copy_problems("alt text", post["alt_text"])
        problems += a
        warnings += b
    if post.get("media") and post.get("media")[0].get("kind") == "image" and not post.get("alt_text"):
        warnings.append("add alt text so people using screen readers know what the image shows")
    if post.get("guest_photos") and not post.get("consent_confirmed"):
        problems.append("guest photos: confirm image consent (every person shown has agreed to the use of their image)")
    for u in sorted(links):
        err = link_check(u) if link_check else ""
        if err:
            problems.append(err)
    return {"ok": not problems, "problems": sorted(set(problems), key=problems.index),
            "warnings": sorted(set(warnings), key=warnings.index), "channels": per}


def schedule_route(channel, when, now, queue_ok):
    """How a channel would be scheduled at `when`. -> (route, problems). route: "meta" (Facebook's own scheduling),
    "queue" (Studio's queue, published by Studio's scheduler on this Mac or the VPS publisher job) or None.
    queue_ok: one of those two is on. Facebook uses Meta's scheduling whenever the time is 10 min to 30 days ahead."""
    if not when:
        return None, ["pick a date and time first"]
    ahead = (when - now).total_seconds()
    if ahead < 60:
        return None, ["the time is in the past: pick a later time or use Post now"]
    if channel == "facebook":
        if FB_SCHEDULE_MIN <= ahead <= FB_SCHEDULE_MAX:
            return "meta", []
        if queue_ok and ahead > FB_SCHEDULE_MAX:
            return "queue", []
        return None, ["Facebook schedules 10 minutes to 30 days ahead" + ("" if ahead > FB_SCHEDULE_MIN else ": pick a later time")]
    if not queue_ok:
        name = "Instagram" if channel == "instagram" else "LinkedIn"
        return None, [f"{name} has no scheduling of its own: turn on Studio's scheduler in SoMe settings (Studio must be open "
                      "on this Mac at that time), or the VPS publisher job. Or use Post now at the time"]
    return "queue", []
