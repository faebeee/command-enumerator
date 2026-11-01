# command-enumerator

A small CLI tool to run a command against every word in a wordlist, substituting
the `[[ENUM]]` placeholder with each word. Commands run in parallel with live
streaming output; failed runs are cleared automatically, and successful ones
are kept visible and summarized at the end.

## Features

- Substitutes `[[ENUM]]` in a command template with each line from a wordlist
- Runs commands in parallel (configurable concurrency)
- Streams live output per command, showing only the last N lines
- Successful commands move into a persistent green "Successful" panel
- Failed commands move into a persistent red "Failed" panel (output stays visible, nothing is cleared)
- Configurable success criteria (exit code only, or exit code + empty stderr)
- Optional per-command timeout
- Final summary listing every successful and failed word with the exact command run

## Requirements

- Python 3.9+
- [rich](https://github.com/Textualize/rich)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python enumerator.py --command "ping [[ENUM]]" --wordlist ./src/wordlist.txt
```

### Options

| Flag              | Required | Default      | Description |
|-------------------|----------|--------------|-------------|
| `--command`       | yes      | -            | Command template containing the `[[ENUM]]` placeholder, e.g. `'ping [[ENUM]]'`. Parsed with shell-like quoting rules (`shlex`), then executed directly without invoking a shell. |
| `--wordlist`      | yes      | -            | Path to a newline-separated wordlist file. Empty lines are ignored. |
| `--concurrency`   | no       | `1`          | Number of commands to run in parallel. |
| `--success-mode`  | no       | `exit-code`  | `exit-code`: success if the process exits with code `0`. `exit-and-stderr`: success if exit code is `0` **and** stderr was empty. |
| `--timeout`       | no       | none         | Per-command timeout in seconds. A command that times out is killed and counted as a failure. |
| `--tail-lines`    | no       | `10`         | Number of trailing output lines shown per live command panel. |
| `--delay`         | no       | `0`          | Minimum delay in seconds between successive command launches. |
| `--stop-on-success` | no     | off          | Stop immediately once the first command succeeds: no new commands are launched, and any currently running commands are killed. |

### Example

```bash
python enumerator.py \
  --command "ping -c 1 -W 1 [[ENUM]]" \
  --wordlist ./src/wordlist.txt \
  --concurrency 5 \
  --success-mode exit-code \
  --timeout 5 \
  --tail-lines 5
```

While running, you'll see a live panel per in-flight command with its latest
output. As each command finishes:

- **Success** → moves into a green "Successful" panel that stays visible.
- **Failure** (non-zero exit, error, or timeout) → moves into a red "Failed"
  panel that also stays visible. Nothing is cleared from the display.

At the end, a summary is printed:

```
Summary: 4/5 succeeded, 1/5 failed

Successful:
  - 127.0.0.1   (ping -c 1 -W 1 127.0.0.1)
  - localhost   (ping -c 1 -W 1 localhost)
  - 8.8.8.8   (ping -c 1 -W 1 8.8.8.8)
  - example.com   (ping -c 1 -W 1 example.com)

Failed:
  - this-host-does-not-exist.invalid   (failed, ping -c 1 -W 1 this-host-does-not-exist.invalid)
```

## Wordlist format

A plain text file with one entry per line. Blank lines are skipped.

```
127.0.0.1
localhost
8.8.8.8
example.com
```

## Notes

- `[[ENUM]]` can appear standalone or embedded within a larger argument after
  shell-like tokenization. For example `"ping [[ENUM]]"`, `"ping[[ENUM]]"`,
  and `"[[ENUM]]:foobar"` are all valid, and multiple occurrences within the
  same argument are all substituted.
- Commands are executed directly via `subprocess` (argv list), not through a
  shell, so shell operators like `|`, `&&`, or `>` in `--command` will not work
  as shell syntax — they'd be passed as literal arguments.
- With `--stop-on-success`, once a command succeeds no new commands are
  launched and any commands still in flight are killed. Words that were never
  attempted or were killed mid-run are reported in a separate
  "Skipped/Aborted" section of the summary.
