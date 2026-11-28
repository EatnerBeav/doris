"""Send one plain message from the wolf.whizz test account, over JMAP, as a person writes it.

    python3 tests/tools/wolf_sends.py [--env PATH] --subject TEXT (--body TEXT | --body-file PATH)
                                      [--in-reply-to MSGID] [--to ADDRESS]

For a live booking run through the production path (`python -m doris run --live`): wolf emails
the owner one meeting request, the owner's forward brings it to her INBOX, and her live run reads
it like any prospect's mail. With --in-reply-to, wolf answers one of her messages on her thread
instead: In-Reply-To and References name that Message-ID, and the message goes to her address.

  --env PATH          the env file, read with envfile (default paths.LIVE_ENV, as the smoke's). It
                      reads WOLF_EMAIL and WOLF_JMAP_TOKEN (the smoke's wolf settings),
                      DORIS_OWNER_ADDRESS and DORIS_MAIL_ADDRESS, and holds nothing else
  --subject TEXT      the Subject, one line
  --body TEXT         the plain-text body; or
  --body-file PATH    the body from a UTF-8 file
  --in-reply-to MSGID answer her message with this Message-ID: To her address, not the owner's
  --to ADDRESS        optional: the address it goes to anyway (the owner's, or with --in-reply-to
                      hers); anything else is refused

What it sends: exactly one text/plain message, From WOLF_EMAIL, To the owner (or her, with
--in-reply-to) alone, Date now, a fresh Message-ID (32 hex digits at wolf's domain: never the
smoke's mark, live.SMOKE_MID_MARK, so no live run passes it over) and no Auto-Submitted header (she
answers no machine). WOLF_EMAIL must be one of the smoke's two test inboxes (smoke.SEND_TO_ALLOWED)
and neither the owner's address nor hers. The sender is the smoke's wolf sender, a
jmap.JmapSender on WOLF_JMAP_TOKEN, under the network guard allowing only what it allows:
api.fastmail.com, then the host its session names below it once the sender checked it
(netguard.admit). One send, never retried; then one look for wolf's own copy in Sent
(JmapSender.find_message), as the smoke looks for a copy.

It prints one line: the Message-ID it sent and whether wolf's Sent copy was found, or why nothing
went. No line holds a token or any value from the env file. Exit 0 sent (the copy found or not,
said plainly); 1 not sent (refused: it surely did not go) or unknown (it may have gone: look in
wolf's Sent for the Message-ID before sending again); 2 the command line or the env file is wrong,
and then nothing is contacted.
"""

import argparse
import datetime as _dt
import email.message
import email.policy
import email.utils
import os
import secrets
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from doris import envfile, loop, netguard, paths, smoke         # noqa: E402  (src is on the path only now)
from doris.adapters import jmap, live                           # noqa: E402
from doris.shapes import MailError, SendUnknown                 # noqa: E402

UTC = _dt.timezone.utc
NAMES = (smoke.WOLF_ADDRESS, smoke.WOLF_TOKEN, loop.ENV_OWNER, live.ENV_ADDRESS)   # all it reads
PROG = "python3 tests/tools/wolf_sends.py"


class Refused(ValueError):
    """The command line or the env file is wrong. Its text names a setting or an argument, never a value."""


def _print(line):
    print(line, flush=True)


def _parser():
    ap = argparse.ArgumentParser(prog=PROG, description="Send one plain message from the wolf.whizz test account "
                                                        "to the owner (or, with --in-reply-to, to her), over JMAP.")
    ap.add_argument("--env", default=paths.LIVE_ENV, metavar="PATH", help="the env file (default %(default)s)")
    ap.add_argument("--subject", required=True, metavar="TEXT", help="the Subject, one line")
    body = ap.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", metavar="TEXT", help="the plain-text body")
    body.add_argument("--body-file", metavar="PATH", help="the plain-text body, from a UTF-8 file")
    ap.add_argument("--in-reply-to", metavar="MSGID",
                    help="answer her message with this Message-ID, on her thread: it goes to her address")
    ap.add_argument("--to", metavar="ADDRESS",
                    help="optional: the owner's address (with --in-reply-to, hers); anything else is refused")
    return ap


# ------------------------------------------------------------------------------------------------
# The arguments and the settings, all checked before anything is contacted
# ------------------------------------------------------------------------------------------------
def _text(args):
    """(subject, body). Refused for an empty or multi-line subject, an empty body or a body file it cannot read."""
    subject = args.subject.strip()
    if not subject or "\r" in subject or "\n" in subject:
        raise Refused("--subject must be one line of text")
    if args.body_file is not None:
        try:
            with open(args.body_file, encoding="utf-8") as fh:
                body = fh.read()
        except (OSError, ValueError) as exc:
            raise Refused("--body-file cannot be read (%s)" % type(exc).__name__) from None
    else:
        body = args.body
    if not body.strip():
        raise Refused("the body is empty")
    return subject, body


