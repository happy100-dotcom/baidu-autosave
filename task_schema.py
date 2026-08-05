"""
Task schema management for baidu-autosave.

Handles schema versioning, validation, and migration from v1 to v2.
"""

import uuid
import copy
import json
import os
import re
import time
from datetime import datetime
from loguru import logger

from path_utils import validate_save_name, normalize_remote_path, safe_join_save_dir

# Schema version
SCHEMA_VERSION = 2

# Valid status values
VALID_STATUSES = [
    'idle', 'running', 'success', 'skipped',
    'partial', 'failed', 'timed_out', 'cancelled',
]

# Valid schedule modes
VALID_SCHEDULE_MODES = ['inherit', 'custom', 'disabled']

# Valid selection modes
VALID_SELECTION_MODES = ['all', 'selected']

# Valid weekdays (ISO: 1=Monday ... 7=Sunday)
VALID_WEEKDAYS = list(range(1, 8))

# Valid weekday names (for display / APScheduler)
WEEKDAY_NAMES = {
    1: 'mon', 2: 'tue', 3: 'wed', 4: 'thu',
    5: 'fri', 6: 'sat', 7: 'sun',
}
WEEKDAY_REVERSE = {v: k for k, v in WEEKDAY_NAMES.items()}


def generate_task_uid():
    """Generate a new unique task UID (32 hex chars)."""
    return uuid.uuid4().hex


def generate_rule_uid():
    """Generate a new unique rule UID (16 hex chars)."""
    return uuid.uuid4().hex[:16]


def _to_iso_weekday(day):
    """Convert various weekday representations to ISO 1-7.

    Args:
        day: int (0=Sun, 1=Mon... or 1=Mon...7=Sun),
             str ('mon', 'tue', ...), or list.

    Returns:
        int 1-7 (1=Monday, 7=Sunday).
    """
    if isinstance(day, list):
        return [_to_iso_weekday(d) for d in day]
    if isinstance(day, str):
        return WEEKDAY_REVERSE.get(day.lower()[:3], 1)
    if isinstance(day, int):
        # Python weekday: 0=Mon, 6=Sun
        # ISO weekday: 1=Mon, 7=Sun
        # Cron weekday: 0=Sun, 1=Mon, 6=Sat
        if day == 0:
            return 7  # Sun in cron/python -> ISO 7
        return day  # 1-6 already match ISO 1-6
    return 1


def _iso_to_cron_weekday(iso_day):
    """Convert ISO weekday (1-7) to cron weekday (0-6, 0=Sun)."""
    if iso_day == 7:
        return 0
    return iso_day


def create_default_task(url, save_dir, pwd=None, name=None, **kwargs):
    """Create a new task dict with all required fields.

    Args:
        url: Share URL.
        save_dir: Target save directory (absolute path).
        pwd: Optional password.
        name: Optional display name.
        **kwargs: Additional fields (save_name, flatten, etc.)

    Returns:
        dict: Complete task dict with schema v2 fields.
    """
    task = {
        'task_uid': generate_task_uid(),
        'name': name or url,
        'url': url,
        'pwd': pwd or '',
        'save_dir': normalize_remote_path(save_dir),
        'save_name': kwargs.get('save_name', ''),
        'flatten': kwargs.get('flatten', True if kwargs.get('save_name') else False),
        'selection_mode': kwargs.get('selection_mode', 'all'),
        'selected_items': kwargs.get('selected_items', []),
        'schedule_mode': kwargs.get('schedule_mode', 'inherit'),
        'schedule_rules': kwargs.get('schedule_rules', []),
        'status': 'idle',
        'message': '',
        'last_run': None,
        'category': kwargs.get('category', ''),
        'regex_pattern': kwargs.get('regex_pattern', ''),
        'regex_replace': kwargs.get('regex_replace', ''),
        'order': kwargs.get('order', 0),
    }

    # Validate immediately
    errors = validate_task(task)
    if errors:
        raise ValueError(f'Task validation failed: {"; ".join(errors)}')

    return task


