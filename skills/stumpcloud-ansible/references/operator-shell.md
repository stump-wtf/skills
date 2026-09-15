# The operator shell

Where StumpCloud commands actually run, and why the same line can be correct in one place
and silently broken in the other.

There are two shells in every StumpCloud task and they are not alike:

- **`[mac]`** — the operator Mac. BSD userland, non-interactive, **no credentials in the
  environment**. This is where the `stumpcloud/ansible` checkout lives and where every
  `ansible`, `git`, `gh`, `tea`, and `vault` command runs.
- **`[host]`** — a fleet host over SSH. Debian or Ubuntu, GNU coreutils, Docker present.
  This is where `docker`, `systemctl`, `zpool`, `journalctl`, and `restic` run.

**Tag every fenced command block with which one it is.** A command that does not say where
it runs will eventually be run in the wrong place, and the BSD/GNU differences below mean
it fails in a way that reads like a fact about the fleet rather than a broken command.

## BSD, not GNU

Verified on the operator Mac, 2026-08-30. These are not "usually present" — they are absent
or reject the flag outright.

| Wanted | `[mac]` result | Use instead on `[mac]` |
|---|---|---|
| `timeout 5 ...` | **not installed** — exit 127 | no timeout, or run it `[host]` |
| `gtimeout` | not installed | same |
| `date -d '1 hour ago'` | `date: illegal option -- d` | `date -v-1H` |
| `date -d @1724968800` | illegal option | `date -r 1724968800` |
| `stat -c %s f` | `stat: illegal option -- c` | `stat -f %z f` |
| `head -n -1` | `head: illegal line count` | `sed '$d'` |
| `gdate`, `gstat` | not installed | as above |
| `restic`, `autorestic` | not installed | `[host]` `docker exec autorestic ...` |

The dangerous half is the interaction with error suppression. A `[mac]` line reading
`timeout 5 docker inspect x || echo WEDGED` prints `WEDGED` because `timeout` does not
exist, not because anything is wedged. Both halves of that pattern — a missing binary and
a rejected flag, each swallowed by `|| true` or `2>/dev/null` — are live in `.claude-ops/`
today, and neither is a bug in the fleet.

**Never wrap a diagnostic in `2>/dev/null` or `|| true` before you have seen it succeed
once.** Suppression turns "this command does not exist here" into a diagnosis.

## No credentials in the environment

`[mac]` `VAULT_ADDR`, `VAULT_TOKEN`, `GITEA_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, and the AWS
variables are **all empty**. The shell is non-interactive, so nothing that a login shell
would export is present. Each tool has its own store:

| Tool | Authenticates from | What you must do |
|---|---|---|
| `gh` | the macOS keyring | nothing — `gh auth status` confirms |
| `tea` | `~/.config/tea/config.yml` | nothing |
| `vault` | `~/.vault-token` | `export VAULT_ADDR=https://vault.stump.rocks` yourself |
| `ssh` | agent + `~/.ssh/config` | nothing |

An empty `VAULT_ADDR` is the usual cause of a `vault` command that appears to hang or
refuses with a connection error. Export the address; do not go looking for the token.

### Never print a token

`vault token lookup -format=json` returns the **live token** in `data.id`, and there is no
`-field` flag on that subcommand to avoid it. Never run it unfiltered — the value lands in
the transcript, and a transcript is stored, summarized, and replayed.

```sh
# [mac] identity check that reveals nothing usable
export VAULT_ADDR=https://vault.stump.rocks
vault token lookup -format=json | python3 -c 'import json,sys; \
d=json.load(sys.stdin)["data"]; print({k: d.get(k) for k in ("display_name","policies","ttl")})'
```

The same applies to `git remote -v` and `git config --list` in any checkout whose remote
could carry an embedded credential, to `curl -v`, and to `docker inspect`.

## SSH lands as joestump on service hosts, root on hypervisors

