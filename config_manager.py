"""
Configuration manager for baidu-autosave.

Provides atomic config writes, config.json / config directory separation,
migration from v1 to v2, schema enforcement, and a clean API for
accessing/modifying config data.

Key design decisions:
- Config is stored in config/config.json (JSON file).
- write is atomic: write to temp file (mkstemp), flush+fsync, then rename.
- On init, if schema v1 is detected, runs migration.
- All public methods that modify state use a reentrant lock (RLock).
- Running state is NOT stored here — see RunStore.
"""

import copy
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
import pytz

from task_schema import (
    migrate_v1_to_v2,
    validate_task,
    create_default_task,
    generate_task_uid,
    SCHEMA_VERSION,
    VALID_SCHEDULE_MODES,
    VALID_SELECTION_MODES,
    convert_rules_to_cron_triggers,
)

# ── paths ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG_DIR = 'config'
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, 'config.json')

# Default global settings
DEFAULT_GLOBAL_SETTINGS = {
    'schedule_mode': 'inherit',
    'schedule_rules': [],
    'timezone': 'Asia/Shanghai',
    'global_schedule_cron': '0 6 * * *',  # 6:00 AM daily
    'max_retries': 3,
    'retry_delay': 30,
    'transfer_mode': 'realtime',           # realtime or scheduled
    'download_limit': 5,                   # max concurrent downloads
    'log_level': 'INFO',
    'auto_cleanup_days': 30,               # days to keep logs
    'notification': {
        'enabled': False,
        'type': 'none',                    # none, email, push
        'config': {},
    },
}

# ── helpers ────────────────────────────────────────────────────────────────

