#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
모닝 콜시트 (Morning Callsheet) — fully automated builder.

Reads template.html (placeholder tokens: {{KEY}}), researches fresh content
via the Anthropic API (Claude + the web_search server tool), fetches and
processes thumbnail images, fills the template, validates the result, and
writes index.html — ready to be committed to GitHub Pages by the calling
GitHub Actions workflow.

Requires the ANTHROPIC_API_KEY environment variable (set as a GitHub
Actions repository secret — never hardcoded, never logged).
"""

import base64
import html
import io
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta

import requests
from anthropic import Anthropic
from PIL import Image

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

MODEL = "claude-sonnet-5"
KST = ZoneInfo("Asia/Seoul")
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "template.html")
OUTPUT_PATH = os.path.join(HERE, "index.html")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

WD_EN = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WD_KO_1CHAR = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}
BRIEF_WEEKDAYS = {0, 2, 4}  # Mon / Wed / Fri


# --------------------------------------------------------------------------
# 1. Dates — computed locally, never trusted to the model
# --------------------------------------------------------------------------

def compute_dates():
    now = datetime.now(KST)
    date_full = "{:04d}.{:02d}.{:02d} {} · 09:00".format(
        now.year, now.month, now.day, WD_EN[now.weekday()]
    )
    date_short = "{:04d}.{:02d}.{:02d} {}".format(
        now.year, now.month, now.day, WD_EN[now.weekday()]
    )
    nxt = now
    for _ in range(7):
        nxt = nxt + timedelta(days=1)
        if nxt.weekday() in BRIEF_WEEKDAYS:
            break
    next_brief = "{:02d}.{:02d}({})".format(nxt.month, nxt.day, WD_KO_1CHAR[nxt.weekday()])
    return {"DATE_FULL": date_full, "DATE_SHORT": date_short, "NEXT_BRIEF": next_brief}


# --------------------------------------------------------------------------
# 2. Research — Anthropic API, forced structured output + web_search
# --------------------------------------------------------------------------

SUBMIT_BRIEFING_TOOL = {
    "name": "submit_briefing",
    "description": "오늘의 모닝 콜시트 브리핑 콘텐츠를 구조화된 형식으로 제출합니다.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["tldr", "ad", "design", "ai", "ref", "featured"],
        "properties": {
            "tldr": {
                "type": "object",
                "required": ["ad", "design", "ai", "ref"],
                "properties": {
                    "ad": {"type": "string", "description": "광고업계 섹션 한 줄 요약 (홈 화면용, 40자 내외)"},
                    "design": {"type": "string", "description": "디자인·전시 섹션 한 줄 요약 (40자 내외)"},
                    "ai": {"type": "string", "description": "AI 섹션 한 줄 요약 (40자 내외)"},
                    "ref": {"type": "string", "description": "레퍼런스 릴 섹션 한 줄 요약 (40자 내외)"},
                },
            },
            "ad": {
                "type": "array", "minItems": 3, "maxItems": 3,
                "items": {
                    "type": "object",
                    "required": ["kw", "src", "url", "title", "summary", "label", "has_image"],
                    "properties": {
                        "kw": {"type": "string", "description": "검색용 키워드, 공백으로 구분된 3~6개 단어"},
                        "src": {"type": "string", "description": "출처명 (예: Madtimes)"},
                        "url": {"type": "string", "description": "실제 기사 URL (반드시 실존하는 링크)"},
                        "title": {"type": "string", "description": "기사 제목 (한국어, 카드 헤드라인용)"},
                        "summary": {"type": "string", "description": "2~3문장 요약"},
                        "label": {"type": "string", "description": "북마크 시트용 짧은 제목 (15자 내외)"},
                        "has_image": {"type": "boolean", "description": "이 기사에 대표 이미지(og:image)가 있어 썸네일로 쓸 만하면 true, 없으면 false"},
                    },
                },
                "description": "광고업계 동향 기사 3건 (Madtimes, VML, Marketing Dive, Adweek, Campaign, The Drum 등 실제 업계 매체에서)",
            },
            "design": {
                "type": "array", "minItems": 2, "maxItems": 2,
                "items": {
                    "type": "object",
                    "required": ["kw", "src", "url", "title", "summary", "label", "has_image"],
                    "properties": {
                        "kw": {"type": "string"}, "src": {"type": "string"}, "url": {"type": "string"},
                        "title": {"type": "string"}, "summary": {"type": "string"}, "label": {"type": "string"},
                        "has_image": {"type": "boolean"},
                    },
                },
                "description": "디자인·전시 기사 2건 (Frieze, Dezeen, It's Nice That, Creative Review 등)",
            },
            "ai": {
                "type": "array", "minItems": 2, "maxItems": 2,
                "items": {
                    "type": "object",
                    "required": ["kw", "src", "url", "title", "summary", "label", "has_image"],
                    "properties": {
                        "kw": {"type": "string"}, "src": {"type": "string"}, "url": {"type": "string"},
                        "title": {"type": "string"}, "summary": {"type": "string"}, "label": {"type": "string"},
                        "has_image": {"type": "boolean"},
                    },
                },
                "description": "AI 업계/크리에이티브 AI 뉴스 2건 (HBR, TechCrunch, The Verge 등)",
            },
            "ref": {
                "type": "array", "minItems": 5, "maxItems": 5,
                "items": {
                    "type": "object",
                    "required": [
                        "kw", "video_url", "platform", "title", "shorttitle", "brand",
                        "shortbrand", "pencil", "synopsis", "credit",
                    ],
                    "properties": {
                        "kw": {"type": "string", "description": "검색용 키워드"},
                        "video_url": {"type": "string", "description": "YouTube 또는 Vimeo 영상 실제 URL"},
                        "platform": {"type": "string", "enum": ["youtube", "vimeo"]},
                        "title": {"type": "string", "description": "작품명 (따옴표 없이)"},
                        "shorttitle": {"type": "string", "description": "홈 릴스트립용 초단축 제목 (10자 내외)"},
                        "brand": {"type": "string", "description": "브랜드 · 제작사 (예: Claude · Mother London)"},
                        "shortbrand": {"type": "string", "description": "홈 릴스트립용 초단축 브랜드명"},
                        "pencil": {"type": "string", "description": "수상 등급 배지 텍스트 (예: YELLOW ×2, SHORTLIST)"},
                        "synopsis": {"type": "string", "description": "영상 내용 시놉시스, 3~4문장"},
                        "credit": {"type": "string", "description": "연출/편집 크레딧과 참고 포인트 한 줄"},
                    },
                },
                "description": "최신 어워드 수상작/화제작 5편 (D&AD, 칸 라이언즈, LBBonline, Ads of the World, Cut+Run 등에서 실제 공개 영상)",
            },
            "featured": {
                "type": "object",
                "required": ["ref_index", "tag", "note"],
                "properties": {
                    "ref_index": {"type": "integer", "minimum": 1, "maximum": 5, "description": "대문 화면에 노출할 ref 항목 번호 (1~5)"},
                    "tag": {"type": "string", "description": "썸네일 위 배지 텍스트 (예: 오늘의 픽)"},
                    "note": {"type": "string", "description": "대문 화면 캡션용 한 줄 코멘트, 20자 내외"},
                },
            },
        },
    },
}

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 12}

RESEARCH_PROMPT = """\
당신은 광고 크리에이티브 팀을 위한 아침 브리핑 큐레이터입니다. 오늘 발행할 "모닝 콜시트"에
들어갈 콘텐츠를 웹 검색으로 조사해 주세요. 오늘 날짜는 {date_full} (KST) 입니다.

