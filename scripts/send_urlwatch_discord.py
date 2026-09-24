import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import savepagenow
except ImportError:
    savepagenow = None


DISCORD_HARD_LIMIT = 2000
DISCORD_CHUNK_LIMIT = 1450

# Wayback失敗時などに例外メッセージが異常に長くなるケース(archive.orgが
# HTMLのエラーページを返す等)への対策。ここで長さの上限を設けておく。
MAX_STATUS_MESSAGE_LENGTH = 300


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


def extract_url_from_paren_content(raw: str) -> str:
    """
    詳細ヘッダーの括弧の中身からURLを取り出します。

    通常の url: ジョブは括弧の中がそのままURL (Markdown化されていれば
    [url](url) 形式) ですが、command: ジョブ (例: グラフィニカの
    fetch_urlwatch_last_good.py 経由の取得) の場合は括弧の中身が

      python scripts/fetch_urlwatch_last_good.py --url https://example.com/... --last-good ...

    のようにコマンドライン全体になります。以前はこの形式を全く想定して
    おらず、見出し行自体がパース用の正規表現にマッチせず、該当サイトの
    通知がまるごと無言でロストしていました。ここでは括弧の中身に含まれる
    最初のURLを拾うことで、コマンド形式でも正しく通知できるようにします。
    """
    raw = raw.strip()

    md_match = re.match(r"^\[(https?://[^\]]+)\]\((https?://[^)]+)\)$", raw)
    if md_match:
        return normalize_url(md_match.group(2))

    url_match = re.search(r"https?://\S+", raw)
    if url_match:
        return normalize_url(url_match.group(0).rstrip(")"))

    # URLが見当たらない場合(通常は起きない想定)は、そのまま返しておく。
    return normalize_url(raw)


def is_timestamped_wayback_url(url: str) -> bool:
    return bool(re.search(r"/web/\d{14}/", url))


def get_timestamped_wayback_url(original_url: str, fallback_url: str = "") -> str:
    """
    Wayback Availability API から、タイムスタンプ付きの最新スナップショットURLを取得します。

    savepagenow.capture_or_cache() が
      https://web.archive.org/web/https://www.explsn.com/
    のようなタイムスタンプなしURLを返す場合があるため、Discordに出すURLはここで補正します。
    """
    original_url = normalize_url(original_url)
    fallback_url = fallback_url.strip()

    if fallback_url and is_timestamped_wayback_url(fallback_url):
        return fallback_url

    query = urllib.parse.urlencode({"url": original_url})
    api_url = f"https://archive.org/wayback/available?{query}"

    request = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": "urlwatch-github-actions-discord"
        },
        method="GET"
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))

    closest = data.get("archived_snapshots", {}).get("closest", {})
    if closest.get("available"):
        closest_url = closest.get("url", "").strip()
        timestamp = closest.get("timestamp", "").strip()

        if closest_url and is_timestamped_wayback_url(closest_url):
            return closest_url.replace("http://web.archive.org/", "https://web.archive.org/")

        if timestamp:
            return f"https://web.archive.org/web/{timestamp}/{original_url}"

    if fallback_url:
        return fallback_url

    return ""


