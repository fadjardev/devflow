"""`python -m devflow` runs the bot; `python -m devflow doctor` checks the setup."""

from __future__ import annotations

import shutil
import subprocess
import sys

from . import config, projects


def doctor(settings: config.Settings) -> int:
    ok = True

    def line(good: bool, what: str, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  {'ok ' if good else 'BAD'}  {what}" + (f" - {detail}" if detail else ""))

    print("devflow doctor")
    line(bool(settings.telegram_token), "TELEGRAM_TOKEN set")
    line(bool(settings.allowed_chat_id), "ALLOWED_CHAT_ID set")
    line((settings.vault / "01-Projects").is_dir(), "vault", str(settings.vault))
    line(settings.projects.is_dir(), "projects", str(settings.projects))
    line(not settings.unsafe_shell, "worker shell restricted",
         "UNSAFE_SHELL is on: the worker can run any command except git" if settings.unsafe_shell
         else f"allowed: {', '.join(settings.worker_shell_allow) or 'nothing'}")
    for exe in ("claude", "opencode", "git"):
        line(bool(shutil.which(exe)), f"{exe} on PATH", shutil.which(exe) or "")
    nginx = settings.nginx_prefix / "current" / "nginx.exe"
    if nginx.is_file():
        t = subprocess.run([str(nginx), "-p", str(settings.nginx_prefix).rstrip("\\/") + "/", "-t"],
                           cwd=settings.nginx_prefix, capture_output=True, text=True)
        line(t.returncode == 0, "nginx -t", (t.stderr.strip().splitlines() or [""])[-1])
    else:
        line(False, "nginx", f"not found at {nginx}")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        line(True, "playwright headless chromium launches")
    except Exception as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        line(False, "playwright headless chromium", f"{first}; run: python -m playwright install chromium-headless-shell")

    print("\nprojects")
    for name in projects.names(settings.vault):
        try:
            pdir = projects.directory(settings, name)
        except ValueError as e:
            print(f"  --   {name}: {e}")
            continue
        vh = projects.find_vhost(settings, pdir)
        php = projects.php_exe(settings, vh.port) if vh else None
        print(f"  {'ok ' if vh else '-- '}  {name}: " +
              (f"https://{vh.host} (php {vh.port}{'' if php else ', no php.exe'})" if vh else "no vhost (HTTP checks skipped)"))
    return 0 if ok else 1


def main() -> None:
    settings = config.load()
    if sys.argv[1:2] == ["doctor"]:
        raise SystemExit(doctor(settings))
    from .bot import main as run_bot
    run_bot(settings)


if __name__ == "__main__":
    main()
