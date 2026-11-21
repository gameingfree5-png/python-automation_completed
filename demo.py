#!/usr/bin/env python3
"""Scrape homepage first, then configured model pages, into JSON and M3U."""

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SCRIPT_VERSION = "home-models-category-v2"
BASE_URL = os.environ.get("BASE_URL", "https://pornhao.com").rstrip("/")
SCROLL_PAUSE = 2
REQUEST_DELAY = 1

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object")
    home = config.get("home", {})
    models = config.get("models", {})
    if not isinstance(home, dict) or not isinstance(models, dict):
        raise ValueError("home and models must be JSON objects")
    if not isinstance(home.get("enabled", True), bool):
        raise ValueError("home.enabled must be true or false")
    if not isinstance(home.get("pages", 2), int) or isinstance(home.get("pages", 2), bool) or home.get("pages", 2) < 1:
        raise ValueError("home.pages must be a positive integer")
    for slug, settings in models.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
            raise ValueError(f"Invalid model slug: {slug}")
        if not isinstance(settings, dict):
            raise ValueError(f"Model {slug}: expected object with pages")
        count = settings.get("pages", 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError(f"Model {slug}: pages must be a positive integer")
        if not isinstance(settings.get("enabled", True), bool):
            raise ValueError(f"Model {slug}: enabled must be boolean")
    template = config.get("model_path_template", "/models/{model}/")
    if not isinstance(template, str) or template.count("{model}") != 1 or not template.startswith("/"):
        raise ValueError('model_path_template must be like "/models/{model}/"')
    config["model_path_template"] = template
    return config


def get_driver(headless=False):
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(90)
    driver.set_script_timeout(30)
    return driver


def open_page(driver, url):
    try:
        driver.get(url)
    except TimeoutException:
        log.warning("Timeout loading %s; using partially loaded HTML", url)
        try:
            driver.execute_script("window.stop()")
        except Exception:
            pass
    markup = driver.page_source
    if not markup or BeautifulSoup(markup, "html.parser").find("body") is None:
        raise RuntimeError(f"No usable HTML loaded: {url}")
    return markup


def scroll_to_load_all(driver, max_scrolls=50):
    last_height = driver.execute_script("return document.body.scrollHeight")
    unchanged = 0
    for _ in range(max_scrolls):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(SCROLL_PAUSE)
        height = driver.execute_script("return document.body.scrollHeight")
        unchanged = unchanged + 1 if height == last_height else 0
        if unchanged >= 2:
            break
        last_height = height


def same_site(url):
    return urlsplit(url).netloc.lower() == urlsplit(BASE_URL).netloc.lower()


def extract_video_urls(driver):
    soup = BeautifulSoup(driver.page_source, "html.parser")
    urls, seen = [], set()
    for anchor in soup.select("a[href], a[data-href]"):
        href = anchor.get("data-href") or anchor.get("href", "")
        url = urldefrag(urljoin(driver.current_url, href))[0]
        if same_site(url) and "/video/" in urlsplit(url).path and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def find_next_page(markup, current_url, listing_root):
    """Use the old Next/numbered pagination logic, but stay within this listing."""
    soup = BeautifulSoup(markup, "html.parser")
    candidates = list(soup.select(
        'a[rel~="next"], link[rel~="next"], a.next, .next a, '
        'a[aria-label="Next"], a[aria-label="Next page"]'
    ))
    pagination = soup.select(".pagination, .page-numbers, .pager, nav[aria-label*='agination']")
    for anchor in soup.select("a[href]"):
        label = " ".join((anchor.get_text(" ", strip=True), anchor.get("aria-label", ""), anchor.get("title", ""))).lower().strip()
        if re.search(r"\bnext(?:\s+page)?\b|\bolder posts\b", label) or label in {"›", "»", "→", ">", ">>"}:
            candidates.append(anchor)

    listing_path = urlsplit(listing_root).path.rstrip("/")

    def valid_listing(url):
        if not same_site(url) or url == urldefrag(current_url)[0]:
            return False
        path = urlsplit(url).path.rstrip("/")
        if "/video/" in path.lower():
            return False
        if listing_path:
            return path == listing_path or path.startswith(listing_path + "/")
        return not re.search(r"/(?:models?|video)/", path.lower() + "/")

    for element in candidates:
        parent_classes = element.parent.get("class", []) if element.parent else []
        disabled = " ".join(element.get("class", [])) + " " + " ".join(parent_classes)
        if "disabled" in disabled.lower() or element.get("aria-disabled") == "true":
            continue
        href = element.get("href", "")
        if not href or href.startswith(("#", "javascript:")):
            continue
        url = urldefrag(urljoin(current_url, href))[0]
        if valid_listing(url):
            return url

    current = soup.select_one('.pagination .current, .page-numbers.current, .pagination .active, [aria-current="page"]')
    if current:
        match = re.search(r"\d+", current.get_text(" ", strip=True))
        if match:
            wanted = str(int(match.group()) + 1)
            for container in pagination:
                anchors = [container] if container.name == "a" else container.select("a[href]")
                for anchor in anchors:
                    if anchor.get_text(strip=True) == wanted:
                        url = urldefrag(urljoin(current_url, anchor.get("href", "")))[0]
                        if valid_listing(url):
                            return url
    return None


def get_listing_avatar(driver, kind):
    soup = BeautifulSoup(driver.page_source, "html.parser")
    if kind != "home":
        image = soup.select_one("img.avatar, img.model-avatar, div.avatar img, img[alt*='model'], img.photo, img[src*='avatar']")
        if image and image.get("src"):
            return urljoin(driver.current_url, image["src"])
    image = soup.select_one('meta[property="og:image"]')
    return urljoin(driver.current_url, image["content"]) if image and image.get("content") else ""


def parse_video_info(markup, video_url):
    """Old model scraper's original video extraction flow, unchanged."""
    soup = BeautifulSoup(markup, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else "Unknown"
    title = re.sub(r"\s*[-|]\s*pornhao.*$", "", title, flags=re.I).strip() or "Unknown"
    image = soup.find("meta", property="og:image")
    video = soup.find("video")
    thumbnail = image.get("content", "") if image else ""
    if not thumbnail and video:
        thumbnail = video.get("poster", "")
    if not thumbnail:
        image = soup.find("img", class_=re.compile(r"thumb|poster|cover|preview"))
        thumbnail = image.get("src", "") if image else ""
    thumbnail = urljoin(video_url, thumbnail) if thumbnail else ""
    stream = video.get("src", "") if video else ""
    if not stream:
        source = soup.select_one("video source[src]")
        stream = source.get("src", "") if source else ""
    if not stream:
        text = html.unescape(markup).replace("\\/", "/")
        for extension in ("m3u8", "mp4"):
            match = re.search(
                r'''https?://[^\s"'<>\\]+?\.''' + extension + r'''(?:\?[^\s"'<>\\]*)?''',
                text, flags=re.I
            )
            if match:
                stream = match.group()
                break
    if stream:
        return {"url": urljoin(video_url, html.unescape(stream)), "title": title, "thumbnail": thumbnail}
    return None


def get_video_info(video_url, session=None, driver=None):
    session = session or requests.Session()
    try:
        response = session.get(video_url, timeout=30, headers={"Referer": BASE_URL + "/"})
        response.raise_for_status()
        info = parse_video_info(response.text, video_url)
        if info:
            return info
    except requests.RequestException as exc:
        log.warning("Video page request failed: %s", type(exc).__name__)
    if driver is not None:
        try:
            open_page(driver, video_url)
        except RuntimeError:
            log.warning("Video page had no usable HTML: %s", video_url)
            return None
        try:
            WebDriverWait(driver, 10).until(
                lambda d: parse_video_info(d.page_source, video_url) is not None
            )
        except TimeoutException:
            return None
        return parse_video_info(driver.page_source, video_url)
    return None


def parse_qualities(raw_stream):
    """Read all quality variants BEFORE trimming stream_url."""
    raw_stream = html.unescape(str(raw_stream or "")).replace("\\/", "/").strip()
    if not raw_stream:
        return {}
    parts = re.split(r",\s*\[([^\]]+)\]\s*", raw_stream)
    qualities = {"Auto": parts[0].strip()}
    for index in range(1, len(parts) - 1, 2):
        label, url = parts[index].strip(), parts[index + 1].strip()
        if label and url:
            qualities[label] = url
    return qualities


def discover_listing(driver, label, start_url, pages):
    page = start_url
    visited, seen, video_pages = set(), set(), []
    avatar = ""
    while page and page not in visited and len(visited) < pages:
        log.info("%s: listing page %s/%s: %s", label, len(visited) + 1, pages, page)
        visited.add(page)
        open_page(driver, page)
        try:
            WebDriverWait(driver, 15).until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'a[href*="/video/"], a[data-href*="/video/"]')
            ))
        except TimeoutException:
            log.warning("%s: no video links yet on listing page", label)
        try:
            scroll_to_load_all(driver)
        except TimeoutException:
            log.warning("%s: scrolling timed out; collecting available links", label)
        avatar = avatar or get_listing_avatar(driver, label)
        for url in extract_video_urls(driver):
            if url not in seen:
                seen.add(url)
                video_pages.append(url)
        page = find_next_page(driver.page_source, driver.current_url, start_url)
        time.sleep(REQUEST_DELAY)
    log.info("%s: found %s unique video pages across %s listing pages", label, len(video_pages), len(visited))
    return video_pages, avatar


