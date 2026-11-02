"""Проверка формы argv до root и обращения к состоянию сервера.

Значения (имя, порт, срок, зона) проверяют существующие обработчики. Здесь
только команды, арность и флаги; ручные парсеры рабочих действий сохраняются.
"""

from dataclasses import dataclass

from xray_vps_manager.core.errors import ManagerError


class UsageError(ManagerError):
    exit_code = 2


@dataclass(frozen=True)
class Form:
    minimum: int = 0
    maximum: int | None = 0
    switches: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    exclusive: tuple[tuple[str, ...], ...] = ()
    required: tuple[str, ...] = ()
    # Большинство старых позиционных адаптеров принимают флаги только в конце.
    interspersed: bool = False
    leading: int = 0

    def check(self, args):
        positional, options = [], {}
        iterator = iter(args)
        option_seen = False
        for arg in iterator:
            if arg in self.switches or arg in self.values:
                if len(positional) < self.leading:
                    raise UsageError('Required positional arguments must precede options.')
                if arg in options:
                    raise UsageError(f'Repeated option: {arg}')
                option_seen = True
                if arg in self.values:
                    value = next(iterator, None)
                    if value is None or value.startswith('--'):
                        raise UsageError(f'{arg} requires a value.')
                    options[arg] = value
                else:
                    options[arg] = True
            elif arg.startswith('--') or arg == '-h':
                raise UsageError(f'Unknown option: {arg}')
            else:
                if option_seen and not self.interspersed:
                    raise UsageError('Positional arguments must precede options.')
                positional.append(arg)
        if len(positional) < self.minimum or self.maximum is not None and len(positional) > self.maximum:
            raise UsageError('Invalid number of arguments.')
        for group in self.exclusive:
            if sum(option in options for option in group) > 1:
                raise UsageError('Use only one of: ' + ', '.join(group))
        for option in self.required:
            if option not in options:
                raise UsageError(f'{option} is required.')
        return positional, options


def table(rows):
    result = {}
    for names, form in rows:
        for name in names.split():
            result[name] = form
    return result


