# Skills

One directory per skill, `skills/<name>/SKILL.md`, in one of three families. The family is
declared in the skill's frontmatter as `metadata: {family: "..."}` and is how a reader, a lane
worker, and the sweep profile decide whether a skill is theirs.

| Family | Who loads it | What it must teach |
|---|---|---|
| `coding` | an agent building or changing a stump.wtf product | the house way to build the thing: layout, pipeline, tests, UI, persistence, release — with the drift it resolves named |
| `operating` | a scheduled sweep agent (small local model, hard caps) or a human operating StumpCloud | how to derive fleet state, triage outside-in, remediate within the sweep caps, and hand off by issue |
| `review` | an agent reviewing a PR, closing an issue, or auditing a repo | how to verify the property rather than a proxy, what to refuse, what to fix in place and how to say so |

Placing a new skill:

- If a sweep must be able to load it, it is `operating`, and its PR must say so: the sweep
  profile only loads skills named in `SWEEP_KEEP_SKILLS` (dotfiles, `sweeps/stumpcloud-sweep/crushrc`),
  so the merge alone changes nothing a sweep can see.
- If it encodes a convention the apps follow, it is `coding`, even when ops agents also read it.
- If it encodes a judgment a reviewer makes, it is `review`, even when the subject is Ansible.
- A skill that belongs to two families is two skills, or one skill with the other half in its
  `references/`.

The authoring contract (caps, harness-agnosticism, verification, scripts) is in the repo-root
`CONTRIBUTING.md`; the model behind it is the `agent-skill-authoring` skill.
