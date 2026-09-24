# Reaching a catalog in more languages than it was written in

An author writes the trigger phrases of a procedure once, in the language they
work in. The catalog is reached in more languages than that. Translating every
trigger by hand does not scale and rots the moment a trigger is edited, and
asking a model at turn time would spend a call on every turn.

So the paraphrases are produced once, outside a turn, and written to a file
beside the skill. The index reads that file alongside the declared triggers.
Nothing about this changes what one turn costs.

## The rule that governs it

No feature adds a model call to a turn.

Everything below follows from it. The derivation happens when a procedure is
published or when somebody runs the command; the reading happens from disk; the
file is stamped so that a stale one is ignored rather than trusted. If the only
way to reach a catalog in a further language were to ask a model during the
turn, the answer would be that the catalog is not reached in that language.

## The derived file

One file per skill version, at:

```
<catalog root>/<type>/<name>/v<version>/generated-examples.json
```

The name is `enrichment.filename` in `configs/runtime.json`. It sits inside the
version directory so that it travels with the version it describes, is copied
with it, and is removed with it.

```json
{
  "source_hash": "9c1f...",
  "languages": {
    "it": ["riavvia il servizio di ricerca", "puoi far ripartire payments"],
    "de": ["starte den checkout-dienst neu"]
  }
}
```

Nothing in it is edited by hand. The declared triggers stay the only authored
truth; these are derived from them and are what gives way when the two
disagree.

## The source hash

`source_hash` is a digest of the declared triggers the file was made from.

Every read compares it against the triggers as they are now. If it does not
match, the whole file is ignored — not partially trusted, not repaired. A file
written from triggers that have since been edited describes a procedure that no
longer exists, and a paraphrase of a phrase nobody declares any more is worse
than no paraphrase at all. The fix is always to regenerate.

That is also why the hash covers the triggers rather than the whole skill file:
editing the body of a runbook does not invalidate a paraphrase of how somebody
asks for it, and editing a trigger does.

## When it regenerates

Two occasions, and no others.

**At publication.** When a proposal is published and a provider is configured
and `enrichment.enabled` is on, the derived phrases of the new version are
written as part of the same operation — beside the package inside the change
request that publication composes, so they travel with it and land in the
catalog when a person applies it. Whatever happens is recorded in the
transition note and nowhere else: the composed package already stands, and
nothing about it is undone by a paraphrase that could not be produced.

**When somebody asks.** After a catalog is edited by hand, after a language is
added to the configuration, or on a schedule:

```sh
oska enrich run                              # every procedure missing a language
oska enrich run --language it --language de  # these languages, not the configured ones
oska enrich run --dry-run                    # what a pass would write, asking nothing
oska enrich run --json
oska enrich show runbook.service_restart     # declared first, derived after
```

Without `--language`, the languages come from `discovery.languages`. A pass
covers every published skill that declares triggers and is missing any of those
languages, one model call per skill, and reports one line per skill —
`written`, `skipped` or `failed` with the reason. One skill that fails does not
end the pass, because a catalog half enriched is still better routed than one
not enriched at all; the command exits non-zero if anything failed. `--dry-run` reports the same lists without asking a provider for
anything, which is how to find out what a pass would cost before paying for it.

`enrich run` needs a provider: `--provider openai_compatible`, or `--provider
scripted --script <file>` to exercise it offline, or the one the settings name.
There is nothing sensible to degrade to, so with the provider `none` the
command stops and says so. This is the one place that differs from a turn,
which degrades rather than stopping.

`enabled` is what the runtime consults, not what the command consults. A person
running `enrich run` is asking for the files whatever the flag says; with the
flag off they are written and ignored.

## What the model is asked for

The description of the procedure and the phrases its author wrote down, with
the instruction to write further phrases a person could use to ask for the same
thing, one set per language, keeping the meaning exactly and adding nothing the
declared phrases do not already say — the way a person asks for something, not
the way a document describes it.

What comes back is filtered before it is written. A phrase that repeats a
declared trigger is dropped, so is a repeat of another derived phrase, and each
language is cut to `enrichment.per_language`. A model that writes something
imaginative produces a phrase nobody matches, not a capability nobody declared:
these texts only ever raise the score of the procedure they belong to.

## What the index does with it

A skill is indexed under several texts — its description, each trigger, each
derived phrase, and each phrasing the improvement job has learned — and scores
the best of them rather than their average. More phrasings can therefore raise
a score and never dilute one. `matched_on` reports which text won, with its
language, which is how a routing surprise is explained: `oska enrich show`
prints the declared phrases and then the derived ones, and one of them is the
answer.

`enrichment.cap_per_route` bounds how many texts one skill contributes, and it
never cuts a declared one. What a person wrote is the truth; the derived
phrases are what give way.

One mechanical detail matters when a derived file is written while a process is
up. The registry fingerprints the `skill.md` files it discovers — path, size,
modification time — so writing a file beside one of them leaves that
fingerprint unchanged and would not by itself rebuild an index. The index folds
in a fingerprint of the derived files as well, which is what makes a newly
written file take effect without a restart.

## Configuration

`enrichment` and `discovery.languages` in
[configuration.md](configuration.md#enrichment). The short version:
`discovery.languages` says which languages the catalog is expected to be
reached in, `enrichment.enabled` says whether the runtime reads the derived
files, `enrichment.per_language` sets the size of a pass, and
`enrichment.cap_per_route` bounds the index.
