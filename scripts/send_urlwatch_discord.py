import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import savepagenow
except ImportError:
    savepagenow = None


DISCORD_HARD_LIMIT = 2000
DISCORD_CHUNK_LIMIT = 1450


def post_to_discord(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {
            "parse": []
        }
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "urlwatch-github-actions-discord"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.getcode()
            if status < 200 or status >= 300:
                raise RuntimeError(f"Discord webhook returned HTTP {status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord webhook HTTPError {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Discord webhook URLError: {e}") from e


def split_long_text(text: str, limit: int = DISCORD_CHUNK_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current.strip():
                chunks.append(current.rstrip())
                current = ""

            while len(line) > limit:
                chunks.append(line[:limit].rstrip())
                line = line[limit:]

        current += line

    if current.strip():
        chunks.append(current.rstrip())

    return chunks


def normalize_url(url: str) -> str:
    url = url.strip()

    # urlwatch の重複回避用フラグメントを通知・Wayback保存用には削除します。
    # 例:
    # https://example.com/recruit/#1 -> https://example.com/recruit/
    # https://example.com/recruit/#news -> https://example.com/recruit/
    url = re.sub(r"#(?:\d+|news|jobs|button|text|topics-recruit)$", "", url)

    return url


def clean_markdown_url(raw_url: str) -> str:
    raw_url = raw_url.strip()

    # fetch_file 経由などで report.txt が Markdown 化され、
    # [https://example.com](https://example.com) 形式になっていても URL を取り出す。
    md_match = re.match(r"\[(https?://[^\]]+)\]\((https?://[^)]+)\)", raw_url)
    if md_match:
        return normalize_url(md_match.group(2))

    return normalize_url(raw_url)


def parse_urlwatch_report(report_text: str) -> list[dict[str, str]]:
    """
    urlwatch の stdout/text レポートを、1ジョブ=1セクションに分割する。

    重要:
    urlwatch レポートの先頭には、以下のような概要行が出ることがあります。
      01. CHANGED: A
      02. CHANGED: B

    これをセクション開始として使うと、URLとdiffが次の会社とズレる原因になります。
    そのため、詳細ヘッダーだけをセクション開始として扱います。

    詳細ヘッダー例:
      CHANGED: 採用情報 - A ( https://example.com/ )
      ### CHANGED: 採用情報 - A ( [https://example.com/](https://example.com/) )
      ERROR: 採用情報 - A ( https://example.com/ )
    """
    lines = report_text.splitlines()

    detail_heading_pattern = re.compile(
        r"^\s*(?:#{1,6}\s*)?"
        r"(NEW|CHANGED|ERROR|UNCHANGED):\s+"
        r"(.+?)\s+"
        r"\(\s*(\[https?://[^\]]+\]\(https?://[^)]+\)|https?://[^\s)]+)\s*\)\s*$"
    )

    starts: list[tuple[int, re.Match[str]]] = []

    for index, line in enumerate(lines):
        match = detail_heading_pattern.match(line)
        if match:
            starts.append((index, match))

    results: list[dict[str, str]] = []

    # 詳細ヘッダーが取れる通常ケース
    if starts:
        for pos, (start_index, match) in enumerate(starts):
            end_index = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
            section_lines = lines[start_index:end_index]
            section_text = "\n".join(section_lines).strip()

            status = match.group(1)
            name = match.group(2).strip()
            url = clean_markdown_url(match.group(3))

            if status == "UNCHANGED":
                continue

            results.append(
                {
                    "status": status,
                    "name": name,
                    "url": url,
                    "body": section_text,
                }
            )

        return results

    # 予備: もし詳細ヘッダーがない古い形式の場合だけ、番号付き行で分割する。
    fallback_heading_pattern = re.compile(
        r"^\s*\d+\.\s+(NEW|CHANGED|ERROR|UNCHANGED):\s+(.+?)\s*$"
    )

    sections: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if fallback_heading_pattern.match(line):
            if current:
                sections.append(current)
            current = [line]
        else:
            if current:
                current.append(line)

    if current:
        sections.append(current)

    for section_lines in sections:
        section_text = "\n".join(section_lines).strip()
        first_line = section_lines[0].strip()
        heading_match = fallback_heading_pattern.match(first_line)

        if not heading_match:
            continue

        status = heading_match.group(1)
        name = heading_match.group(2).strip()

        if status == "UNCHANGED":
            continue

        url = ""
        url_match = re.search(r"\(\s*(https?://[^\s)]+)\s*\)", section_text)
        if url_match:
            url = normalize_url(url_match.group(1))

        results.append(
            {
                "status": status,
                "name": name,
                "url": url,
                "body": section_text,
            }
        )

    return results


def should_archive_with_wayback(status: str) -> bool:
    # 「変更があった場合」に魚拓を取る。
    # 初回追加時も取りたいなら NEW を含めたままでOK。
    return status in {"NEW", "CHANGED"}


def capture_wayback(url: str) -> tuple[str, str]:
    if not url:
        return "", "URLなし"

    wayback_enabled = os.environ.get("WAYBACK_ENABLED", "false").lower() == "true"
    if not wayback_enabled:
        return "", "Wayback保存無効"

    if savepagenow is None:
        return "", "Wayback保存失敗: savepagenow がインストールされていません"

    access_key = os.environ.get("SAVEPAGENOW_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("SAVEPAGENOW_SECRET_KEY", "").strip()
    authenticate = bool(access_key and secret_key)

    try:
        archive_url, captured = savepagenow.capture_or_cache(
            url,
            authenticate=authenticate
        )

        if captured:
            return archive_url, "保存しました"
        return archive_url, "既存キャッシュを使用しました"

    except Exception as e:
        return "", f"Wayback保存失敗: {e}"


def build_message(
    item: dict[str, str],
    archive_url: str,
    archive_status: str,
    chunk: str,
    index: int,
    total: int
) -> str:
    status_label = {
        "NEW": "新規検知",
        "CHANGED": "変更検知",
        "ERROR": "エラー",
        "UNCHANGED": "変更なし",
    }.get(item["status"], item["status"])

    title = f"**{status_label}: {item['name']}**"

    url_line = ""
    if item["url"]:
        url_line = f"\nURL: {item['url']}"

    archive_line = ""
    if archive_url:
        archive_line = f"\nWayback: {archive_url}"
    elif archive_status:
        archive_line = f"\nWayback: {archive_status}"

    chunk_line = ""
    if total > 1:
        chunk_line = f"\n分割: {index}/{total}"

    message = (
        f"{title}"
        f"{url_line}"
        f"{archive_line}"
        f"{chunk_line}"
        f"\n```diff\n{chunk}\n```"
    )

    # Discordの2000文字制限を超えないように最終保険をかける。
    if len(message) > DISCORD_HARD_LIMIT:
        over = len(message) - DISCORD_HARD_LIMIT
        shortened_chunk = chunk[: max(0, len(chunk) - over - 20)] + "\n..."
        message = (
            f"{title}"
            f"{url_line}"
            f"{archive_line}"
            f"{chunk_line}"
            f"\n```diff\n{shortened_chunk}\n```"
        )

    return message


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/send_urlwatch_discord.py report.txt", file=sys.stderr)
        return 1

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        return 1

    report_path = Path(sys.argv[1])
    if not report_path.exists():
        print(f"Report file not found: {report_path}", file=sys.stderr)
        return 1

    report_text = report_path.read_text(encoding="utf-8", errors="replace")
    items = parse_urlwatch_report(report_text)

    if not items:
        print("No NEW / CHANGED / ERROR sections found. Nothing to send.")
        return 0

    sent_count = 0
    wayback_sleep_seconds = int(os.environ.get("WAYBACK_SLEEP_SECONDS", "0"))

    for item in items:
        archive_url = ""
        archive_status = ""

        if should_archive_with_wayback(item["status"]):
            archive_url, archive_status = capture_wayback(item["url"])

            if wayback_sleep_seconds > 0:
                time.sleep(wayback_sleep_seconds)

        chunks = split_long_text(item["body"])

        for index, chunk in enumerate(chunks, start=1):
            message = build_message(
                item=item,
                archive_url=archive_url,
                archive_status=archive_status,
                chunk=chunk,
                index=index,
                total=len(chunks)
            )
            post_to_discord(webhook_url, message)
            sent_count += 1

            # Discord連投対策
            time.sleep(1)

    print(f"Sent {sent_count} Discord message(s) for {len(items)} site(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
