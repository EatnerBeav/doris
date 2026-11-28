#!/usr/bin/env python3
"""Secret scan for the Doris repo. Standard library only, Python 3.9+.

A committed secret disqualifies the entry, so this runs on every commit (the
pre-commit hook in .githooks/) and on every push (CI). It blocks when a file
holds any of:

  1. a known key shape (Anthropic, OpenAI, GitHub, AWS, Slack, Google, private keys,
     or a long literal assigned to something named like a key, token or password);
  2. a value from a .env file: the real one lives only on the server, never in git. Settings
     are left out: a key named like a host, port, user, address, email or zone, and any value
     that is a plain email address, whatever its key (addresses are in receipts by design);
  3. a term from a private terms file kept outside the repo (DORIS_SCAN_TERMS);
  4. a file that should never be committed at all (.env, private key files).

It never prints a matched value, only the file, the line and the rule.

Usage:
  python3 src/secret_scan.py --staged         files staged for commit (the pre-commit hook)
  python3 src/secret_scan.py --all            every tracked file and every untracked, not-ignored file
  python3 src/secret_scan.py PATH [PATH ...]  these files or folders

Environment:
  DORIS_ENV_FILE    .env files whose values must never appear, separated by os.pathsep.
                    <repo>/.env and /root/doris-secrets/.env are always added when present.
  DORIS_SCAN_TERMS  a private file of extra terms, one per line, matched case-insensitively.
                    Defaults to ~/.doris-scan-terms when present. It never enters the repo.
"""

import os
import re
import subprocess
import sys

MAX_BYTES = 5 * 1024 * 1024
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_FILES = (os.path.join(REPO_ROOT, ".env"), "/root/doris-secrets/.env")

# Known key shapes. Order matters only for which rule name is reported first.
PATTERNS = (
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("private_key_block", re.compile("-----BEGIN " + r"[A-Z ]*" + "PRIVATE KEY-----")),
)

# A long literal assigned to something named like a secret: password = "a1b2...".
ASSIGNMENT = re.compile(
    r"(?i)\b[A-Za-z_]*(?:api[_-]?key|secret|passw(?:or)?d|token)[A-Za-z_]*\b"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9/+_\-.]{16,})"
)
PLACEHOLDER_WORDS = ("replace", "example", "placeholder", "changeme", "your", "xxxx", "dummy", "redacted")

# .env keys whose values are settings, not secrets (addresses and hosts appear in real traces).
NON_SECRET_KEY = re.compile(r"(?:_HOST|_PORT|_USER|_ADDRESS|_EMAIL|_TZ)$|^DORIS_MODEL$|^DORIS_MODE$")
# A value that is a plain email address and nothing else is a setting whatever its key: smoke run 2's
# trace was blocked because a stray key held the owner's address and wolf's address holds it.
PLAIN_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
MIN_ENV_VALUE_LEN = 6

FORBIDDEN_NAME = re.compile(r"^(?:\.env(?:\..+)?|id_rsa|id_ed25519|.+\.pem|.+\.p12|.+\.pfx|.+\.key)$", re.I)
ALLOWED_NAMES = {".env.example"}


def _git(args):
    return subprocess.run(["git", "-C", REPO_ROOT] + args, capture_output=True, check=True).stdout


def read_env_values(paths):
    """Return the secret values from the given .env files (never printed), settings left out."""
    values = []
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if NON_SECRET_KEY.search(key) or len(value) < MIN_ENV_VALUE_LEN or PLAIN_ADDRESS.fullmatch(value):
                    continue
                values.append(value)
    return values


def read_terms(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return [t.strip().lower() for t in fh if t.strip() and not t.startswith("#")]


def _is_placeholder(value):
    low = value.lower()
    return any(w in low for w in PLACEHOLDER_WORDS)


def scan_text(text, env_values, terms):
    """Yield (line_number, rule) for every finding in one file's text."""
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        found = None
        for rule, pattern in PATTERNS:
            if pattern.search(line):
                found = rule
                break
        if not found:
            for match in ASSIGNMENT.finditer(line):
                value = match.group(1)
                has_letter = any(c.isalpha() for c in value)
                has_digit = any(c.isdigit() for c in value)
                if has_letter and has_digit and not _is_placeholder(value):
                    found = "secret_assignment"
                    break
        if not found:
            for value in env_values:
                if value in line:
                    found = "env_value"
                    break
        if not found and terms:
            low = line.lower()
            for index, term in enumerate(terms, 1):
                if term in low:
                    found = "private_term_%d" % index
                    break
        if found:
            yield number, found


def scan_blob(path, data, env_values, terms):
    findings = []
    name = os.path.basename(path)
    if name not in ALLOWED_NAMES and FORBIDDEN_NAME.match(name):
        findings.append((path, 0, "forbidden_file"))
    if len(data) > MAX_BYTES:
        findings.append((path, 0, "too_large_to_scan"))
        return findings
    text = data.decode("utf-8", errors="replace")
    for number, rule in scan_text(text, env_values, terms):
        findings.append((path, number, rule))
    return findings


def staged_files():
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
    for path in [p for p in out.decode("utf-8", "replace").split("\0") if p]:
        yield path, _git(["show", ":" + path])


def all_files():
    out = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    for path in sorted(set(p for p in out.decode("utf-8", "replace").split("\0") if p)):
        full = os.path.join(REPO_ROOT, path)
        if os.path.isfile(full):
            with open(full, "rb") as fh:
                yield path, fh.read()


def given_files(paths):
    for root in paths:
        if os.path.isfile(root):
            with open(root, "rb") as fh:
                yield root, fh.read()
            continue
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in sorted(files):
                full = os.path.join(folder, f)
                with open(full, "rb") as fh:
                    yield full, fh.read()


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    env_paths = list(DEFAULT_ENV_FILES)
    if os.environ.get("DORIS_ENV_FILE"):
        env_paths.extend(os.environ["DORIS_ENV_FILE"].split(os.pathsep))
    env_values = read_env_values(env_paths)
    terms = read_terms(os.environ.get("DORIS_SCAN_TERMS") or os.path.expanduser("~/.doris-scan-terms"))
    if argv[0] == "--staged":
        source = staged_files()
    elif argv[0] == "--all":
        source = all_files()
    else:
        source = given_files(argv)
    findings = []
    count = 0
    for path, data in source:
        count += 1
        findings.extend(scan_blob(path, data, env_values, terms))
    if findings:
        for path, number, rule in findings:
            print("BLOCKED %s:%d %s" % (path, number, rule))
        print("secret scan: %d finding(s) in %d file(s). Nothing was committed or pushed." % (len(findings), count))
        return 1
    print("secret scan: %d file(s), clean (%d .env value(s), %d private term(s) checked)." % (count, len(env_values), len(terms)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
