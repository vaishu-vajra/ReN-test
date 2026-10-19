import hashlib
import os
import re  # Make sure 're' is imported

# Use a safer max length (e.g., 240) to avoid hitting the 255-char limit
MAX_FILENAME_LENGTH = 230


def sanitize_filename(filename, max_length=MAX_FILENAME_LENGTH):
    """
    Sanitize and truncate the filename to be system-safe and within max_length.
    """

    # Ensure the filename is a string
    filename = str(filename)

    # 1. Replace any unsafe characters (slashes, colons, etc.)
    # This replaces / \ : * ? " < > | % and newlines/tabs with an underscore
    sanitized = re.sub(r'[\\/:*?"<>|%\r\n\t]+', '_', filename)

    # 2. Handle truncation if the sanitized name is too long
    if len(sanitized) <= max_length:
        return sanitized

    # If we need to truncate:
    # Get file extension *from the sanitized name*
    if "." in sanitized:
        name, ext = os.path.splitext(sanitized)
    else:
        name, ext = sanitized, ""

    # Generate a hash from the *original* (pre-sanitized) filename
    # This helps preserve uniqueness if sanitization causes collisions
    hash_object = hashlib.md5(filename.encode())
    hash_part = hash_object.hexdigest()[:8]  # Use first 8 characters of hash

    # Calculate max length for the name part
    # -1 for the underscore separator
    max_name_len = max_length - len(ext) - len(hash_part) - 1

    # Ensure max_name_len is not negative
    if max_name_len < 0:
        # If ext + hash is already too long, just use the hash and truncated ext
        name = hash_part
        max_ext_len = max_length - len(name)
        ext = ext[:max_ext_len]
        return f"{name}{ext}"

    # Truncate the name part
    name = name[:max_name_len]

    return f"{name}_{hash_part}{ext}"
