"""
Path utilities for baidu-autosave.

All paths use POSIX semantics (posixpath), never os.path,
because Baidu Pan remote paths are always POSIX-style.

Security: input validation REJECTS ambiguous input rather than
silently rewriting it.  Rejecting is safer than guessing.
"""

import posixpath
import re


def normalize_remote_path(path):
    """Normalize a Baidu Pan remote path.

    Always returns a POSIX-style absolute path starting with '/'.
    Removes duplicate slashes, trailing slashes, and decodes URL-encoded chars.
    Rejects '.' and '..' segments (they are not valid remote paths).

    Args:
        path: Raw path string.

    Returns:
        Normalized absolute path.

    Raises:
        ValueError: If path contains '.' or '..' segments.
    """
    if not path:
        return '/'

    path = str(path)

    # Decode URL-encoded chars
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

    # Reject '.' and '..' segments
    for segment in path.split('/'):
        if segment in ('.', '..'):
            raise ValueError(
                f'Path must not contain "{segment}" segment: {path!r}'
            )

    # Ensure leading slash
    if not path.startswith('/'):
        path = '/' + path

    return path


def normalize_share_relative_path(path):
    """Normalize and strictly validate a share-relative path.

    Share-relative paths are relative to the share root.
    They MUST:
    - Be relative (NOT start with '/')
    - NOT contain '..' segments
    - NOT contain backslashes (rejected, not converted)
    - NOT contain empty segments
    - NOT contain NUL or any control characters (including tab)
    - NOT contain '.' as a standalone segment

    Args:
        path: Raw relative path string.

    Returns:
        Normalized relative path (no leading slash, single slashes).

    Raises:
        ValueError: If the path is invalid.
    """
    if not path:
        raise ValueError('Path must not be empty')

    path = str(path)

    # Reject NUL and ALL control characters (including tab)
    for ch in path:
        if ord(ch) < 32:
            raise ValueError(
                f'Path must not contain control characters (ord={ord(ch)}): {path!r}'
            )

    # Reject backslashes outright (security: don't silently convert)
    if '\\' in path:
        raise ValueError(f'Path must not contain backslashes: {path!r}')

    # Reject absolute paths
    if path.startswith('/'):
        raise ValueError(f'Path must be relative, got absolute: {path!r}')

    # Split and validate each segment
    segments = path.split('/')
    clean_segments = []
    for seg in segments:
        if seg == '':
            raise ValueError(f'Path must not contain empty segments: {path!r}')
        if seg == '..':
            raise ValueError(f'Path must not contain "..": {path!r}')
        if seg == '.':
            raise ValueError(f'Path must not contain "." as a segment: {path!r}')
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

    # Reject control characters (including tab)
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

    Uses posixpath.commonpath to verify the result is within *base*.

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
    result = normalize_remote_path(result)

    # Use commonpath for boundary check (handles /base vs /base2 correctly)
    common = posixpath.commonpath([base, result])
    if common != base:
        raise ValueError(
            f'Path escape detected: {result!r} is not under {base!r} '
            f'(common base: {common!r})'
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

    valid, cleaned, error = validate_save_name(save_name)
    if not valid:
        raise ValueError(f'Invalid save_name: {error}')

    return safe_join(save_dir, cleaned)


def compute_final_dir(save_dir, save_name, flatten):
    """Compute final directory.

    save_dir + save_name = final_dir.
    flatten is a separate flag indicating whether to flatten the
    source directory structure into the final_dir.

    Args:
        save_dir: Base directory.
        save_name: Optional subfolder name (may be empty).
        flatten: Whether flatten mode is active (independent of save_name).

    Returns:
        (final_dir: str, effective_flatten: bool)
    """
    final_dir = safe_join_save_dir(save_dir, save_name)
    return final_dir, flatten