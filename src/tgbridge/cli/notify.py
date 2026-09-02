"""tgnotify — отправить уведомление в Telegram через VPS.

Примеры:
    tgnotify "сборка готова"
    tgnotify -l error "тесты упали"
    echo "долгий вывод" | tgnotify -l warn -
"""

from __future__ import annotations

import argparse
import sys

import httpx

from ..config import load


def main() -> int:
    p = argparse.ArgumentParser(prog="tgnotify", description="отправить уведомление в Telegram")
    p.add_argument("text", help="текст сообщения, или '-' чтобы прочитать stdin")
    p.add_argument("-l", "--level", choices=["info", "warn", "error"], default="info")
    p.add_argument(
        "--session",
        default="",
        metavar="ID",
        help="id сессии Claude — прицепить кнопку «Перейти к сессии»",
    )
    args = p.parse_args()

    text = sys.stdin.read().strip() if args.text == "-" else args.text
    if not text:
        print("tgnotify: пустой текст", file=sys.stderr)
        return 2

    body = {"text": text[:4000], "level": args.level}
    if args.session:
        body["session_id"] = args.session

    cfg = load()
    try:
        r = httpx.post(
            f"{cfg.server_url}/notify",
            headers={"Authorization": f"Bearer {cfg.token}"},
            json=body,
            timeout=10,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"tgnotify: не доставлено: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