def validate_task(task):
    """Validate a task dict against schema v2 rules.

    Args:
        task: Task dict to validate.

    Returns:
        list of error strings (empty if valid).
    """
    errors = []

    if not task.get('task_uid'):
        errors.append('task_uid is required')

    if not task.get('url'):
        errors.append('url is required')

    if not task.get('save_dir'):
        errors.append('save_dir is required')
    else:
        save_dir = task['save_dir']
        if not save_dir.startswith('/'):
            errors.append('save_dir must be an absolute path starting with /')

    # Validate save_name if present
    save_name = task.get('save_name', '')
    if save_name:
        valid, cleaned, err_msg = validate_save_name(save_name)
        if not valid:
            errors.append(f'save_name: {err_msg}')

    # Validate selection_mode
    sel_mode = task.get('selection_mode', 'all')
    if sel_mode not in VALID_SELECTION_MODES:
        errors.append(f'selection_mode must be one of {VALID_SELECTION_MODES}')
    elif sel_mode == 'selected':
        selected_items = task.get('selected_items', [])
        if not selected_items:
            errors.append('selection_mode=selected requires at least one selected_items entry')
        for item in selected_items:
            if not isinstance(item, dict):
                errors.append('Each selected_items entry must be a dict')
                continue
            if 'path' not in item:
                errors.append('Each selected_items entry must have a "path"')
            if 'kind' not in item:
                errors.append('Each selected_items entry must have a "kind"')
            elif item['kind'] not in ('file', 'dir'):
                errors.append('kind must be "file" or "dir"')
    elif sel_mode == 'all':
        if task.get('selected_items'):
            errors.append('selection_mode=all requires selected_items to be empty')

    # Validate schedule_mode
    sched_mode = task.get('schedule_mode', 'inherit')
    if sched_mode not in VALID_SCHEDULE_MODES:
        errors.append(f'schedule_mode must be one of {VALID_SCHEDULE_MODES}')
    elif sched_mode == 'custom':
        rules = task.get('schedule_rules', [])
        if not rules:
            errors.append('schedule_mode=custom requires at least one schedule_rule')
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f'schedule_rules[{i}] must be a dict')
                continue
            weekdays = rule.get('weekdays', [])
            if not weekdays:
                errors.append(f'schedule_rules[{i}]: weekdays must not be empty')
            for wd in weekdays:
                if wd not in VALID_WEEKDAYS:
                    errors.append(f'schedule_rules[{i}]: invalid weekday {wd}')
            time_val = rule.get('time', '')
            if not re.match(r'^([01]\d|2[0-3]):([0-5]\d)$', time_val):
                errors.append(f'schedule_rules[{i}]: invalid time "{time_val}" (must be HH:MM)')
    elif sched_mode == 'disabled':
        if task.get('schedule_rules'):
            errors.append('schedule_mode=disabled requires schedule_rules to be empty')

    # Validate status
    status = task.get('status', '')
    if status and status not in VALID_STATUSES:
        errors.append(f'status must be one of {VALID_STATUSES}')

    return errors


def migrate_v1_to_v2(config):
    """Migrate a v1 config dict to v2.

    Migration is idempotent: running multiple times produces the same result.

    Args:
        config: The full config dict (from config.json).

    Returns:
        (migrated_config: dict, log: list of str)
    """
    log = []
    config = copy.deepcopy(config)

    # Check if already migrated
    if config.get('config_schema_version') == 2:
        log.append('Config already at schema v2, no migration needed')
        return config, log

    # Backup before migration
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f'config/config.json.backup.{timestamp}'
    try:
        with open('config/config.json', 'r') as f:
            backup_data = f.read()
        with open(backup_path, 'w') as f:
            f.write(backup_data)
        log.append(f'Backed up config to {backup_path}')
    except Exception as e:
        log.append(f'Warning: Could not create backup: {e}')

    tasks = config.get('baidu', {}).get('tasks', [])
    migrated_count = 0

    for i, task in enumerate(tasks):
        changes = []

        # 1. Ensure task_uid
        if not task.get('task_uid'):
            task['task_uid'] = generate_task_uid()
            changes.append('generated task_uid')

        # 2. Ensure save_dir is absolute
        save_dir = task.get('save_dir', '/')
        if not save_dir.startswith('/'):
            save_dir = '/' + save_dir
            task['save_dir'] = save_dir
            changes.append('fixed save_dir')

        # 3. Add save_name if missing
        if 'save_name' not in task:
            task['save_name'] = ''
            changes.append('added save_name=""')

        # 4. Add flatten if missing
        if 'flatten' not in task:
            task['flatten'] = False
            changes.append('added flatten=false')

        # 5. Migrate selection_mode
        old_selected = task.get('selected_files') or task.get('selected_items') or []
        if 'selection_mode' not in task:
            if old_selected:
                task['selection_mode'] = 'selected'
                # Convert old format (string list) to new format (dict list)
                if old_selected and isinstance(old_selected[0], str):
                    task['selected_items'] = [
                        {'path': p, 'kind': 'file'} for p in old_selected
                    ]
                else:
                    task['selected_items'] = old_selected
                changes.append('migrated selection_mode=selected')
            else:
                task['selection_mode'] = 'all'
                task['selected_items'] = []
                changes.append('migrated selection_mode=all')

        # Clean up old field names
        task.pop('selected_files', None)

        # 6. Migrate schedule_mode
        old_cron = task.get('cron')
        if 'schedule_mode' not in task:
            if old_cron and str(old_cron).strip():
                task['schedule_mode'] = 'custom'
                # Convert cron to schedule_rules
                rules = _parse_legacy_cron(str(old_cron))
                task['schedule_rules'] = rules
                task['raw_cron'] = str(old_cron)
                changes.append(f'migrated schedule_mode=custom ({len(rules)} rule(s))')
            else:
                task['schedule_mode'] = 'inherit'
                task['schedule_rules'] = []
                changes.append('migrated schedule_mode=inherit')

        # 7. Ensure status is valid
        old_status = task.get('status', '')
        if old_status and old_status not in VALID_STATUSES:
            task['status'] = 'idle'
            changes.append('reset invalid status to idle')

        # 8. Ensure order
        if 'order' not in task:
            task['order'] = i + 1
            changes.append(f'set order={i+1}')

        if changes:
            migrated_count += 1
            log.append(f'Task {i} ({task.get("name", "?")}): {", ".join(changes)}')

    # Set schema version
    config['config_schema_version'] = 2
    log.append(f'Set config_schema_version=2')

    # Validate migrated config
    for i, task in enumerate(tasks):
        task_errors = validate_task(task)
        if task_errors:
            log.append(f'WARNING: Task {i} has validation errors: {"; ".join(task_errors)}')

    log.append(f'Migration complete: {migrated_count} tasks migrated')
    return config, log


