#!/usr/bin/env python3
"""Extract conversations from Antigravity IDE's local trajectory databases.

Antigravity stores each agent conversation ("trajectory") as a SQLite database
under ~/.gemini/antigravity-ide/conversations/<uuid>.db.  The interesting column
is steps.step_payload, an undocumented protobuf blob.  Antigravity's own chat
history UI has lost these conversations more than once, so this decodes them
directly off disk without needing the IDE.

Nothing outside the standard library is required: the protobuf wire format is
walked by hand (no .proto available), and sqlite3 is used through the Python
module because the sqlite3 CLI is not necessarily installed.

Usage:
    extract_antigravity_session.py --list
    extract_antigravity_session.py --prompts <uuid|path>
    extract_antigravity_session.py --transcript <uuid|path> [-o out.md]
    extract_antigravity_session.py --extract-writes <uuid|path> [--restore DIR]
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB_DIR = Path.home() / ".gemini" / "antigravity-ide" / "conversations"

# Field paths within the decoded step_payload.  Established empirically by
# dumping every string in every step and correlating with the rendered chat;
# they are stable across all five databases present on this machine.
F_USER_MESSAGE = ".19.2"          # user's chat message
F_ASSISTANT_TEXT = ".20.1"        # assistant's rendered markdown reply
F_ASSISTANT_TEXT_DUP = ".20.8"    # same text, repeated by the encoder
F_THINKING = ".20.3"              # assistant reasoning summary
F_TOOL_NAME = ".20.7.2"           # tool being invoked
F_TOOL_ARGS = ".20.7.3"           # tool arguments, JSON-encoded
F_RESULT_ARGS = ".5.4.3"          # tool arguments echoed on the result step
F_RESULT_SUMMARY = ".5.30"        # one-line human summary of the tool result
F_VIEWED_CONTENT = ".14.4"        # file content returned by a view/read tool
F_CONTINUATION = ".30.5"          # summary written when context was compacted


def read_varint(buf, i):
    result = shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def as_text(raw):
    """Return raw decoded as UTF-8 if it looks like text rather than a blob."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    printable = sum(1 for c in text if c.isprintable() or c in "\n\t")
    return text if printable / len(text) > 0.9 else None


def walk(buf, path="", out=None, depth=0):
    """Walk a protobuf message, collecting (field_path, string) pairs.

    Length-delimited fields are ambiguous on the wire: a nested message and a
    string look identical.  Recurse first, and fall back to treating the bytes
    as a string only when the recursion found nothing convincing.
    """
    if out is None:
        out = []
    i, n = 0, len(buf)
    while i < n:
        try:
            key, i = read_varint(buf, i)
        except IndexError:
            return out
        field, wire = key >> 3, key & 7
        if field == 0:
            return out
        sub_path = f"{path}.{field}"
        try:
            if wire == 0:
                _, i = read_varint(buf, i)
            elif wire == 1:
                i += 8
            elif wire == 5:
                i += 4
            elif wire == 2:
                length, i = read_varint(buf, i)
                if length < 0 or i + length > n:
                    return out
                raw, i = buf[i:i + length], i + length
                text = as_text(raw)
                nested = walk(raw, sub_path, [], depth + 1) if depth < 8 else []
                if nested and (text is None or len(nested) > 1):
                    out.extend(nested)
                elif text:
                    out.append((sub_path, text))
            else:
                return out
        except IndexError:
            return out
    return out


