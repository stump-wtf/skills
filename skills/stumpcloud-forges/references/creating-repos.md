# Creating and configuring a repository

Creating a repo is not "make it and push a README." It is finished only when every section below
holds, and all of it happens in **the same task that creates the repo**. A half-configured repo is
worse than none: the gaps surface later as a broken deploy, a mirror nobody updates, or a PR that
merged with no check gating it.

## 0. Is a control plane going to own this repo?

Check before creating anything by hand. Each Gitea namespace that uses the repo factory has a
workspace repo named `<owner>/<owner>-repositories`. Its `README.md` and `repos.yaml` say how to
vend a repo: append an entry (`name` and `description` are the only required fields) and open a PR.
The factory plans and policy-checks it on the PR and applies it on merge to `main`, and its
protection floor cannot be lowered from the entry.

```sh
# operator Mac -- does this namespace vend through a factory? (private repo: needs a token)
curl -s -H "Authorization: token $TOKEN" \
  https://gitea.stump.rocks/api/v1/repos/<owner>/<owner>-repositories | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(d.get("full_name"), d.get("description"))'
```

If it exists:

- Every field the factory declares (read `repos.yaml` and the module it calls for the current list)
  is set **only** through a PR to that workspace. Setting it by API is drift the next apply reverts,
  or worse, a diff the next plan fights.
- Agents hold read-only Gitea repo tokens by design. A `401` on a repo write is the floor working;
  open the PR or hand it to the operator.
- Whatever the factory does *not* manage still needs doing by hand — typically the GitHub side and
  the push mirror. Derive which from the module, not from this file.

If it does not exist, do every step below yourself, in order.

## 1. Put it in the right place

- **Default to the `stump.wtf` org.** Infra goes to `stumpcloud`. The personal namespace is only for
  something genuinely personal.
- **Name it idiomatically for its ecosystem**: an Oh My Zsh plugin is `zsh-<name>`, a Terraform
  provider `terraform-provider-<name>`, and so on.
- **Set up the GitHub push mirror.** Create `github.com/stump-wtf/<repo>` first, then add the mirror
  on the Gitea side. Gitea stays authoritative; GitHub is read-only downstream.

```sh
# operator Mac
gh repo create stump-wtf/<repo> --public --description "<one sentence>"
# then, against the Gitea API with a token that can administer the repo:
#   POST /repos/stump.wtf/<repo>/push_mirrors
#   {"remote_address":"https://github.com/stump-wtf/<repo>.git",
#    "remote_username":"<github user>","remote_password":"<token from the secret store>",
#    "interval":"8h0m0s","sync_on_commit":true}
```

Never paste the mirror credential into a command line or a transcript; read it from the secret
store into an environment variable the request reads. After the first sync, **compare the two heads**
rather than trusting the mirror's empty `last_error` — an empty error field is not proof of delivery.

## 2. Add the operator's agent account

Every repo made "for us" — public or private, whoever asked, either forge — gets the operator's
agent account as a collaborator with **write** access. Never hand back a repo the agent cannot
reach. The account name lives in the private agent rules, not in this public repo.

- **Gitea:** `PUT /repos/<owner>/<repo>/collaborators/<agent>` with `{"permission":"write"}`.
- **GitHub:** `gh api -X PUT /repos/<owner>/<repo>/collaborators/<agent> -f permission=push`. GitHub
  sends an invitation the agent account must accept.

## 3. Fill in the metadata

An unlabelled repo is undiscoverable, and a repo with no website link sends everyone hunting.

- **Description**: one sentence saying what it does, not a restatement of the name.
- **Search topics**: the ecosystem and domain tags someone would look for (`go`, `mcp`, `ansible`,
  `zsh-plugin`, and so on).
- **Canonical-host topics — mandatory, on both hosts.** `canonical-gitea` or `canonical-github` on
  each copy, plus `downstream-mirror` on the copy. Topics are not replicated by the mirror. A repo
  without them makes every future agent guess which copy is real.
- **Website URL**: point it at the docs, not the source. The Gitea repo points at its Gitea Pages
  site (`https://<owner>.pages.stump.rocks/<repo>/`); the GitHub mirror points at the public site or
  its GitHub Pages twin. Never put the Gitea Pages URL on the GitHub side: it resolves to a LAN
  address on public DNS.
- **Issue labels**: at minimum `feature`, `bug`, and `toil`, so PRs can be labelled to match their
  branch prefix.

## 4. Ship the boilerplate for its kind

Every repo gets:

- `README.md` — what it is, how to run it, how to test it.
- `LICENSE`.
- A `.gitignore` matched to the stack.
- A `Makefile` exposing `test`, `lint`, and `check`, wrapping the native tools.

Then what the type demands: a Go service needs a `Dockerfile`, a library needs usage docs, anything
with a docs site needs its generator wired up.

## 5. Protect `main` and require the checks

- Branch protection on `main`: no direct pushes, PR required, **CI must pass before merge**.
- **Mark the status checks required.** A check that runs but is not required is decoration, and a
  red PR stays mergeable. Require only contexts the repo has actually reported, or every PR wedges.
- Prefer linear history: rebase or squash merges.

The `stumpcloud-ci` skill covers the exact context strings and the idempotent script that applies
protection without hand-rolling the API call.

## 6. Wire up CI before the first real PR

A repo whose first PR arrives before CI exists gets merged unverified, and that becomes the habit.
The minimum pipeline (tests, lint, secret scan, ship on merge) and the rules for the workflows
themselves are in the `stumpcloud-ci` skill.

## Done means verified

Before reporting the repo finished, check each property directly rather than trusting the call that
set it: read both hosts' topic lists back, confirm the collaborator appears, push a trivial branch
and watch the required checks post, and compare the GitHub head with the Gitea head after a sync.
