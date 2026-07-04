import argparse
import re
import sys
import time
from pathlib import Path

import html2text
import requests


def html_to_text(html: str) -> str:
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    converter.ignore_emphasis = False
    converter.ignore_links = False
    text = converter.handle(html)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line != "").strip()


def looks_like_error(text: str, patterns: list[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def fetch_once(url: str, timeout: int, user_agent: str) -> str:
    response = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": user_agent,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    response.raise_for_status()
    return response.text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--last-good", required=True)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--sleep", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--error-pattern",
        action="append",
        default=[],
        help="この文字列が取得結果に含まれる場合、一時エラーとして扱う。複数指定可。",
    )
    parser.add_argument(
        "--user-agent",
        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    )
    args = parser.parse_args()

    default_error_patterns = [
        "システムエラー",
        "アプリケーションに障害が発生中です",
        "ご迷惑をおかけしております",
        "しばらく時間をおいてからアクセスをお願いします",
    ]
    error_patterns = args.error_pattern or default_error_patterns

    last_good_path = Path(args.last_good)
    last_good_path.parent.mkdir(parents=True, exist_ok=True)

    last_error = ""
    total_attempts = args.retries + 1

    for attempt in range(1, total_attempts + 1):
        try:
            html = fetch_once(args.url, args.timeout, args.user_agent)
            text = html_to_text(html)

            if looks_like_error(text, error_patterns):
                last_error = f"一時エラーページを検出しました attempt={attempt}/{total_attempts}"
            else:
                last_good_path.write_text(text, encoding="utf-8")
                print(text)
                return 0

        except Exception as exc:
            last_error = f"取得失敗 attempt={attempt}/{total_attempts}: {exc}"

        if attempt < total_attempts:
            time.sleep(args.sleep)

    # 2回再試行しても正常ページが取れなかった場合は、前回の正常取得内容を出力する。
    # これにより urlwatch には「前回と同じ内容」として見せ、CHANGED扱いを避ける。
    if last_good_path.exists():
        print(last_good_path.read_text(encoding="utf-8", errors="replace"))
        print(f"[temporary fetch error ignored: {last_error}]", file=sys.stderr)
        return 0

    # 初回実行などで last_good がまだない場合だけ、urlwatch側にERRORとして伝える。
    print(f"No last-good cache exists and fetch failed: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