def _her_mid(value):
    """The Message-ID --in-reply-to names, in angle brackets, or Refused."""
    bare = value.strip()
    if bare.startswith("<") and bare.endswith(">"):
        bare = bare[1:-1]
    if not jmap._MESSAGE_ID.match(bare):
        raise Refused("--in-reply-to must be one Message-ID")
    return "<%s>" % bare


def _settings(env):
    """(wolf's JmapSender, the owner's address, her address). live.ConfigError naming the setting,
    never its value: WOLF_EMAIL must be one of the two test inboxes, and neither hers nor the owner's."""
    wolf = jmap.JmapSender(env, (env.get(smoke.WOLF_ADDRESS) or "").strip(), address_name=smoke.WOLF_ADDRESS,
                           token_name=smoke.WOLF_TOKEN)
    if wolf.from_addr.lower() not in smoke.SEND_TO_ALLOWED:
        raise live.ConfigError("%s must be one of the two test inboxes the smoke takes" % smoke.WOLF_ADDRESS)
    owner = live.bare_address(env, loop.ENV_OWNER)
    doris = live.bare_address(env, live.ENV_ADDRESS)
    if wolf.from_addr.lower() in (owner.lower(), doris.lower()):
        raise live.ConfigError("%s must be the test account's own address, not hers or the owner's"
                               % smoke.WOLF_ADDRESS)
    return wolf, owner, doris


def _recipient(owner, doris, in_reply_to, asked):
    """The one address it sends to: the owner's, or with --in-reply-to hers. --to may only repeat it."""
    to = doris if in_reply_to is not None else owner
    if asked is not None and asked.strip().lower() != to.lower():
        raise Refused("refused --to: it takes only the owner's address (%s), or with --in-reply-to hers (%s)"
                      % (loop.ENV_OWNER, live.ENV_ADDRESS))
    return to


# ------------------------------------------------------------------------------------------------
# The message
# ------------------------------------------------------------------------------------------------
def new_message_id(from_addr):
    """A fresh Message-ID at the sender's domain: 32 hex digits, which never hold the smoke's mark."""
    mid = "<%s@%s>" % (secrets.token_hex(16), from_addr.rsplit("@", 1)[-1])
    assert live.SMOKE_MID_MARK not in mid.lower()
    return mid


def message(from_addr, to_addr, subject, body, message_id, now, in_reply_to=None):
    """One plain-text message as a person writes it (no Auto-Submitted), as the smoke's request is built."""
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(now.astimezone(UTC).replace(microsecond=0))
    msg["Message-ID"] = message_id
    if in_reply_to is not None:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg.as_bytes()


# ------------------------------------------------------------------------------------------------
# The send and the look
# ------------------------------------------------------------------------------------------------
def _look(wolf, message_id):
    """What one look for wolf's own copy found, said plainly."""
    try:
        roles = wolf.find_message(message_id)
    except Exception as exc:
        return "a look for wolf's Sent copy could not tell (%s)" % smoke._why(exc)
    if roles and "sent" in roles:
        return "wolf's Sent copy found"
    if roles:
        return "wolf's Sent copy not found on one look (a copy is in: %s)" % ", ".join(roles)
    return "wolf's Sent copy not found on one look"


def send(wolf, to_addr, raw, message_id, who):
    """Send once under the network guard, then look once for wolf's copy. (exit code, the one line)."""
    guard = netguard.install(jmap.hosts())
    try:
        try:
            wolf.send(raw, [to_addr], message_id)
        except Exception as exc:        # MailError: surely not sent; SendUnknown, or anything else: it may have gone
            if isinstance(exc, MailError) and not isinstance(exc, SendUnknown):
                return 1, "wolf_sends: not sent (%s); nothing went" % (smoke._why(exc),)
            return 1, ("wolf_sends: send unknown, it may have gone (%s): look in wolf's Sent for %s before "
                       "sending again" % (smoke._why(exc), message_id))
        return 0, "wolf_sends: sent %s to %s; %s" % (message_id, who, _look(wolf, message_id))
    finally:
        guard.uninstall()


def main(argv, out=None):
    out = out or _print
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:           # --help (0), or argparse's own refusal on stderr (2)
        return 0 if exc.code in (0, None) else 2
    try:
        subject, body = _text(args)
        her_mid = _her_mid(args.in_reply_to) if args.in_reply_to is not None else None
        env = envfile.load(args.env, names=NAMES)
        wolf, owner, doris = _settings(env)
        to_addr = _recipient(owner, doris, her_mid, args.to)
    except Refused as exc:
        out("wolf_sends: %s" % (exc,))
        return 2
    except envfile.EnvFileError as exc:
        out("wolf_sends: env file: " + smoke._why(exc))
        return 2
    except live.ConfigError as exc:
        out("wolf_sends: config: " + smoke._why(exc))
        return 2
    message_id = new_message_id(wolf.from_addr)
    raw = message(wolf.from_addr, to_addr, subject, body, message_id, _dt.datetime.now(UTC), her_mid)
    who = "her, in reply to %s" % her_mid if her_mid is not None else "the owner"
    code, line = send(wolf, to_addr, raw, message_id, who)
    out(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