def _ensure_dir_exists(path):
    """Ensure parent directory exists."""
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _atomic_write(filepath, data):
    """Atomically write data to filepath.

    Uses tempfile.mkstemp for unique temp file, flush+fsync on the fd,
    chmod 0600, os.replace for atomic swap, and fsync on the parent
    directory for durable metadata.

    Args:
        filepath: Target file path.
        data: String data to write.

    Raises:
        IOError: If write fails; original file is preserved.
    """
    _ensure_dir_exists(filepath)
    dir_path = os.path.dirname(filepath) or '.'
    fd, tmp_path = tempfile.mkstemp(
        suffix='.tmp',
        prefix='config_',
        dir=dir_path,
    )
    try:
        if isinstance(data, str):
            data_bytes = data.encode('utf-8')
        else:
            data_bytes = data

        os.write(fd, data_bytes)
        os.flush(fd)     # flush Python-level buffer
        os.fsync(fd)     # fsync to disk
        os.close(fd)
        fd = None        # prevent double-close

        # Restrict permissions before rename
        os.chmod(tmp_path, 0o600)

        # Atomic swap
        os.replace(tmp_path, filepath)

        # fsync parent directory so the rename is durable
        dir_fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    except Exception:
        # Clean up temp file on failure; original file is untouched
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path, default=None):
    """Read and parse a JSON file.

    Args:
        path: File path.
        default: Default value if file doesn't exist or is invalid.

    Returns:
        Parsed dict, or default if not found.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.warning(f'Failed to read {path}: {e}')
        return default


def _serialize_json(data, indent=2):
    """Serialize data to JSON string with consistent formatting.

    Args:
        data: Data to serialize.
        indent: Indentation level.

    Returns:
        JSON string.
    """
    return json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=True) + '\n'


# ── ConfigManager class ────────────────────────────────────────────────────

class ConfigManager:
    """Manages configuration loading, saving, migration, and access.

    Thread-safe: all public methods that modify state use a reentrant lock
    (RLock) so that internal helper methods can safely call each other.
    """

    _instance = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path=None):
        """Initialize ConfigManager.

        Args:
            config_path: Path to config.json.  Defaults to DEFAULT_CONFIG_PATH.
        """
        if hasattr(self, '_initialized') and self._initialized:
            return

        self._config_path = config_path or DEFAULT_CONFIG_PATH
        self._config_dir = os.path.dirname(self._config_path)
        self._config = {}
        self._lock = threading.RLock()  # RLock prevents deadlock on nested calls
        self._loaded = False
        self._initialized = False

        # Load config on init
        self._load_config()

        # Run migration if needed
        self._maybe_migrate()

        self._initialized = True

    # ── loading / saving ────────────────────────────────────────────────

    def _load_config(self):
        """Load config from disk."""
        config = _read_json(self._config_path, default=None)

        if config is None:
            config = self._create_default_config()
            self._config = config
            self._save_config_internal()
            self._loaded = True
            logger.info('Created default config')
        else:
            self._config = config
            self._loaded = True
            logger.info(f'Loaded config from {self._config_path}')

    def _create_default_config(self):
        """Create a default config structure."""
        return {
            'config_schema_version': SCHEMA_VERSION,
            'global_settings': copy.deepcopy(DEFAULT_GLOBAL_SETTINGS),
            'baidu': {
                'current_user': '',
                'users': {},
                'tasks': [],
            },
        }

    def _save_config_internal(self, data=None):
        """Internal save (caller must hold _lock)."""
        data = data or self._config
        try:
            json_str = _serialize_json(data)
            _atomic_write(self._config_path, json_str)
        except Exception as e:
            logger.error(f'Failed to save config: {e}')
            raise

    def save_config(self):
        """Public save with lock and backup."""
        with self._lock:
            self._create_backup()
            self._save_config_internal()

    def _create_backup(self, keep_max=5):
        """Create a timestamped backup of the current config file.

        Args:
            keep_max: Maximum number of backups to keep.
        """
        if not os.path.exists(self._config_path):
            return

        backup_dir = os.path.join(self._config_dir, 'backups')
        os.makedirs(backup_dir, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(
            backup_dir,
            f'config.json.backup.{timestamp}',
        )

        try:
            shutil.copy2(self._config_path, backup_path)
            os.chmod(backup_path, 0o600)
            logger.debug(f'Created config backup: {backup_path}')
        except Exception as e:
            logger.warning(f'Failed to create config backup: {e}')

        # Clean up old backups
        try:
            backups = sorted([
                f for f in os.listdir(backup_dir)
                if f.startswith('config.json.backup.')
            ])
            while len(backups) > keep_max:
                old = os.path.join(backup_dir, backups.pop(0))
                os.remove(old)
                logger.debug(f'Removed old backup: {old}')
        except Exception as e:
            logger.warning(f'Failed to clean up backups: {e}')

    def _maybe_migrate(self):
        """Check schema version and migrate if needed."""
        version = self._config.get('config_schema_version', 1)

        if version == 2:
            logger.debug('Config already at schema v2')
            return

        if version < 2:
            logger.info(f'Migrating config from schema v{version} to v2...')
            with self._lock:
                migrated, log = migrate_v1_to_v2(self._config)
                self._config = migrated
                self._save_config_internal()
                for line in log:
                    logger.info(f'Migration: {line}')
            logger.success('Config migration complete')

    # ── public accessors ────────────────────────────────────────────────

    @property
    def config_path(self):
        return self._config_path

    @property
    def config(self):
        """Get the full config dict (read-only copy)."""
        with self._lock:
            return copy.deepcopy(self._config)

    @property
    def is_loaded(self):
        return self._loaded

    @property
    def schema_version(self):
        return self._config.get('config_schema_version', 1)

    @property
    def global_settings(self):
        with self._lock:
            return copy.deepcopy(
                self._config.get('global_settings', DEFAULT_GLOBAL_SETTINGS)
            )

    # ── task CRUD ───────────────────────────────────────────────────────

    def list_tasks(self):
        """Get all tasks with their final state."""
        with self._lock:
            tasks = self._config.get('baidu', {}).get('tasks', [])
            return copy.deepcopy(tasks)

    def get_task(self, task_uid):
        """Get a single task by UID.

        Args:
            task_uid: Task UID string.

        Returns:
            Task dict, or None.
        """
        with self._lock:
            for task in self._config.get('baidu', {}).get('tasks', []):
                if task.get('task_uid') == task_uid:
                    return copy.deepcopy(task)
            return None

    def get_task_by_order(self, order):
        """Get a single task by order field (legacy compat)."""
        with self._lock:
            for task in self._config.get('baidu', {}).get('tasks', []):
                if task.get('order') == order:
                    return copy.deepcopy(task)
            return None

    def get_task_by_url(self, url):
        """Get a task by share URL (legacy compat)."""
        with self._lock:
            url = url.split('#')[0].strip()
            for task in self._config.get('baidu', {}).get('tasks', []):
                if task.get('url', '').split('#')[0].strip() == url:
                    return copy.deepcopy(task)
            return None

    def add_task(self, url, save_dir, pwd=None, name=None, **kwargs):
        """Add a new task.

        Args:
            url: Share URL.
            save_dir: Target save directory.
            pwd: Optional password.
            name: Optional display name.
            **kwargs: Additional task fields (save_name, flatten, etc.)

        Returns:
            (task: dict, error: str or None)
        """
        try:
            task = create_default_task(url, save_dir, pwd, name, **kwargs)
        except ValueError as e:
            return None, str(e)

        with self._lock:
            tasks = self._config.setdefault('baidu', {}).setdefault('tasks', [])

            # Check for duplicate URL
            clean_url = url.split('#')[0].strip()
            for t in tasks:
                if t.get('url', '').split('#')[0].strip() == clean_url:
                    return None, f'Task with URL already exists: {url}'

            task['order'] = len(tasks) + 1
            tasks.append(task)
            self._save_config_internal()

        logger.success(f'Added task: {task.get("name", url)}')
        return copy.deepcopy(task), None

    def update_task(self, task_uid, updates):
        """Update an existing task with validation.

        Uses candidate+validate+commit pattern:
        1. Deep-copy the existing task.
        2. Apply only allowed fields to the candidate.
        3. Validate the candidate.
        4. If valid, replace the original; otherwise return 422.

        Args:
            task_uid: Task UID to update.
            updates: Dict of fields to update.

        Returns:
            (task: dict, error: str or None)
        """
        with self._lock:
            tasks = self._config.get('baidu', {}).get('tasks', [])
            for i, task in enumerate(tasks):
                if task.get('task_uid') == task_uid:
                    # Build candidate from deep copy
                    candidate = copy.deepcopy(task)

                    # Apply allowed updates to candidate
                    for key in ('name', 'url', 'pwd', 'save_dir', 'save_name',
                                'flatten', 'save_mode', 'selection_mode',
                                'selected_items', 'schedule_mode', 'schedule_rules',
                                'category', 'regex_pattern', 'regex_replace',
                                'cron', 'message'):
                        if key in updates:
                            candidate[key] = copy.deepcopy(updates[key])

                    # Validate candidate
                    errors = validate_task(candidate)
                    if errors:
                        return None, f'Validation failed: {"; ".join(errors)}'

                    # Replace original with validated candidate
                    tasks[i] = candidate
                    self._save_config_internal()
                    logger.success(f'Updated task: {task_uid}')
                    return copy.deepcopy(candidate), None

            return None, f'Task not found: {task_uid}'

    def _find_task_uid_by_order(self, order):
        """Internal: find task_uid by order (no lock required)."""
        for task in self._config.get('baidu', {}).get('tasks', []):
            if task.get('order') == order:
                return task.get('task_uid')
        return None

    def remove_task(self, task_uid):
        """Remove a task by UID.

        Args:
            task_uid: Task UID to remove.

        Returns:
            bool: True if removed.
        """
        with self._lock:
            tasks = self._config.get('baidu', {}).get('tasks', [])
            for i, task in enumerate(tasks):
                if task.get('task_uid') == task_uid:
                    tasks.pop(i)
                    for j, t in enumerate(tasks):
                        t['order'] = j + 1
                    self._save_config_internal()
                    logger.success(f'Removed task: {task_uid}')
                    return True
            return False

    def remove_task_by_order(self, order):
        """Remove a task by order (legacy compat)."""
        with self._lock:
            tasks = self._config.get('baidu', {}).get('tasks', [])
            for i, task in enumerate(tasks):
                if task.get('order') == order:
                    tasks.pop(i)
                    for j, t in enumerate(tasks):
                        t['order'] = j + 1
                    self._save_config_internal()
                    logger.success(f'Removed task by order: {order}')
                    return True
            return False

    def remove_tasks(self, orders):
        """Remove multiple tasks by order list.

        Args:
            orders: List of order numbers.

        Returns:
            int: Number of tasks removed.
        """
        removed = 0
        with self._lock:
            tasks = self._config.get('baidu', {}).get('tasks', [])
            for order in sorted(orders, reverse=True):
                for i, task in enumerate(tasks):
                    if task.get('order') == order:
                        tasks.pop(i)
                        removed += 1
                        break
            for j, t in enumerate(tasks):
                t['order'] = j + 1
            if removed:
                self._save_config_internal()
            return removed

    # ── user management ─────────────────────────────────────────────────

    def get_current_user(self):
        with self._lock:
            return self._config.get('baidu', {}).get('current_user', '')

    def set_current_user(self, username):
        with self._lock:
            baidu = self._config.setdefault('baidu', {})
            if username and username not in baidu.get('users', {}):
                return False, f'User not found: {username}'
            baidu['current_user'] = username
            self._save_config_internal()
            return True, None

    def get_users(self):
        with self._lock:
            return copy.deepcopy(
                self._config.get('baidu', {}).get('users', {})
            )

    def get_user(self, username):
        with self._lock:
            return copy.deepcopy(
                self._config.get('baidu', {}).get('users', {}).get(username)
            )

    def add_user(self, username, cookies, name=None):
        with self._lock:
            users = self._config.setdefault('baidu', {}).setdefault('users', {})
            if username in users:
                return None, f'User already exists: {username}'

            users[username] = {
                'cookies': cookies,
                'name': name or username,
                'user_id': username,
                'created_at': int(time.time()),
            }
            self._save_config_internal()
            return copy.deepcopy(users[username]), None

    def update_user(self, username, cookies=None, name=None):
        with self._lock:
            users = self._config.get('baidu', {}).get('users', {})
            if username not in users:
                return False
            if cookies is not None:
                users[username]['cookies'] = cookies
            if name is not None:
                users[username]['name'] = name
            self._save_config_internal()
            return True

    def remove_user(self, username):
        with self._lock:
            users = self._config.get('baidu', {}).get('users', {})
            if username not in users:
                return False
            del users[username]
            if self._config.get('baidu', {}).get('current_user') == username:
                self._config['baidu']['current_user'] = ''
            self._save_config_internal()
            return True

    # ── global settings ─────────────────────────────────────────────────

    def get_global_settings(self):
        with self._lock:
            return copy.deepcopy(
                self._config.get('global_settings', DEFAULT_GLOBAL_SETTINGS)
            )

    def update_global_settings(self, updates):
        with self._lock:
            settings = self._config.setdefault(
                'global_settings',
                copy.deepcopy(DEFAULT_GLOBAL_SETTINGS),
            )
            settings.update(updates)
            self._save_config_internal()
            return True

    # ── categories ──────────────────────────────────────────────────────

    def get_categories(self):
        with self._lock:
            tasks = self._config.get('baidu', {}).get('tasks', [])
            cats = {t.get('category', '') for t in tasks if t.get('category')}
            return sorted(c for c in cats if c)

    def get_tasks_by_category(self, category=None):
        with self._lock:
            tasks = self._config.get('baidu', {}).get('tasks', [])
            if category is None:
                return [t for t in tasks if not t.get('category')]
            return [t for t in tasks if t.get('category') == category]

    # ── validation ──────────────────────────────────────────────────────

    def validate_config(self):
        """Validate the entire config.

        Returns:
            list of (path, error) tuples.
        """
        errors = []

        version = self._config.get('config_schema_version', 1)
        if version != 2:
            errors.append(('config_schema_version', f'Expected 2, got {version}'))

        baidu = self._config.get('baidu', {})
        if not baidu:
            errors.append(('baidu', 'Missing baidu section'))

        tasks = baidu.get('tasks', [])
        for i, task in enumerate(tasks):
            task_errors = validate_task(task)
            for err in task_errors:
                errors.append((f'tasks[{i}]', err))

        return errors