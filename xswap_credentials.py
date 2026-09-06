"""Read local credentials through one verified file descriptor."""
import json
import os
import stat


class CredentialError(Exception):
    pass


def read_auth(home):
    # Reject final-component symlinks and non-regular files, including FIFOs
    # without blocking. Never repair a user's credential file implicitly.
    fd = None
    try:
        fd = os.open(home / 'auth.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise CredentialError('Unsafe auth.json: require a user-owned regular file with mode 0600 (0400 is also accepted); remove links and review permissions before retrying.')
        with os.fdopen(fd, 'r') as stream:
            fd = None
            value = json.load(stream)
        if not isinstance(value, dict):
            raise CredentialError('Invalid auth.json object; sign in again.')
        return value
    except (OSError, ValueError):
        raise CredentialError('Cannot safely read auth.json; require a user-owned regular file, no symlink, and mode 0600.') from None
    finally:
        if fd is not None:
            os.close(fd)
