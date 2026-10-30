# Running devflow as a service

devflow is one long-running process (`devflow run`). Run it as **your own
user**, never as root or SYSTEM: it uses your Claude Code login, your
opencode providers and your git credentials, and its files are private to
you. Check the setup first with `devflow doctor`.

The examples assume devflow was installed with pipx, so the command is
`~/.local/bin/devflow` (Linux, macOS) or
`%USERPROFILE%\.local\bin\devflow.exe` (Windows). Use `which devflow` /
`where devflow` to find yours.

The tools devflow starts (`claude`, `opencode`, `git`, `php`, `npm`, …) must be
on the service's `PATH`, which is often shorter than your shell's. Set it
explicitly as shown.

## Linux: systemd user service

`~/.config/systemd/user/devflow.service`:

```ini
[Unit]
Description=devflow (Telegram-driven AI coding workflow)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=%h/.local/bin/devflow run
Restart=on-failure
RestartSec=30
# Where claude, opencode, git and your toolchains live:
Environment=PATH=%h/.local/bin:%h/.bun/bin:/usr/local/bin:/usr/bin:/bin
# Keep the token out of the config file if you prefer:
# EnvironmentFile=%h/.config/devflow/env   (TELEGRAM_TOKEN=..., mode 0600)
# Let a worker's run finish its phase before stopping:
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now devflow
loginctl enable-linger "$USER"     # keep it running when you are logged out
journalctl --user -u devflow -f    # console output; devflow's own logs are in its runtime folder
```

## macOS: launchd agent

`~/Library/LaunchAgents/dev.devflow.plist` (replace `YOU` with your user name):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.devflow</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/bin/devflow</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/Users/YOU/.local/bin:/Users/YOU/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/devflow.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/devflow.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.devflow.plist
launchctl kickstart -k gui/$(id -u)/dev.devflow   # restart after an upgrade
launchctl bootout gui/$(id -u)/dev.devflow        # stop
```

A LaunchAgent runs while you are logged in, which is what Claude Code's
keychain login needs.

## Windows: Task Scheduler

Run at logon, as you, restarted if it stops. In PowerShell:

```powershell
$devflow = (Get-Command devflow).Source
$action  = New-ScheduledTaskAction -Execute $devflow -Argument "run"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "devflow" -Action $action -Trigger $trigger -Settings $settings `
            -Description "devflow (Telegram-driven AI coding workflow)"
Start-ScheduledTask -TaskName "devflow"
```

The task runs with your user's environment, so `claude`, `opencode` and
`git` resolve as they do in your terminal. Stop it with
`Stop-ScheduledTask -TaskName devflow`; remove it with
`Unregister-ScheduledTask -TaskName devflow`.

Running it as a Windows service (NSSM, `sc.exe`) works too, but the service
must log on as your account, not LocalSystem.

## After a restart

Tasks that were in the middle of a phase are parked as `blocked`
(`blocked_reason: parked`) and announced on Telegram; `/retry <id>` resumes
each. Tasks waiting at a gate are unaffected. With `[limits] auto_resume`,
tasks stopped by a provider limit resume on their own once it resets.
