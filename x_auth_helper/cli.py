"""Login to X via Chrome and save Playwright storage state."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _emit(payload: dict) -> int:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0 if payload.get("status") == "ok" else 1


def _is_logged_in(page) -> bool:
    selectors = (
        '[data-testid="SideNav_NewTweet_Button"]',
        '[data-testid="SideNav_AccountSwitcher_Button"]',
        'a[data-testid="AppTabBar_Home_Link"]',
    )
    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue

    try:
        cookies = page.context.cookies()
        if any(c.get("name") == "auth_token" for c in cookies):
            return True
    except Exception:
        pass

    url = page.url.lower()
    return "/home" in url and "/login" not in url and "/i/flow/login" not in url


def _x_cookie(name: str, value: str, *, domain: str = ".x.com") -> dict:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": -1,
        "httpOnly": name in {"auth_token", "twid"},
        "secure": True,
        "sameSite": "None",
    }


def build_storage_state(auth_token: str, ct0: str) -> dict:
    """Build Playwright storage_state JSON from X session cookies."""
    token = auth_token.strip()
    csrf = ct0.strip()
    if not token or not csrf:
        raise ValueError("auth_token and ct0 are required")

    cookies = []
    for domain in (".x.com", ".twitter.com"):
        cookies.append(_x_cookie("auth_token", token, domain=domain))
        cookies.append(_x_cookie("ct0", csrf, domain=domain))
    return {"cookies": cookies, "origins": []}


def _write_storage_state(path: Path, auth_token: str, ct0: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_storage_state(auth_token, ct0)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _verify_storage_state(storage_path: Path) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright not installed"

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, headless=True)
        context = browser.new_context(storage_state=str(storage_path))
        page = context.new_page()
        try:
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
            ok = _is_logged_in(page)
            if not ok:
                return False, "not logged in at x.com/home — cookies may be expired"
            return True, "ok"
        except Exception as exc:
            return False, str(exc)
        finally:
            browser.close()


def command_import_cookies(args: argparse.Namespace) -> int:
    storage_path = Path(args.storage_state).expanduser().resolve()
    auth_token = args.auth_token or ""
    ct0 = args.ct0 or ""

    if args.cookies_file:
        text = Path(args.cookies_file).expanduser().read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key in {"X_AUTH_TOKEN", "AUTH_TOKEN", "auth_token"}:
                auth_token = val
            elif key in {"X_CT0", "CT0", "ct0"}:
                ct0 = val

    if not auth_token or not ct0:
        return _emit(
            {
                "status": "error",
                "message": "auth_token and ct0 required (args or --cookies-file)",
            }
        )

    try:
        _write_storage_state(storage_path, auth_token, ct0)
    except ValueError as exc:
        return _emit({"status": "error", "message": str(exc)})

    if args.verify:
        ok, detail = _verify_storage_state(storage_path)
        if not ok:
            storage_path.unlink(missing_ok=True)
            return _emit(
                {
                    "status": "error",
                    "message": f"Session verify failed: {detail}",
                }
            )

    return _emit({"status": "ok", "storage_state_path": str(storage_path), "method": "import-cookies"})


def _launch_browser(playwright, *, headless: bool):
    launch_kwargs = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return playwright.chromium.launch(channel="chrome", **launch_kwargs)
    except Exception:
        _log("Chrome channel unavailable — using bundled Chromium")
        return playwright.chromium.launch(**launch_kwargs)


def command_login(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _emit(
            {
                "status": "error",
                "message": "pip install playwright && playwright install chrome",
            }
        )

    storage_path = Path(args.storage_state).expanduser().resolve()
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    _log("")
    _log("=== X 로그인 (구독 계정으로 로그인) ===")
    _log("Chrome 창이 열립니다. X에 로그인·2FA까지 완료하세요.")
    _log(f"세션 저장 위치: {storage_path}")
    if not args.headless:
        _log("로그인이 끝나면 이 터미널로 돌아와 Enter 키를 누르세요.")
    _log("")

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright, headless=args.headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)

            logged_in = False
            if args.headless:
                deadline = time.time() + args.timeout_seconds
                while time.time() < deadline:
                    if _is_logged_in(page):
                        logged_in = True
                        break
                    time.sleep(1.0)
            else:
                try:
                    input("로그인 완료 후 Enter: ")
                except EOFError:
                    _log("stdin 없음 — 자동 감지 모드로 전환")
                    deadline = time.time() + args.timeout_seconds
                    while time.time() < deadline:
                        if _is_logged_in(page):
                            logged_in = True
                            break
                        time.sleep(1.0)
                else:
                    try:
                        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60_000)
                    except Exception:
                        pass
                    logged_in = _is_logged_in(page)

            if not logged_in:
                return _emit(
                    {
                        "status": "error",
                        "message": (
                            "로그인 확인 실패. Chrome에서 x.com/home 이 보이는지 확인 후 다시 실행하세요."
                        ),
                    }
                )

            _log("로그인 확인됨. 세션 저장 중...")
            context.storage_state(path=str(storage_path))
            try:
                storage_path.chmod(0o600)
            except OSError:
                pass
        finally:
            browser.close()

    if not storage_path.is_file() or storage_path.stat().st_size < 100:
        return _emit(
            {
                "status": "error",
                "message": f"세션 파일이 생성되지 않았습니다: {storage_path}",
            }
        )

    return _emit({"status": "ok", "storage_state_path": str(storage_path)})


def command_status(args: argparse.Namespace) -> int:
    path = Path(args.storage_state).expanduser()
    exists = path.is_file()
    return _emit(
        {
            "status": "ok" if exists else "missing",
            "storage_state_path": str(path),
            "exists": exists,
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="X browser session helper for pba-toss-sync")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Save logged-in X session")
    login.add_argument("--storage-state", required=True)
    login.add_argument("--url", default="https://x.com/login")
    login.add_argument("--timeout-seconds", type=int, default=600)
    login.add_argument("--headless", action="store_true")

    imp = sub.add_parser("import-cookies", help="WSL/headless: import auth_token+ct0 from Windows Chrome")
    imp.add_argument("--storage-state", required=True)
    imp.add_argument("--auth-token", default="")
    imp.add_argument("--ct0", default="")
    imp.add_argument(
        "--cookies-file",
        default="",
        help="Env file with X_AUTH_TOKEN=... and X_CT0=... (chmod 600)",
    )
    imp.add_argument("--verify", action="store_true", default=True)
    imp.add_argument("--no-verify", action="store_false", dest="verify")

    status = sub.add_parser("status", help="Check session file")
    status.add_argument("--storage-state", required=True)

    args = parser.parse_args(argv)
    if args.command == "login":
        return command_login(args)
    if args.command == "import-cookies":
        return command_import_cookies(args)
    return command_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
