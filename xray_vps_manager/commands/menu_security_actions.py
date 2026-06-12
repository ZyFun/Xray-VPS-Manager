"""Security and SSH actions used by the interactive menu."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

SSHD_CONFIG_PATH = Path("/etc/ssh/sshd_config")
SSHD_DROPIN_PATH = Path("/etc/ssh/sshd_config.d/00-xray-vps-manager.conf")
SSHD_LEGACY_DROPIN_PATH = Path("/etc/ssh/sshd_config.d/99-xray-vps-manager.conf")
AUTHORIZED_KEYS_PATH = Path("/root/.ssh/authorized_keys")
GREEN = "\033[92m"
RED = "\033[31m"
RESET = "\033[0m"

ConfirmCallback = Callable[[str], bool]
RecordAuditCallback = Callable[[], datetime]
FormatTimeCallback = Callable[[datetime], str]


def color(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"{code}{text}{RESET}"


def green(text: str) -> str:
    return color(text, GREEN)


def red(text: str) -> str:
    return color(text, RED)


def sshd_binary() -> str | None:
    for candidate in ("/usr/sbin/sshd", "/usr/local/sbin/sshd", shutil.which("sshd")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def sshd_effective_config() -> tuple[dict[str, str] | None, str]:
    binary = sshd_binary()
    if not binary:
        return None, "sshd не найден."
    result = subprocess.run(
        [binary, "-T"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "sshd -T завершился с ошибкой."
        return None, message
    settings = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            settings[parts[0].lower()] = parts[1].strip()
    return settings, ""


def validate_sshd_config() -> tuple[bool, str]:
    binary = sshd_binary()
    if not binary:
        return False, "sshd не найден."
    result = subprocess.run(
        [binary, "-t"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    message = result.stderr.strip() or result.stdout.strip()
    return result.returncode == 0, message


def root_authorized_key_count() -> int:
    if not AUTHORIZED_KEYS_PATH.exists():
        return 0
    count = 0
    for line in AUTHORIZED_KEYS_PATH.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def sshd_config_includes_manager_dropin() -> bool:
    if not SSHD_CONFIG_PATH.exists():
        return False
    include_line = f"include {SSHD_DROPIN_PATH}".lower()
    for line in SSHD_CONFIG_PATH.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.lower() == include_line:
            return True
    return False


def ensure_sshd_dropin_include() -> bool:
    if sshd_config_includes_manager_dropin():
        return False
    lines = SSHD_CONFIG_PATH.read_text(errors="ignore").splitlines()
    include_block = [
        "# Added by Xray VPS Manager",
        f"Include {SSHD_DROPIN_PATH}",
        "",
    ]
    lines = include_block + lines
    SSHD_CONFIG_PATH.write_text("\n".join(lines) + "\n")
    return True


def remove_sshd_dropin_include() -> bool:
    if not SSHD_CONFIG_PATH.exists():
        return False
    target = f"include {SSHD_DROPIN_PATH}".lower()
    lines = SSHD_CONFIG_PATH.read_text(errors="ignore").splitlines()
    new_lines = []
    removed = False
    skip_next_blank = False

    for line in lines:
        stripped = line.strip()
        if stripped.lower() == target:
            removed = True
            if new_lines and new_lines[-1].strip() == "# Added by Xray VPS Manager":
                new_lines.pop()
            skip_next_blank = True
            continue
        if skip_next_blank and not stripped:
            skip_next_blank = False
            continue
        skip_next_blank = False
        new_lines.append(line)

    if removed:
        SSHD_CONFIG_PATH.write_text("\n".join(new_lines) + "\n")
    return removed


def write_sshd_password_dropin() -> None:
    SSHD_DROPIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSHD_DROPIN_PATH.write_text(
        "\n".join(
            [
                "# Managed by Xray VPS Manager.",
                "# Password SSH logins are disabled; public-key SSH remains enabled.",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "ChallengeResponseAuthentication no",
                "PubkeyAuthentication yes",
                "",
            ]
        )
    )
    SSHD_DROPIN_PATH.chmod(0o644)


def remove_legacy_sshd_dropin() -> bool:
    if SSHD_LEGACY_DROPIN_PATH.exists():
        SSHD_LEGACY_DROPIN_PATH.unlink()
        return True
    return False


def backup_existing_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = Path(f"{path}.bak.{timestamp}")
    shutil.copy2(path, backup)
    return backup


def restore_text_file(path: Path, text: str | None) -> None:
    if text is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if path in (SSHD_DROPIN_PATH, SSHD_LEGACY_DROPIN_PATH):
        path.chmod(0o644)


def reload_sshd_service() -> bool:
    attempts = [
        ("reload", "ssh"),
        ("reload", "sshd"),
        ("restart", "ssh"),
        ("restart", "sshd"),
    ]
    errors = []
    for action, unit in attempts:
        result = subprocess.run(
            ["systemctl", action, unit],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            print(green(f"SSH service обновлён через: systemctl {action} {unit}"))
            return True
        errors.append(f"systemctl {action} {unit}: {(result.stderr or result.stdout).strip()}")
    print(red("Не удалось перезагрузить ssh/sshd через systemctl."))
    for error in errors:
        if error.strip():
            print(error)
    return False


def ssh_password_login_disabled(settings: dict[str, str] | None) -> bool:
    if not settings:
        return False
    password = settings.get("passwordauthentication")
    keyboard = settings.get("kbdinteractiveauthentication")
    challenge = settings.get("challengeresponseauthentication")
    return password == "no" and keyboard == "no" and challenge in (None, "no")


def ssh_password_login_enabled(settings: dict[str, str] | None) -> bool:
    if not settings:
        return False
    return settings.get("passwordauthentication") == "yes"


def format_ssh_setting(key: str, value: str) -> str:
    if value == "unknown":
        return value
    if key == "pubkeyauthentication":
        return green(value) if value == "yes" else red(value)
    if key in ("passwordauthentication", "kbdinteractiveauthentication", "challengeresponseauthentication"):
        return green(value) if value == "no" else red(value)
    return value


def show_ssh_access() -> None:
    settings, error = sshd_effective_config()
    if settings:
        print("Effective SSH config по данным sshd -T:")
        for key in (
            "pubkeyauthentication",
            "passwordauthentication",
            "kbdinteractiveauthentication",
            "challengeresponseauthentication",
            "permitrootlogin",
        ):
            value = settings.get(key, "unknown")
            print(f"{key}: {format_ssh_setting(key, value)}")
        print()
        if ssh_password_login_disabled(settings):
            print(green("Парольный вход SSH отключен."))
        else:
            print(red("Парольный вход SSH может быть разрешен."))
    else:
        print(red(f"Не удалось прочитать effective config SSH: {error}"))
    key_count = root_authorized_key_count()
    key_message = f"{AUTHORIZED_KEYS_PATH}: {key_count} ключ(ей)"
    print(green(key_message) if key_count > 0 else red(key_message))
    dropin_status = "есть" if SSHD_DROPIN_PATH.exists() else "нет"
    print(f"Managed drop-in: {SSHD_DROPIN_PATH} ({dropin_status})")
    if SSHD_LEGACY_DROPIN_PATH.exists():
        print(f"Legacy drop-in: {SSHD_LEGACY_DROPIN_PATH} (есть, будет убран при следующем применении)")


def run_security_audit(record_audit_run: RecordAuditCallback, format_manager_time: FormatTimeCallback) -> None:
    print("Проверка безопасности сервера.")
    print("Текущий набор проверок: SSH password login.")
    print()

    findings = []
    settings, error = sshd_effective_config()
    if not settings:
        print(red("FAIL SSH password login: не удалось получить effective config через sshd -T."))
        findings.append(
            {
                "title": "Не удалось проверить SSH password login",
                "details": error or "sshd -T не вернул настройки.",
                "recommendations": [
                    "Проверь, что установлен openssh-server и команда sshd доступна.",
                    "Выполни /usr/sbin/sshd -t и исправь ошибки конфигурации, если они есть.",
                    "Повтори проверку через меню Безопасность -> Проверить безопасность сервера.",
                ],
            }
        )
    else:
        password = settings.get("passwordauthentication", "unknown")
        keyboard = settings.get("kbdinteractiveauthentication", "unknown")
        challenge = settings.get("challengeresponseauthentication", "unknown")
        details = (
            f"PasswordAuthentication={password}, "
            f"KbdInteractiveAuthentication={keyboard}, "
            f"ChallengeResponseAuthentication={challenge}"
        )
        password_available = password == "yes" or keyboard == "yes" or challenge == "yes"
        if password_available:
            print(red(f"FAIL SSH password login: вход по паролю доступен. {details}"))
            findings.append(
                {
                    "title": "SSH password login доступен",
                    "details": details,
                    "recommendations": [
                        "Убедись, что вход по SSH-ключу работает в отдельной сессии.",
                        "Открой Безопасность -> Отключить вход по паролю SSH.",
                        "После применения снова запусти Проверить безопасность сервера и проверь, что PasswordAuthentication=no.",
                    ],
                }
            )
        else:
            print(green(f"OK   SSH password login: вход по паролю отключён. {details}"))

    print()
    if findings:
        print(red("Рекомендации по найденным проблемам:"))
        for index, finding in enumerate(findings, 1):
            print(f"{index}. {finding['title']}")
            print(f"   Детали: {finding['details']}")
            for recommendation in finding["recommendations"]:
                print(f"   - {recommendation}")
    else:
        print(green("Проблем безопасности из текущего набора проверок не найдено."))

    try:
        recorded_at = record_audit_run()
    except Exception as exc:
        print(red(f"WARN: не удалось записать время проверки безопасности: {exc}"))
    else:
        print()
        print(f"Последняя проверка безопасности записана: {format_manager_time(recorded_at)}")


def disable_ssh_password_login(confirm: ConfirmCallback) -> None:
    print("Будет отключён только вход по логину и паролю.")
    print("SSH-сервис и вход по SSH-ключам останутся включены.")
    print("Перед применением скрипт проверит root authorized_keys и валидность sshd config.")
    print()
    show_ssh_access()
    if root_authorized_key_count() < 1:
        print()
        print(red(f"Остановка: в {AUTHORIZED_KEYS_PATH} не найдено ни одного ключа."))
        print("Сначала добавь SSH-ключ, открой вторую сессию по ключу и только потом отключай парольный вход.")
        return
    if not SSHD_CONFIG_PATH.exists():
        print(red(f"Остановка: не найден {SSHD_CONFIG_PATH}."))
        return
    print()
    print("После применения текущая сессия обычно остаётся открытой, но новый вход по паролю будет запрещён.")
    if not confirm("Отключить парольный вход SSH сейчас"):
        print("Изменение отменено.")
        return

    original_config = SSHD_CONFIG_PATH.read_text(errors="ignore")
    original_dropin = SSHD_DROPIN_PATH.read_text(errors="ignore") if SSHD_DROPIN_PATH.exists() else None
    original_legacy_dropin = (
        SSHD_LEGACY_DROPIN_PATH.read_text(errors="ignore") if SSHD_LEGACY_DROPIN_PATH.exists() else None
    )
    config_backup = backup_existing_file(SSHD_CONFIG_PATH)
    dropin_backup = backup_existing_file(SSHD_DROPIN_PATH)
    legacy_backup = backup_existing_file(SSHD_LEGACY_DROPIN_PATH)
    if config_backup:
        print(f"Бэкап SSH config: {config_backup}")
    if dropin_backup:
        print(f"Бэкап managed drop-in: {dropin_backup}")
    if legacy_backup:
        print(f"Бэкап legacy drop-in: {legacy_backup}")

    try:
        write_sshd_password_dropin()
        include_added = ensure_sshd_dropin_include()
        if include_added:
            print(f"Добавлен ранний Include для {SSHD_DROPIN_PATH} в {SSHD_CONFIG_PATH}.")
        if remove_legacy_sshd_dropin():
            print(f"Удалён legacy drop-in: {SSHD_LEGACY_DROPIN_PATH}")
        valid, message = validate_sshd_config()
        if not valid:
            raise RuntimeError(f"sshd -t не прошёл проверку: {message}")
        settings, error = sshd_effective_config()
        if not ssh_password_login_disabled(settings):
            raise RuntimeError(f"sshd -T всё ещё показывает включённый парольный вход: {error or settings}")
        if not reload_sshd_service():
            raise RuntimeError("не удалось применить SSH config через systemctl")
    except Exception as exc:
        restore_text_file(SSHD_CONFIG_PATH, original_config)
        restore_text_file(SSHD_DROPIN_PATH, original_dropin)
        restore_text_file(SSHD_LEGACY_DROPIN_PATH, original_legacy_dropin)
        valid, _message = validate_sshd_config()
        if valid:
            reload_sshd_service()
        print(red(f"Изменения SSH отменены: {exc}"))
        return

    print(green("Готово: вход по паролю SSH отключён, вход по SSH-ключам оставлен."))
    print("Проверь новый вход из отдельного терминала: ssh root@SERVER_HOST")


def enable_ssh_password_login(confirm: ConfirmCallback) -> None:
    print("Будет убрана managed-настройка Xray VPS Manager, которая отключала вход по паролю.")
    print("После этого парольный вход включится, если он разрешён основным sshd_config или drop-in файлами сервера.")
    print("Вход по SSH-ключам не отключается.")
    print()
    show_ssh_access()
    if not SSHD_CONFIG_PATH.exists():
        print(red(f"Остановка: не найден {SSHD_CONFIG_PATH}."))
        return
    print()
    if not confirm("Включить парольный вход SSH сейчас"):
        print("Изменение отменено.")
        return

    original_config = SSHD_CONFIG_PATH.read_text(errors="ignore")
    original_dropin = SSHD_DROPIN_PATH.read_text(errors="ignore") if SSHD_DROPIN_PATH.exists() else None
    original_legacy_dropin = (
        SSHD_LEGACY_DROPIN_PATH.read_text(errors="ignore") if SSHD_LEGACY_DROPIN_PATH.exists() else None
    )
    config_backup = backup_existing_file(SSHD_CONFIG_PATH)
    dropin_backup = backup_existing_file(SSHD_DROPIN_PATH)
    legacy_backup = backup_existing_file(SSHD_LEGACY_DROPIN_PATH)
    if config_backup:
        print(f"Бэкап SSH config: {config_backup}")
    if dropin_backup:
        print(f"Бэкап managed drop-in: {dropin_backup}")
    if legacy_backup:
        print(f"Бэкап legacy drop-in: {legacy_backup}")

    try:
        if remove_sshd_dropin_include():
            print(f"Удалён ранний Include для {SSHD_DROPIN_PATH} из {SSHD_CONFIG_PATH}.")
        if SSHD_DROPIN_PATH.exists():
            SSHD_DROPIN_PATH.unlink()
            print(f"Удалён managed drop-in: {SSHD_DROPIN_PATH}")
        if remove_legacy_sshd_dropin():
            print(f"Удалён legacy drop-in: {SSHD_LEGACY_DROPIN_PATH}")

        valid, message = validate_sshd_config()
        if not valid:
            raise RuntimeError(f"sshd -t не прошёл проверку: {message}")
        settings, error = sshd_effective_config()
        if not ssh_password_login_enabled(settings):
            raise RuntimeError(f"sshd -T всё ещё показывает отключённый парольный вход: {error or settings}")
        if not reload_sshd_service():
            raise RuntimeError("не удалось применить SSH config через systemctl")
    except Exception as exc:
        restore_text_file(SSHD_CONFIG_PATH, original_config)
        restore_text_file(SSHD_DROPIN_PATH, original_dropin)
        restore_text_file(SSHD_LEGACY_DROPIN_PATH, original_legacy_dropin)
        valid, _message = validate_sshd_config()
        if valid:
            reload_sshd_service()
        print(red(f"Изменения SSH отменены: {exc}"))
        return

    print(green("Готово: вход по паролю SSH включён."))
    print("Проверь новый вход из отдельного терминала перед закрытием текущей SSH-сессии.")