def parse_urlwatch_report(report_text: str) -> list[dict[str, str]]:
    """
    urlwatch の stdout/text レポートを、1ジョブ=1セクションに分割します。

    urlwatch レポートの先頭には、以下のような概要行が出ることがあります。
      01. CHANGED: A
      02. CHANGED: B

    これをセクション開始として使うと、URLとdiffが次の会社とズレる原因になります。
    そのため、詳細ヘッダーだけをセクション開始として扱います。

    詳細ヘッダー例:
      CHANGED: 採用情報 - A ( https://example.com/ )
      ### CHANGED: 採用情報 - A ( [https://example.com/](https://example.com/) )
      ERROR: 採用情報 - A ( https://example.com/ )
      CHANGED: 採用情報 - グラフィニカ ( python scripts/fetch_urlwatch_last_good.py --url https://example.com/ ... )
    """
    lines = report_text.splitlines()

    # 括弧の中身は「URLそのもの」でも「command: ジョブのコマンドライン」でも
    # 拾えるように、中身は緩めにマッチさせ、URLの抽出は別関数に任せる。
    detail_heading_pattern = re.compile(
        r"^\s*(?:#{1,6}\s*)?"
        r"(NEW|CHANGED|ERROR|UNCHANGED):\s+"
        r"(.+?)\s+"
        r"\(\s*(.+?)\s*\)\s*$"
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
            url = extract_url_from_paren_content(match.group(3))

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

    # 予備: もし詳細ヘッダーがない古い形式の場合だけ、番号付き行で分割します。
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
    # 「変更があった場合」に魚拓を取ります。
    # 初回追加時も取りたいなら NEW を含めたままでOKです。
    return status in {"NEW", "CHANGED"}


def capture_wayback(url: str) -> tuple[str, str]:
    """
    Wayback Machine へ保存を試みます。

    Discordに表示するURLは、必ず可能な限りタイムスタンプ付きURLにします。
    例:
      https://web.archive.org/web/20260512060439/https://www.explsn.com/
    """
    if not url:
        return "", "URLなし"

    if not url.startswith(("http://", "https://")):
        # command: ジョブなどでURLが取り出せなかった場合の保険。
        return "", "Wayback保存スキップ: 有効なURLを取得できませんでした"

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

        # savepagenow の戻り値がタイムスタンプなしの場合があるため、
        # Availability API で最新のタイムスタンプ付きURLに補正する。
        time.sleep(3)
        timestamped_url = get_timestamped_wayback_url(url, archive_url)

        if captured:
            return timestamped_url, "保存しました"
        return timestamped_url, "既存キャッシュを使用しました"

    except Exception as e:
        # archive.org側がHTMLのエラーページ等を返すと、例外メッセージが
        # 数千文字になることがある。Discordメッセージの組み立てで
        # オーバーヘッドが肥大化しないよう、ここで長さを打ち切っておく。
        message = str(e).replace("\n", " ").strip()
        if len(message) > MAX_STATUS_MESSAGE_LENGTH:
            message = message[:MAX_STATUS_MESSAGE_LENGTH] + "…(省略)"
        return "", f"Wayback保存失敗: {message}"


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

    def assemble(body: str) -> str:
        return (
            f"{title}"
            f"{url_line}"
            f"{archive_line}"
            f"{chunk_line}"
            f"\n```diff\n{body}\n```"
        )

    message = assemble(chunk)

    # 数式ベースの見積もりだけに頼らず、最終的な長さを必ずチェックして
    # 2000文字を超えないようにする(タイトルやURL・Waybackステータス側が
    # 想定以上に長くなった場合の保険)。
    if len(message) > DISCORD_HARD_LIMIT:
        over = len(message) - DISCORD_HARD_LIMIT
        shortened_chunk = chunk[: max(0, len(chunk) - over - 20)] + "\n...(省略)"
        message = assemble(shortened_chunk)

    if len(message) > DISCORD_HARD_LIMIT:
        # それでも超える場合(タイトルやURL自体が極端に長い等)は、
        # 本文を思い切って落として最低限の情報だけ残す。
        message = assemble("(内容が長すぎるため省略しました)")

    if len(message) > DISCORD_HARD_LIMIT:
        # 最終手段: メッセージ全体を強制的に切り詰める。
        message = message[: DISCORD_HARD_LIMIT - 4] + "\n..."

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
    failed_count = 0
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

            # 1件の送信失敗でジョブ全体を落とさない。
            # (以前はここで例外が伝播し、以降の全サイトの通知が
            #  送信されないままスクリプトごと異常終了していた)
            try:
                post_to_discord(webhook_url, message)
                sent_count += 1
            except Exception:
                failed_count += 1
                print(
                    f"Failed to send Discord message for '{item['name']}' "
                    f"(chunk {index}/{len(chunks)}):",
                    file=sys.stderr
                )
                traceback.print_exc()

            # Discord連投対策
            time.sleep(1)

    print(
        f"Sent {sent_count} Discord message(s) for {len(items)} site(s). "
        f"({failed_count} failed)"
    )

    # 一部失敗しても他サイトの通知は届いている状態なので、
    # CI上は分かるように失敗があれば非ゼロで終了しつつ、処理自体は最後まで行う。
    return 1 if failed_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
