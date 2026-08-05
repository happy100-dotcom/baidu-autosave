"""
Path utilities for baidu-autosave.

All paths use POSIX semantics (posixpath), never os.path,
because Baidu Pan remote paths are always POSIX-style.
"""

import posixpath
import re
import unicodedata


def normalize_remote_path(path):
    """Normalize a Baidu Pan remote path.

    Always returns a POSIX-style absolute path starting with '/'.
    Removes duplicate slashes, trailing slashes, and decodes URL-encoded chars.

    Args:
        path: Raw path string.

    Returns:
        Normalized absolute path.
    """
    if not path:
        return '/'

    # Ensure string
    path = str(path)

    # Decode URL-encoded chars like %20 -> space
    try:
        from urllib.parse import unquote
        path = unquote(path)
    except Exception:
        pass

    # Normalize backslashes to forward slashes
    path = path.replace('\\', '/')

    # Collapse multiple slashes
    path = re.sub(r'/+', '/', path)

    # Strip trailing slash
    path = path.rstrip('/')

    # Ensure leading slash
    if not path.startswith('/'):
        path = '/' + path

    return path


def normalize_share_relative_path(path):
    """Normalize a share-relative path.

    Share-relative paths are relative to the share root.
    They must NOT:
    - Be absolute (start with '/')
    - Contain '..' segments
    - Contain backslashes
    - Contain empty segments
    - Contain NUL or control characters

    Args:
        path: Raw relative path string.

    Returns:
        Normalized relative path (no leading slash).

    Raises:
        ValueError: If the path is invalid.
    """
    if not path:
        raise ValueError('Path must not be empty')

    path = str(path)

    # Reject NUL and control characters
    for ch in path:
        if ord(ch) < 32 and ch not in ('\t',):
            raise ValueError(f'Path contains control character: {path!r}')

    # Normalize backslashes
    path = path.replace('\\', '/')

    # Reject absolute paths
    if path.startswith('/'):
        raise ValueError(f'Path must be relative, got absolute: {path!r}')

    # Split and validate segments
    segments = path.split('/')
    clean_segments = []
    for seg in segments:
        if not seg:
            continue  # skip empty segments from double slashes
        if seg == '..':
            raise ValueError(f'Path must not contain "..": {path!r}')
        if seg == '.':
            continue
        if seg in ('',):
            raise ValueError(f'Path contains empty segment: {path!r}')
        clean_segments.append(seg)

    if not clean_segments:
        raise ValueError(f'Path resolves to empty: {path!r}')

    return '/'.join(clean_segments)


def validate_save_name(name):
    """Validate a save_name (single folder name component).

    Rules:
    - Must be a single name, not a path (no '/', no '\\')
    - No NUL or control characters
    - Not '.', not '..'
    - Stripped length 1-255 bytes (UTF-8 encoded)
    - No leading/trailing whitespace after strip

    Args:
        name: The save_name string to validate.

    Returns:
        (valid: bool, cleaned: str or None, error: str or None)
    """
    if not name:
        return False, None, 'save_name must not be empty'

    name = str(name)

    # Strip whitespace
    stripped = name.strip()
    if not stripped:
        return False, None, 'save_name is only whitespace'
    if stripped != name:
        return False, stripped, 'save_name must not have leading/trailing whitespace'

    # Reject path separators
    if '/' in name:
        return False, None, 'save_name must not contain "/"'
    if '\\' in name:
        return False, None, 'save_name must not contain backslash'

    # Reject special directory names
    if name in ('.', '..'):
        return False, None, f'save_name must not be "{name}"'

    # Reject control characters
    for ch in name:
        if ord(ch) < 32:
            return False, None, 'save_name must not contain control characters'

    # Check UTF-8 byte length (Baidu Pan limit: 255 bytes)
    encoded = name.encode('utf-8')
    if len(encoded) > 255:
        return False, None, f'save_name too long ({len(encoded)} bytes, max 255)'

    return True, name, None


def safe_join(base, *parts):
    """Safely join path components using POSIX semantics.

    Like posixpath.join, but guarantees the result stays within *base*.
    If any part would escape base, raises ValueError.

    Args:
        base: Base directory path (absolute POSIX path).
        *parts: Additional path components to join.

    Returns:
        Joined absolute path.

    Raises:
        ValueError: If the result would escape *base*.
    """
    base = normalize_remote_path(base)
    result = posixpath.join(base, *parts)

    # Normalize the result
    result = normalize_remote_path(result)

    # Check that result starts with base (after normalization)
    if not result.startswith(base):
        raise ValueError(
            f'Path escape detected: {result!r} is not under {base!r}'
        )

    # Also check for directory traversal
    rel = posixpath.relpath(result, base)
    if rel.startswith('..'):
        raise ValueError(
            f'Path escape detected via relpath: {result!r} is not under {base!r}'
        )

    return result


def safe_join_save_dir(save_dir, save_name):
    """Compute the final directory from save_dir + save_name.

    Args:
        save_dir: Absolute base directory path.
        save_name: Single folder name component (or empty/None).

    Returns:
        Final directory path (absolute POSIX).

    Raises:
        ValueError: If save_dir is invalid or save_name causes escape.
    """
    save_dir = normalize_remote_path(save_dir)

    if not save_name:
        return save_dir

    # Validate save_name first
    valid, cleaned, error = validate_save_name(save_name)
    if not valid:
        raise ValueError(f'Invalid save_name: {error}')

    return safe_join(save_dir, cleaned)


def compute_final_dir(save_dir, save_name, flatten):
    """Compute final directory, with backward-compatible flatten hint.

    If save_name is set and flatten is True, the final dir is
    save_dir/save_name.  If save_name is empty, final_dir = save_dir.

    Args:
        save_dir: Base directory.
        save_name: Optional subfolder name.
        flatten: Whether flatten mode is active.

    Returns:
        (final_dir: str, effective_flatten: bool)
    """
    final_dir = safe_join_save_dir(save_dir, save_name)
    effective_flatten = flatten if save_name else False
    return final_dir, effective_flatten