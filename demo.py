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
                                "model": source_name,
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
        model = item["model"]
        title = clean_attribute(display_prefix + item.get("title", model))
        lines.append(
            f'#EXTINF:-1 tvg-id="{clean_attribute(model.lower())}" '
            f'tvg-name="{clean_attribute(model.replace("-", " ").title())}" '
            f'tvg-logo="{clean_attribute(item.get("thumbnail", ""))}" '
            f'group-title="{clean_attribute(model)}",{title}'
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