The operator `~/.ssh/config` sets no `User` for `*.stump.rocks`, so an unqualified login
falls through to the local username. Whether that account exists on the far side is a
property of the host, not of the config, and the two classes answer differently. Verified
`[mac]`, 2026-08-30:

| Host class | `ssh <host> id -un` | `ssh root@<host> id -un` |
|---|---|---|
| service hosts (`ie01`, `ie02`, ...) | `joestump` | refused: *Please login as the user "joestump"* |
| hypervisors (`lir`, `dagda`, ...) | `Permission denied (publickey,password)` | `root` |

So there is no single right answer to prefix a command with. **Derive the class before you
connect** — hypervisors and appliances are the hosts the inventory gives `ansible_user:
root`, because they carry no `joestump` account:

```sh
[mac] ansible-inventory -i dub.yaml --list | python3 -c 'import json,sys; \
d=json.load(sys.stdin, strict=False); \
print(sorted(h for h,v in d["_meta"]["hostvars"].items() if v.get("ansible_user")=="root"))'
```

`ansible_user` is still not the same question as an interactive login — it is Ansible's
per-host connection identity — but on this fleet the two coincide, and it is the only
machine-readable signal for the split.

Prose gets this wrong in both directions, so do not take it from prose. `CLAUDE-OPS.md`
used to say `root@` universally; since PR #578 (2026-08-30) it says `joestump@`
universally, and its own hypervisor rows now print `joestump@lir.stump.rocks`, a login
`lir` refuses. When a `[host]` command needs privilege on a service host, use `sudo`.

## Three different ansibles

This is the trap that produces "it worked locally".

| Where | Version | How you get it |
|---|---|---|
| `[mac]` bare `PATH` | **ansible-core 2.10.15** (Homebrew) | `/opt/homebrew/bin/ansible-playbook` |
| `[mac]` repo venv | 2.21.x, Python 3.13 | `pipenv run ...`, or `./.venv/bin/...` |
| CI | ansible-core **2.17.14**, Python 3.10 | the digest-pinned `ansible-runner` image |

Measured 2026-08-30; the exact patch levels move, the gap does not. Print the version
rather than assuming it:

```sh
# [mac] from the repo root
./.venv/bin/ansible-playbook --version | head -1
```

Homebrew's 2.10 is eleven minor releases behind the repo pin and is not what anything else
runs. Never invoke a playbook with the bare command.

The venv-to-CI gap is real and does not reproduce locally: on 2026-08-22 a Handlebars
template guarded with `{% raw %}` rendered correctly in the venv and died in CI, because
whether a `set_fact` result is auto-marked unsafe changed between those two versions. The
`Pipfile` header and the `ci.yaml` preamble both document it. When a template passes locally
and reds `main`, suspect this before suspecting your change.

## Running a playbook from the Mac

```sh
# [mac] from the repo root
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES pipenv run ansible-playbook \
  -i dub.yaml playbooks/services/<name>.yaml --limit <host> --check --diff
```

Four things in that line are load-bearing:

1. **`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`.** macOS aborts forked Objective-C runtime
   calls without it. It is the first thing in the repo `README.md` for that reason.
2. **`-i <site>.yaml`, always.** `ansible.cfg` sets `inventory = ./dub.yaml`, so a command
   without `-i` silently answers about DUB regardless of which site you meant.
3. **`pipenv run`** — see the three-ansibles table.
4. **`--check --diff` first**, on anything touching a live service, then `--limit <host>` to
   keep the blast radius to one box.

For a playbook that only calls an API (DNS, Route53) when the host is unreachable, the repo
`CLAUDE.md` documents the `-c local` variant; use it as written rather than improvising the
interpreter path.

`make test`, `make lint`, and `make check` wrap `.gitea/scripts/run-tests.sh` and
`run-lint.sh`, which CI invokes directly, so local and CI cannot drift. From a worktree the
`Makefile` borrows the main checkout's `.venv` — a worktree has none of its own, and
`pipenv run` inside one resolves to an unrelated interpreter.