반드시 지켜야 할 원칙:
- 모든 기사·영상은 web_search로 실제로 확인한 것만 사용하세요. URL, 제목, 내용을 절대 지어내지 마세요.
- 가능하면 최근 1~2주 이내의 소식을 우선하세요.
- 광고업계 동향 3건 (Madtimes, VML, Adweek, Campaign, The Drum, Marketing Dive 중심),
  디자인·전시 소식 2건 (Frieze, Dezeen, It's Nice That, Creative Review 중심),
  AI/크리에이티브 AI 뉴스 2건 (HBR, TechCrunch, The Verge 중심),
  레퍼런스 영상 5편 (D&AD, 칸 라이언즈, LBBonline, Ads of the World, Cut+Run 등에서 최근 수상작/화제작,
  YouTube 또는 Vimeo에 실제 임베드 가능한 영상)을 찾아주세요.
- 레퍼런스 영상은 반드시 YouTube(watch?v= 형식) 또는 Vimeo(vimeo.com/숫자 형식) 링크여야 합니다.
- 각 기사에 이미지(og:image)가 있을 만한지 has_image로 표시하세요. 확신이 없으면 false로 두세요.
- 모든 텍스트는 자연스러운 한국어로 작성하세요.
- 조사를 충분히 마친 뒤, 마지막에 반드시 submit_briefing 도구를 호출해 결과를 제출하세요.
"""

FORCE_SUBMIT_PROMPT = """\
지금까지의 리서치 결과를 바탕으로, 지금 바로 submit_briefing 도구를 호출해서
구조화된 JSON으로 제출하세요. 추가 설명 없이 도구 호출만 하세요.
"""


def call_anthropic_research(date_full):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    client = Anthropic(api_key=api_key)
    tools = [WEB_SEARCH_TOOL, SUBMIT_BRIEFING_TOOL]
    messages = [{"role": "user", "content": RESEARCH_PROMPT.format(date_full=date_full)}]

    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        tools=tools,
        messages=messages,
    )

    briefing = _extract_submit_briefing(resp)
    if briefing is not None:
        return briefing

    # Model didn't call submit_briefing on its own (e.g. it just summarized in
    # text) — do one more turn, this time forcing the tool call.
    messages.append({"role": "assistant", "content": resp.content})
    messages.append({"role": "user", "content": FORCE_SUBMIT_PROMPT})

    resp2 = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        tools=tools,
        tool_choice={"type": "tool", "name": "submit_briefing"},
        messages=messages,
    )
    briefing = _extract_submit_briefing(resp2)
    if briefing is None:
        raise RuntimeError("submit_briefing 도구 호출을 끝내 받지 못했습니다. API 응답을 확인하세요.")
    return briefing


def _extract_submit_briefing(resp):
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_briefing":
            return block.input
    return None


# --------------------------------------------------------------------------
# 3. Images
# --------------------------------------------------------------------------

YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})"
)
VIMEO_ID_RE = re.compile(r"vimeo\.com/(\d+)")


def fetch_bytes(url, headers=None, timeout=15):
    r = requests.get(url, headers=headers or {"User-Agent": BROWSER_UA}, timeout=timeout)
    r.raise_for_status()
    return r.content


def fetch_og_image(article_url):
    """Scrape an article page for its og:image / twitter:image meta tag."""
    try:
        r = requests.get(article_url, headers={"User-Agent": BROWSER_UA}, timeout=15)
        r.raise_for_status()
        html_text = r.text
    except Exception as e:
        print(f"  [warn] og:image 페이지 요청 실패 ({article_url}): {e}", file=sys.stderr)
        return None

    m = re.search(
        r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html_text, re.I,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
        html_text, re.I,
    ) or re.search(
        r'<meta[^>]+(?:property|name)=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        html_text, re.I,
    )
    if not m:
        return None
    img_url = urllib.parse.urljoin(article_url, html.unescape(m.group(1)))
    try:
        return fetch_bytes(img_url)
    except Exception as e:
        print(f"  [warn] og:image 다운로드 실패 ({img_url}): {e}", file=sys.stderr)
        return None


def fetch_video_thumbnail(video_url, platform):
    try:
        if platform == "youtube":
            m = YOUTUBE_ID_RE.search(video_url)
            if not m:
                return None
            vid = m.group(1)
            for variant in ("maxresdefault.jpg", "hqdefault.jpg"):
                try:
                    data = fetch_bytes(f"https://i.ytimg.com/vi/{vid}/{variant}")
                    # YouTube serves a tiny placeholder JPEG (~1-2KB, 120x90)
                    # when maxresdefault doesn't exist for a video — skip it.
                    if len(data) > 4000:
                        return data
                except Exception:
                    continue
            return None
        elif platform == "vimeo":
            m = VIMEO_ID_RE.search(video_url)
            if not m:
                return None
            oembed = requests.get(
                "https://vimeo.com/api/oembed.json",
                params={"url": video_url},
                headers={"User-Agent": BROWSER_UA},
                timeout=15,
            )
            oembed.raise_for_status()
            thumb_url = oembed.json().get("thumbnail_url")
            if not thumb_url:
                return None
            return fetch_bytes(thumb_url)
    except Exception as e:
        print(f"  [warn] 영상 썸네일 가져오기 실패 ({video_url}): {e}", file=sys.stderr)
        return None
    return None


PLACEHOLDER_JPEG = None  # lazily-generated flat-color fallback tile


def _placeholder_bytes(w, h):
    img = Image.new("RGB", (w, h), (233, 227, 216))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def to_data_uri(raw_bytes, target_w, target_h, quality=78):
    """Center-crop + resize + JPEG-compress raw image bytes into a single,
    correctly-prefixed base64 data URI. NEVER re-add the data: prefix on top
    of an already-prefixed string elsewhere in this pipeline — this function
    is the ONLY place that prefix is added."""
    if raw_bytes is None:
        raw_bytes = _placeholder_bytes(target_w, target_h)
    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    except Exception as e:
        print(f"  [warn] 이미지 디코딩 실패, 플레이스홀더로 대체: {e}", file=sys.stderr)
        img = Image.open(io.BytesIO(_placeholder_bytes(target_w, target_h))).convert("RGB")

    src_w, src_h = img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        x0 = (src_w - new_w) // 2
        img = img.crop((x0, 0, x0 + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        y0 = (src_h - new_h) // 2
        img = img.crop((0, y0, src_w, y0 + new_h))
    img = img.resize((target_w, target_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + b64  # <-- the ONE prefix, added ONCE


# Target sizes per placement in the layout (see callsheet_cosmos_template.html)
SIZE_CARD = (640, 480)      # ad/design/ai card-media
SIZE_CLIP = (640, 360)      # reference-page clip-poster / home reelstrip / featured cover


# --------------------------------------------------------------------------
# 4. Assemble the placeholder map
# --------------------------------------------------------------------------

def esc(v):
    return html.escape(str(v), quote=True)


def js_str_esc(v):
    """Escape for embedding inside a single-quoted JS string literal.
    Used ONLY for the *_LABEL placeholders, which the template inserts
    inside <script>...</script> (e.g. 'ad-1':'{{AD1_LABEL}}') — HTML entity
    escaping must NOT be used there, since <script> content is raw text to
    the HTML parser and entities like &#x27; would show up literally in the
    UI instead of being decoded."""
    s = str(v)
    s = s.replace("\\", "\\\\").replace("'", "\\'")
    s = s.replace("\n", " ").replace("\r", " ")
    s = s.replace("</script", "<\\/script")  # defense in depth
    return s


def build_placeholder_map(briefing, dates):
    ph = dict(dates)

    ad_items = briefing["ad"]
    de_items = briefing["design"]
    ai_items = briefing["ai"]
    ref_items = briefing["ref"]

    # --- article images ---------------------------------------------------
    # By layout design, the 2nd ad card and 1st design card show a generic
    # SVG icon instead of a photo (kept consistent with the hand-built
    # template); every other article slot gets a real og:image when found.
    ad_img_slots = {0: True, 1: False, 2: True}   # index -> wants image
    de_img_slots = {0: False, 1: True}

    for i, item in enumerate(ad_items):
        key = f"AD{i+1}"
        ph[f"{key}_KW"] = esc(item["kw"])
        ph[f"{key}_SRC"] = esc(item["src"])
        ph[f"{key}_URL"] = esc(item["url"])
        ph[f"{key}_TITLE"] = esc(item["title"])
        ph[f"{key}_SUMMARY"] = esc(item["summary"])
        ph[f"{key}_LABEL"] = js_str_esc(item["label"])
        if ad_img_slots.get(i):
            raw = fetch_og_image(item["url"]) if item.get("has_image", True) else None
            ph[f"{key}_IMG"] = to_data_uri(raw, *SIZE_CARD)

    for i, item in enumerate(de_items):
        key = f"DE{i+1}"
        ph[f"{key}_KW"] = esc(item["kw"])
        ph[f"{key}_SRC"] = esc(item["src"])
        ph[f"{key}_URL"] = esc(item["url"])
        ph[f"{key}_TITLE"] = esc(item["title"])
        ph[f"{key}_SUMMARY"] = esc(item["summary"])
        ph[f"{key}_LABEL"] = js_str_esc(item["label"])
        if de_img_slots.get(i):
            raw = fetch_og_image(item["url"]) if item.get("has_image", True) else None
            ph[f"{key}_IMG"] = to_data_uri(raw, *SIZE_CARD)

    for i, item in enumerate(ai_items):
        key = f"AI{i+1}"
        ph[f"{key}_KW"] = esc(item["kw"])
        ph[f"{key}_SRC"] = esc(item["src"])
        ph[f"{key}_URL"] = esc(item["url"])
        ph[f"{key}_TITLE"] = esc(item["title"])
        ph[f"{key}_SUMMARY"] = esc(item["summary"])
        ph[f"{key}_LABEL"] = js_str_esc(item["label"])
        raw = fetch_og_image(item["url"]) if item.get("has_image", True) else None
        ph[f"{key}_IMG"] = to_data_uri(raw, *SIZE_CARD)

    # --- reference clips ----------------------------------------------------
    ref_raw_images = []
    for i, item in enumerate(ref_items):
        n = i + 1
        key = f"REF{n}"
        raw = fetch_video_thumbnail(item["video_url"], item.get("platform", "youtube"))
        ref_raw_images.append(raw)
        ph[f"{key}_KW"] = esc(item["kw"])
        ph[f"{key}_URL"] = esc(item["video_url"])
        ph[f"{key}_IMG"] = to_data_uri(raw, *SIZE_CLIP)
        ph[f"{key}_TITLE"] = esc(item["title"])
        ph[f"{key}_SHORTTITLE"] = esc(item["shorttitle"])
        ph[f"{key}_BRAND"] = esc(item["brand"])
        ph[f"{key}_SHORTBRAND"] = esc(item["shortbrand"])
        ph[f"{key}_PENCIL"] = esc(item["pencil"])
        ph[f"{key}_SYNOPSIS"] = esc(item["synopsis"])
        ph[f"{key}_CREDIT"] = esc(item["credit"])
        ph[f"{key}_LABEL"] = js_str_esc(f"{item['brand']} · {item['title']}"[:40])

    # NOTE: REFn_IMG is reused in two places with different aspect ratios —
    # the home reelstrip thumb (near-square) and the 16:9 clip-poster on the
    # reference page — but the template exposes only one {{REFn_IMG}} token
    # per clip. Encode at 16:9 (the reference page's native ratio); both CSS
    # spots use background-size:cover, so the home reelstrip crops it further
    # rather than distorting it.

    # --- home collage (reuses ad-1 / design-2 images) -----------------------
    # AD1_IMG / DE2_IMG are already set above.

    # --- featured (cover) ---------------------------------------------------
    feat = briefing["featured"]
    fi = feat["ref_index"]
    fitem = ref_items[fi - 1]
    ph["FEATURED_IMG"] = ph[f"REF{fi}_IMG"]
    ph["FEATURED_URL"] = esc(fitem["video_url"])
    ph["FEATURED_TAG"] = esc(feat["tag"])
    ph["FEATURED_TITLE"] = esc(fitem["title"])
    ph["FEATURED_BRAND"] = esc(fitem["brand"])
    ph["FEATURED_NOTE"] = esc(feat["note"])
    ph["FEATURED_CLIP_ID"] = f"clip-{fi}"

    # --- home TL;DR ----------------------------------------------------------
    ph["TLDR_AD"] = esc(briefing["tldr"]["ad"])
    ph["TLDR_DESIGN"] = esc(briefing["tldr"]["design"])
    ph["TLDR_AI"] = esc(briefing["tldr"]["ai"])
    ph["TLDR_REF"] = esc(briefing["tldr"]["ref"])

    return ph


# --------------------------------------------------------------------------
# 5. Fill + validate
# --------------------------------------------------------------------------

def fill_template(template_text, ph):
    out = template_text
    for key, value in ph.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def validate(out):
    errors = []

    leftover = re.findall(r"\{\{[A-Z0-9_]+\}\}", out)
    if leftover:
        errors.append(f"채워지지 않은 placeholder 남음: {sorted(set(leftover))}")

    for tag in ("div", "button", "svg", "a", "section"):
        opens = len(re.findall(r"<" + tag + r"(\s|>)", out))
        closes = len(re.findall(r"</" + tag + r">", out))
        if opens != closes:
            errors.append(f"태그 불균형: <{tag}> open={opens} close={closes}")

    scripts = re.findall(r"<script>(.*?)</script>", out, re.S)
    for i, s in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(s)
            path = f.name
        try:
            subprocess.run(["node", "--check", path], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            errors.append(f"script[{i}] 문법 오류:\n{e.stderr}")
        except FileNotFoundError:
            print("  [warn] node를 찾을 수 없어 JS 문법 검증을 건너뜁니다.", file=sys.stderr)
        finally:
            os.unlink(path)

    if "base64,data:image" in out:
        errors.append("base64 이중 접두사(base64,data:image) 발견 — 이미지 인코딩 버그")

    for m in re.finditer(r'data:image/jpeg;base64,([A-Za-z0-9+/=]+)', out):
        b64_str = m.group(1)
        try:
            raw = base64.b64decode(b64_str, validate=True)
        except Exception:
            errors.append("base64 디코딩 실패한 이미지 발견")
            continue
        if raw[:3] != b"\xff\xd8\xff":
            errors.append("JPEG magic bytes(FFD8FF) 불일치 이미지 발견")

    return errors


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    dates = compute_dates()
    print(f"[1/5] 날짜 계산 완료: {dates}")

    print("[2/5] Anthropic API로 콘텐츠 리서치 중...")
    briefing = call_anthropic_research(dates["DATE_FULL"])
    print(f"  ad={len(briefing['ad'])} design={len(briefing['design'])} "
          f"ai={len(briefing['ai'])} ref={len(briefing['ref'])}")

    print("[3/5] 이미지 수집/가공 중...")
    ph = build_placeholder_map(briefing, dates)
    print(f"  placeholder {len(ph)}개 준비 완료")

    print("[4/5] 템플릿 채우는 중...")
    if not os.path.exists(TEMPLATE_PATH):
        print(f"ERROR: 템플릿을 찾을 수 없습니다: {TEMPLATE_PATH}", file=sys.stderr)
        sys.exit(1)
    template_text = open(TEMPLATE_PATH, encoding="utf-8").read()
    out = fill_template(template_text, ph)

    print("[5/5] 검증 중...")
    errors = validate(out)
    if errors:
        print("검증 실패:", file=sys.stderr)
        for e in errors:
            print(" -", e, file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"완료: {OUTPUT_PATH} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