TLS = ('--tls-min-version', '--tls-max-version')
XHTTP = ('--grpc-service-name', '--xhttp-path', '--xhttp-mode', '--xhttp-extra-json')
CONNECTION = Form(1, 1, values=('--connection',), leading=1)
CLIENT = table([
    ('list connection-list trojan-password-check validate-trojan-passwords sync-routes limit-list timezone', Form()),
    ('add-connection', Form(3, 5, ('--install-caddy',), ('--security', '--transport', '--public-port', *XHTTP, *TLS), interspersed=True)),
    ('add-trojan-connection', Form(3, 6, ('--caddy', '--install-caddy', '--no-caddy', '--no-install-caddy'), ('--transport', '--ws-path', '--public-port', '--fingerprint', *TLS), interspersed=True)),
    ('update-trojan-connection trojan-connection-update', Form(1, 1, values=('--domain', '--tls-domain', '--local-port', '--public-port', '--ws-path', '--fingerprint', *TLS), leading=1)),
    ('connection-transport set-connection-transport', Form(2, 2, values=XHTTP, leading=2)),
    ('connection-xhttp-extra set-connection-xhttp-extra', Form(1, 1, ('--clear-xhttp-extra',), ('--xhttp-extra-json',), (('--clear-xhttp-extra', '--xhttp-extra-json'),), leading=1)),
    ('add', Form(1, 2, values=('--connection', '--protocol', '--payment'), exclusive=(('--connection', '--protocol'),), interspersed=True, leading=1)),
    ('disable off enable on link rotate-trojan-password trojan-password-rotate', CONNECTION),
    ('connection-rename rename-connection move-connection move-client move set-days access-days extend-days prolong-days set-payment payment-type', Form(2, 2)),
    ('remove-connection delete-connection remove delete del rm key clear-limit set-timezone', Form(1, 1)),
    ('route traffic-day traffic-week traffic-month', Form(1, 2)),
    ('traffic-summary', Form(0, 1)),
    ('traffic-period set-limit', Form(3, 3)),
    ('expire-due', Form(switches=('--quiet',))),
    ('enforce-limits', Form(switches=('--quiet', '--sync'))),
])
ACTIVITY = table([
    ('status enable disable sync geoip-status retention-overview raw-logs bypass-status block-sync', Form()),
    ('detail-mode alert-detection counters-today counters-week counters-growth errors-summary suspicious geoip-risks bypass-events retention alert-retention error-retention raw-log-rotate-time geo-list', Form(0, 1)),
    ('alerts errors', Form(0, 2)),
    ('counters errors-days', Form(0, 3)),
    ('error-detail exception-delete block-delete export-delete delete-export', Form(1, 1)),
    ('client exception-add raw-log-retention', Form(1, 2)),
    ('raw-log-archives exceptions blocklist block-stats export-list', Form(switches=('--plain',))),
    ('exception-candidates', Form(0, 1, ('--plain',), interspersed=True)),
    ('block-candidates', Form(1, 3, ('--plain',), interspersed=True)),
    ('rotate-raw-logs', Form(switches=('--due',))),
    ('block-add', Form(4, 5)),
    ('export', Form(3, 3, ('--path-only',))),
    ('download-command', Form(1, 3)),
    ('exception-delete-all export-delete-all delete-all-exports', Form(switches=('--yes',))),
    ('cleanup', Form(0, 1, ('--yes',), required=('--yes',))),
    ('raw-log-timer-sync', Form(switches=('--no-systemctl',))),
    ('detail-clients', Form(0, None)),
    ('risk-limits', Form(0, 5)),
    ('backfill', Form(3, 3, ('--dry-run', '--apply', '--yes'), exclusive=(('--dry-run', '--apply'),))),
])
TELEGRAM = table([
    ('status setup owner chat finish-setup enable disable test commands set-commands run-poller poll-daemon subscribers subscriptions', Form()),
    ('mode', Form(1, 1)),
    ('bot-name payment-amount payment-domain-rent', Form(0, 1)),
    ('notify-geoip poll-users notify-expiry', Form(switches=('--quiet',))),
    ('daily-summary', Form(switches=('--dry-run',))),
    ('notify-daily-summary', Form(switches=('--quiet', '--dry-run'), exclusive=(('--quiet', '--dry-run'),))),
    ('notify-access', Form(1, 1, ('--quiet',))),
    ('maintenance-notice', Form(0, 1, ('--dry-run', '--yes'), exclusive=(('--dry-run', '--yes'),))),
    ('payment-rounding', Form(0, 2)),
    ('payment-details', Form(0, None)),
])
BACKUP = table([
    ('create', Form(switches=('--path-only', '--no-sync'))),
    ('list', Form(switches=('--plain',))),
    ('restore delete remove rm', Form(1, 1)),
    ('download-command', Form(1, 3)),
    ('upload-command', Form(0, 2)),
])
CASCADE = table([
    ('list status --list --status --disable --direct disable direct test-select select-test', Form()),
    ('add set replace', Form(0, 1, values=('--country',), interspersed=True)),
    ('use switch select remove delete rm test --test', Form(0, 1)),
    ('country set-country', Form(2, None)),
])
BYPASS_OPTIONS = ('--region', '--test-target', '--target')
BYPASS = table([
    ('list status --list --status', Form()),
    ('add set replace', Form(0, 1, ('--replace',), BYPASS_OPTIONS, interspersed=True)),
    ('enable', Form(0, 1, ('--replace',), BYPASS_OPTIONS, interspersed=True)),
    ('region set-region', Form(0, 2, ('--replace',), BYPASS_OPTIONS, interspersed=True)),
    ('target set-target test-target', Form(0, 2, values=BYPASS_OPTIONS, interspersed=True)),
    ('disable remove delete rm', Form(0, 1)),
    ('test --test', Form(0, 1, values=BYPASS_OPTIONS, interspersed=True)),
])
WARP = table([
    ('status disable verify-disabled remove test', Form()),
    ('create', Form(switches=('--force', '--pin-hosts', '--no-pin-hosts'), exclusive=(('--pin-hosts', '--no-pin-hosts'),))),
    ('enable', Form(switches=('--pin-hosts', '--no-pin-hosts'), exclusive=(('--pin-hosts', '--no-pin-hosts'),))),
])
UPDATE = table([
    ('--check check --test-latest test-latest --update update --backups backups --list-backups --update-assets-loyalsoldier update-assets-loyalsoldier --update-geoip-v2fly update-geoip-v2fly', Form()),
    ('--update-assets update-assets --update-geo --rollback rollback', Form(0, 1)),
])


