# Version info for NyoAnime
# THIS IS THE SINGLE SOURCE OF VERSION
# All other files (pyproject.toml, workflows, etc.) read from here

__version__ = "2.2.0"

APP_NAME = "NyoAnime"
APP_VERSION = f"v{__version__}"


def parse_version(ver_string: str) -> tuple:
    """Parse version string into comparable tuple."""
    import re
    ver_string = str(ver_string or "").strip().lower()
    if ver_string.startswith('v'):
        ver_string = ver_string[1:]
    
    parts = ver_string.split('.')
    result = []
    for p in parts:
        digits = re.match(r'(\d+)', p)
        if digits:
            result.append(int(digits.group(1)))
    
    while len(result) < 3:
        result.append(0)
    
    return tuple(result[:3])