def decode_steps(db_path):
    """Yield (idx, step_type, {field_path: [strings]}) for each step."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.text_factory = bytes
    try:
        rows = con.execute(
            "SELECT idx, step_type, step_payload FROM steps ORDER BY idx")
        for idx, step_type, payload in rows:
            if not payload:
                continue
            fields = {}
            for path, text in walk(payload):
                fields.setdefault(path, []).append(text)
            yield idx, step_type, fields
    finally:
        con.close()


def resolve_db(name):
    candidate = Path(name)
    if candidate.is_file():
        return candidate
    candidate = DEFAULT_DB_DIR / name
    if candidate.is_file():
        return candidate
    candidate = DEFAULT_DB_DIR / f"{name}.db"
    if candidate.is_file():
        return candidate
    # Allow a unique uuid prefix.
    matches = sorted(DEFAULT_DB_DIR.glob(f"{name}*.db"))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sys.exit(f"ambiguous session '{name}': {[m.stem for m in matches]}")
    sys.exit(f"no such session: {name}")


def cmd_list(_args):
    if not DEFAULT_DB_DIR.is_dir():
        sys.exit(f"no conversations directory at {DEFAULT_DB_DIR}")
    rows = []
    for db in DEFAULT_DB_DIR.glob("*.db"):
        steps = prompts = 0
        title = ""
        for _, _, fields in decode_steps(db):
            steps += 1
            for message in fields.get(F_USER_MESSAGE, []):
                prompts += 1
                if not title:
                    title = " ".join(message.split())[:70]
        rows.append((db.stat().st_mtime, db.stem, steps, prompts, title))
    for mtime, uuid, steps, prompts, title in sorted(rows):
        import datetime
        when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{uuid}  {when}  {steps:>5} steps  {prompts:>3} prompts  {title}")


def cmd_prompts(args):
    for idx, _, fields in decode_steps(resolve_db(args.session)):
        for message in fields.get(F_USER_MESSAGE, []):
            print(f"[step {idx}] {message}")


def _tool_line(fields):
    """Render a tool invocation as a single readable line."""
    args_json = (fields.get(F_TOOL_ARGS) or fields.get(F_RESULT_ARGS) or [None])[0]
    name = (fields.get(F_TOOL_NAME) or [None])[0]
    if not args_json:
        return None
    try:
        parsed = json.loads(args_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    label = parsed.get("toolSummary") or parsed.get("toolAction") or name
    detail = (parsed.get("CommandLine") or parsed.get("TargetFile")
              or parsed.get("AbsolutePath") or parsed.get("DirectoryPath"))
    if detail:
        detail = " ".join(str(detail).split())
        if len(detail) > 160:
            detail = detail[:157] + "..."
        return f"{label} — `{detail}`"
    return label


def cmd_transcript(args):
    db = resolve_db(args.session)
    out = open(args.output, "w") if args.output else sys.stdout
    try:
        print(f"# Antigravity session `{db.stem}`\n", file=out)
        print(f"Extracted from `{db}` by "
              "`docs/rgw-scheduling/tools/extract_antigravity_session.py`.\n",
              file=out)
        last_tool = None
        for idx, _, fields in decode_steps(db):
            for message in fields.get(F_USER_MESSAGE, []):
                print(f"\n---\n\n## User (step {idx})\n\n{message}\n", file=out)
            for summary in fields.get(F_CONTINUATION, []):
                print(f"\n<details><summary>Context compaction "
                      f"(step {idx})</summary>\n\n{summary}\n\n</details>\n",
                      file=out)
            if args.thinking:
                for thought in fields.get(F_THINKING, []):
                    print(f"> _thinking:_ {' '.join(thought.split())}\n", file=out)
            line = _tool_line(fields)
            if line and line != last_tool:
                print(f"- 🔧 {line}", file=out)
                last_tool = line
            for text in fields.get(F_ASSISTANT_TEXT, []):
                print(f"\n### Assistant (step {idx})\n\n{text}\n", file=out)
                last_tool = None
    finally:
        if args.output:
            out.close()
            print(f"wrote {args.output}", file=sys.stderr)


def cmd_extract_writes(args):
    """Recover files the agent wrote, from the write_to_file tool payloads."""
    seen = {}
    for idx, _, fields in decode_steps(resolve_db(args.session)):
        for args_json in fields.get(F_TOOL_ARGS, []) + fields.get(F_RESULT_ARGS, []):
            try:
                parsed = json.loads(args_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            target, content = parsed.get("TargetFile"), parsed.get("CodeContent")
            if target and content is not None:
                seen[target] = (idx, content)  # last write wins
    for target, (idx, content) in sorted(seen.items()):
        print(f"{target}  ({len(content)} bytes, last written at step {idx})")
        if args.restore:
            dest = Path(args.restore) / Path(target).name
            dest.write_text(content)
            print(f"  -> restored to {dest}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true",
                       help="index every session found on disk")
    group.add_argument("--prompts", metavar="SESSION",
                       help="print only the user's messages")
    group.add_argument("--transcript", metavar="SESSION",
                       help="render the conversation as markdown")
    group.add_argument("--extract-writes", metavar="SESSION",
                       help="list files the agent wrote, with --restore to recover them")
    parser.add_argument("-o", "--output", help="write to this file instead of stdout")
    parser.add_argument("--thinking", action="store_true",
                        help="include the assistant's reasoning summaries")
    parser.add_argument("--restore", metavar="DIR",
                        help="with --extract-writes, write recovered files into DIR")
    args = parser.parse_args()

    if args.list:
        cmd_list(args)
    elif args.prompts:
        args.session = args.prompts
        cmd_prompts(args)
    elif args.transcript:
        args.session = args.transcript
        cmd_transcript(args)
    else:
        args.session = args.extract_writes
        cmd_extract_writes(args)


if __name__ == "__main__":
    main()