def validate(command, args):
    """Возвращает None либо поднимает UsageError; файловых операций нет."""
    args = list(args)
    if command == 'manager-update':
        # argparse остаётся владельцем преобразования Namespace; проверка формы
        # здесь также запрещает смешивать действия и параметры чужого действия.
        action = None
        options = set()
        index = 0
        while index < len(args):
            arg = args[index]
            # argparse принимает также --update=TAG и --rollback=NAME.
            option, separator, value = arg.partition('=')
            inline_value = bool(separator) and option in ('--update', '--rollback')
            if inline_value:
                arg = option
            if arg in ('--check', '--backups', '--update', '--rollback'):
                if action is not None:
                    raise UsageError('Choose only one action.')
                action = arg
                if not inline_value and arg in ('--update', '--rollback') and index + 1 < len(args) and not args[index + 1].startswith('-'):
                    index += 1
            elif arg in ('--force', '--no-test'):
                if arg in options:
                    raise UsageError(f'Repeated option: {arg}')
                options.add(arg)
            else:
                raise UsageError(f'Unknown argument: {arg}')
            index += 1
        if action is None or '--force' in options and action != '--update' or '--no-test' in options and action not in ('--update', '--rollback'):
            raise UsageError('Options do not match the selected action.')
        return
    if command in ('menu', 'traffic-sync', 'test'):
        if command == 'menu':
            Form().check(args)
        elif command == 'traffic-sync':
            Form(switches=('--quiet',)).check(args)
        elif args not in ([], ['--all'], ['all']):
            raise UsageError('Expected --all or no arguments.')
        return
    if command == 'activity':
        if args.count('--quiet') > 1:
            raise UsageError('Repeated option: --quiet')
        args = [arg for arg in args if arg != '--quiet']
    defaults = {'activity': 'status', 'telegram': 'status', 'warp': 'status', 'cascade': 'add', 'bypass': 'add'}
    action, rest = (args[0], args[1:]) if args else (defaults[command], [])
    forms = {'client': CLIENT, 'activity': ACTIVITY, 'telegram': TELEGRAM,
             'backup': BACKUP, 'cascade': CASCADE, 'bypass': BYPASS, 'warp': WARP, 'update': UPDATE,
             'sqlite': {'status': Form()}, 'units': {'sync': Form(switches=('--check', '--no-systemctl'))},
             'caddy': {'random-tls-run': Form(switches=('--quiet',), values=('--domain',), interspersed=True)}}
    form = forms[command].get(action)
    if form is None:
        if command in ('cascade', 'bypass') and (
                not action.startswith('-')
                or action in forms[command]['add'].values + forms[command]['add'].switches):
            form, rest = forms[command]['add'], args
        else:
            raise UsageError(f'Unknown command: {action}')
    values, options = form.check(rest)
    if command == 'activity':
        valid = True
        if action == 'backfill':
            valid = set(options) in ({'--dry-run'}, {'--dry-run', '--yes'}, {'--apply', '--yes'})
        elif action == 'risk-limits':
            valid = not values or len(values) == 5 and values[0] == 'set'
        elif action == 'raw-log-retention':
            valid = values[0] in ('access', 'error')
        elif action == 'detail-clients':
            valid = not values or values in (['list'], ['clear']) or values[0] == 'set'
        if not valid:
            raise UsageError(f'Invalid arguments for {action}.')
    if command == 'telegram':
        if action == 'payment-rounding' and values and len(values) != (2 if values[0] == 'step' else 1):
            raise UsageError('Expected payment-rounding [none|step VALUE].')
        if action == 'payment-details' and values:
            valid = (values[0] in ('none', 'clear', 'off') and len(values) == 1
                     or values[0] == 'phone' and len(values) >= 3
                     or values[0] in ('card', 'bank-account') and len(values) >= 2)
            if not valid:
                raise UsageError('Invalid payment-details arguments.')
        if action == 'maintenance-notice' and options and (not values or values[0] in ('list', 'templates')):
            raise UsageError('Choose a maintenance notice before its options.')
    if command == 'client':
        if action in ('connection-xhttp-extra', 'set-connection-xhttp-extra') and not options:
            raise UsageError('Expected --xhttp-extra-json or --clear-xhttp-extra.')
        if action in ('update-trojan-connection', 'trojan-connection-update') and not options:
            raise UsageError('Specify at least one connection option.')
        if action == 'add-trojan-connection':
            transport = (options.get('--transport') or '').strip().lower()
            ws = {'--caddy', '--install-caddy', '--no-caddy', '--no-install-caddy', '--ws-path', '--public-port', *TLS}
            tcp = transport == 'tcp' or not transport and not (set(options) & ws) and len(values) in (5, 6)
            if len(values) not in ((5, 6) if tcp else (3, 4)):
                raise UsageError('Invalid number of Trojan connection arguments.')
