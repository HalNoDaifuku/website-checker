import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DISCORD_MESSAGE_LIMIT = 1900


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


def split_long_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
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

    # urlwatch の重複回避用 #1, #2, #news などは通知上では消す
    # ただし本当に意味のあるアンカーを使っている場合は消したくない可能性があるので、
    # 必要ならこの処理をコメントアウトしてください。
    url = re.sub(r"#(?:\d+|news|jobs|button|text|topics-recruit)$", "", url)

    return url


def parse_urlwatch_report(report_text: str) -> list[dict[str, str]]:
    lines = report_text.splitlines()

    sections: list[list[str]] = []
    current: list[str] = []

    heading_pattern = re.compile(
        r"^\d+\.\s+(NEW|CHANGED|ERROR|UNCHANGED):\s+(.+)$"
    )

    for line in lines:
        if heading_pattern.match(line):
            if current:
                sections.append(current)
            current = [line]
        else:
            if current:
                current.append(line)

    if current:
        sections.append(current)

    results: list[dict[str, str]] = []

    for section_lines in sections:
        section_text = "\n".join(section_lines).strip()

        first_line = section_lines[0].strip()
        heading_match = heading_pattern.match(first_line)

        if not heading_match:
            continue

        status = heading_match.group(1)
        name = heading_match.group(2).strip()

        # 変更なしは通知しない
        if status == "UNCHANGED":
            continue

        url = ""

        # 例:
        # CHANGED: 採用情報 - OLM ( https://www.olm.co.jp/new-graduate )
        # ERROR: Example ( https://example.com/ )
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


def build_message(item: dict[str, str], chunk: str, index: int, total: int) -> str:
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

    chunk_line = ""
    if total > 1:
        chunk_line = f"\n分割: {index}/{total}"

    return (
        f"{title}"
        f"{url_line}"
        f"{chunk_line}"
        f"\n```diff\n{chunk}\n```"
    )


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

    for item in items:
        chunks = split_long_text(item["body"])

        for index, chunk in enumerate(chunks, start=1):
            message = build_message(item, chunk, index, len(chunks))
            post_to_discord(webhook_url, message)
            sent_count += 1

            # Discordの連投対策
            time.sleep(1)

    print(f"Sent {sent_count} Discord message(s) for {len(items)} site(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