def _parse_legacy_cron(cron_str):
    """Parse a legacy cron string into schedule_rules.

    Handles simple cron expressions like "*/5 * * * *" or "30 20 * * 1,3,5".

    Args:
        cron_str: Legacy cron expression.

    Returns:
        list of schedule_rule dicts.
    """
    cron_str = cron_str.strip()
    parts = cron_str.split()
    if len(parts) != 5:
        return []

    # Try to extract weekday and time
    minute = parts[0]
    hour = parts[1]
    day_of_week = parts[4]

    # If minute/hour are specific values (not */n or *), extract time
    time_str = None
    try:
        m = int(minute)
        h = int(hour)
        time_str = f'{h:02d}:{m:02d}'
    except (ValueError, TypeError):
        pass

    # If day_of_week is specific, extract weekdays
    weekdays = []
    if day_of_week != '*':
        for part in day_of_week.split(','):
            try:
                iso_day = _to_iso_weekday(int(part))
                weekdays.append(iso_day)
            except (ValueError, TypeError):
                pass

    if not time_str or not weekdays:
        # Fallback: store as raw_cron and create a single rule at midnight
        return [{
            'rule_uid': generate_rule_uid(),
            'weekdays': list(range(1, 8)),  # every day
            'time': '00:00',
            'timezone': 'Asia/Shanghai',
        }]

    return [{
        'rule_uid': generate_rule_uid(),
        'weekdays': sorted(set(weekdays)),
        'time': time_str,
        'timezone': 'Asia/Shanghai',
    }]


def convert_rules_to_cron_triggers(rules, timezone='Asia/Shanghai'):
    """Convert schedule_rules to APScheduler cron expression strings.

    Each rule generates one cron expression. Rules are NOT merged.

    Args:
        rules: List of schedule_rule dicts.
        timezone: IANA timezone string.

    Returns:
        list of (cron_string, timezone) tuples.
    """
    import pytz
    from apscheduler.triggers.cron import CronTrigger

    results = []
    for rule in rules:
        weekdays = sorted(rule.get('weekdays', []))
        time_str = rule.get('time', '00:00')
        tz = rule.get('timezone', timezone)

        try:
            hour, minute = time_str.split(':')
            hour = int(hour)
            minute = int(minute)
        except (ValueError, AttributeError):
            continue

        # Convert ISO weekdays to cron format (0=Sun)
        cron_weekdays = sorted(set(_iso_to_cron_weekday(wd) for wd in weekdays))
        cron_weekday_str = ','.join(str(wd) for wd in cron_weekdays)

        cron_expr = f'{minute} {hour} * * {cron_weekday_str}'

        # Validate the expression
        try:
            CronTrigger.from_crontab(cron_expr, timezone=pytz.timezone(tz))
            results.append((cron_expr, tz))
        except Exception as e:
            logger.warning(f'Invalid cron expression "{cron_expr}": {e}')

    return results


def convert_cron_to_rules(cron_expr):
    """Convert a single cron expression to schedule_rules.

    This is lossy for complex cron expressions. Used for display only.

    Args:
        cron_expr: Cron expression string.

    Returns:
        list of schedule_rule dicts, or empty list if parsing fails.
    """
    return _parse_legacy_cron(cron_expr)