def scrape_all(config, headless=False):
    sources = []
    home = config.get("home", {})
    if home.get("enabled", True):
        sources.append(("home", BASE_URL + "/", home.get("pages", 2)))
    template = config["model_path_template"]
    for model, settings in config.get("models", {}).items():
        if settings.get("enabled", True):
            path = template.replace("{model}", model)
            sources.append((model, urljoin(BASE_URL + "/", path.lstrip("/")), settings.get("pages", 1)))
    if not sources:
        raise ValueError("No enabled sources in scrape_config.json")

    driver = get_driver(headless)
    results = []
    try:
        with requests.Session() as session:
            # Each source is processed independently, in config order: home first.
            for source_name, start_url, page_count in sources:
                video_pages, avatar = discover_listing(driver, source_name, start_url, page_count)
                if not video_pages:
                    log.warning("%s: no video links found", source_name)
                    continue
                session.headers["User-Agent"] = driver.execute_script("return navigator.userAgent")
                for cookie in driver.get_cookies():
                    session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""))
                source_saved = 0
                for index, url in enumerate(video_pages, 1):
                    log.info("%s: processing video %s/%s", source_name, index, len(video_pages))
                    try:
                        info = get_video_info(url, session, driver)
                    except Exception:
                        log.exception("%s: video processing failed: %s", source_name, url)
                        info = None
                    if info:
                        raw_stream = info["url"]
                        qualities = parse_qualities(raw_stream)
                        auto_url = qualities.get("Auto", "").strip()
                        if auto_url:
                            results.append({
                                "category": source_name,
                                "title": info["title"],
                                "page_url": url,
                                "stream_url": auto_url,
                                "thumbnail": info["thumbnail"] or avatar,
                                "qualities": qualities,
                            })
                            source_saved += 1
                    else:
                        log.warning("%s: no stream found for video %s/%s", source_name, index, len(video_pages))
                    time.sleep(REQUEST_DELAY)
                log.info("%s: saved %s videos", source_name, source_saved)
                if source_saved == 0:
                    raise RuntimeError(f"No streams found for {source_name}; preserving existing playlists")
    finally:
        driver.quit()
    if not results:
        raise RuntimeError("No streams found in any configured source; existing playlists preserved")
    return results


def clean_attribute(value):
    return re.sub(r"[\r\n]+", " ", str(value)).replace('"', "'")


def save_playlist(entries, json_file="playlist.json", m3u_file="playlist.m3u", display_prefix="⭐ "):
    lines = ["#EXTM3U", ""]
    for item in entries:
        category = item["category"]
        title = clean_attribute(display_prefix + item.get("title", category))
        lines.append(
            f'#EXTINF:-1 tvg-id="{clean_attribute(category.lower())}" '
            f'tvg-name="{clean_attribute(category.replace("-", " ").title())}" '
            f'tvg-logo="{clean_attribute(item.get("thumbnail", ""))}" '
            f'group-title="{clean_attribute(category)}",{title}'
        )
        lines.extend([item["stream_url"], ""])
    outputs = [
        (Path(json_file), json.dumps(entries, indent=2, ensure_ascii=False) + "\n"),
        (Path(m3u_file), "\n".join(lines)),
    ]
    for path, content in outputs:
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(content, encoding="utf-8")
    for path, _ in outputs:
        path.with_name(path.name + ".tmp").replace(path)
    log.info("Saved %s entries to %s and %s", len(entries), json_file, m3u_file)


def main():
    global BASE_URL
    parser = argparse.ArgumentParser(description="Scrape homepage first, then configured models")
    parser.add_argument("--config", default="scrape_config.json")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--output-json", default="playlist.json")
    parser.add_argument("--output-m3u", default="playlist.m3u")
    parser.add_argument("--display-prefix", default="⭐ ")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    BASE_URL = args.base_url.rstrip("/")
    if urlsplit(BASE_URL).scheme not in {"http", "https"} or not urlsplit(BASE_URL).netloc:
        parser.error("--base-url must be an HTTP or HTTPS URL")
    try:
        config = load_config(args.config)
        log.info("Scraper version: %s; website: %s; config: %s", SCRIPT_VERSION, BASE_URL, args.config)
        entries = scrape_all(config, args.headless)
        save_playlist(entries, args.output_json, args.output_m3u, args.display_prefix)
    except Exception:
        log.exception("Scrape failed. Existing playlist files preserved if scraping failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
